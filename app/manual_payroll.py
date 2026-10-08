"""One manual attendance/payroll engine shared by web, exports and mobile.

Every write is committed by the caller. Activation and opening reset are atomic.
Reviewed days and finalized salary/leave snapshots never replace legacy history.
"""
import calendar
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .extensions import db
from .models import (InitialSetupState, User, StaffProfile, StaffAttendance,
                     InventoryCategory, InventoryExpenseLog,
                     ReusableInventoryLossAllocation, ReusableInventoryLossEvent)
from .manual_payroll_models import (ManualEmployment, ManualAttendance, ManualPayrollMonth,
    ManualCashSettlement, ManualCashAllocation, ManualPayrollPayment, ManualPayrollAudit,
    ManualPayrollAsset)
from .manual_attendance_policy import (EFFECTIVE_DATE, STATUS_LABELS, ManualDay,
    calculate_days, month_cash_entitlement, service_allowance)

KEY = 'manual_payroll_2026_10_v1'


def today():
    return datetime.now(ZoneInfo('Asia/Kolkata')).date()


def active():
    from flask import g, has_request_context
    if has_request_context() and hasattr(g, '_manual_payroll_active'):
        return g._manual_payroll_active
    enabled = db.session.get(InitialSetupState, KEY) is not None
    if has_request_context():
        g._manual_payroll_active = enabled
    return enabled


def money(value):
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError('Enter a valid amount.') from exc
    if not number.is_finite() or number < 0 or number > Decimal('9999999999'):
        raise ValueError('Amount must be finite and non-negative.')
    return number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def audit(user_id, actor, action, details):
    db.session.add(ManualPayrollAudit(user_id=user_id, actor_id=actor.id,
        action=action, details_json=json.dumps(details, default=str)))


def require_admin(actor):
    if not actor or not actor.has_role('admin'):
        raise PermissionError('An administrator must review and finalize payroll.')


def activate(actor, *, include_admins=False):
    require_admin(actor)
    if active():
        return []
    from .manual_leave_reset import reset_staff_opening_balances
    changed = reset_staff_opening_balances(actor, include_admins=include_admins)
    db.session.add(InitialSetupState(key=KEY))
    audit(actor.id, actor, 'activate', {'effective_date': EFFECTIVE_DATE, 'include_admins': include_admins, 'reset_user_ids': changed})
    db.session.flush()
    return changed


def bounds(month):
    if month.day != 1 or month < EFFECTIVE_DATE:
        raise ValueError('Choose October 2026 or later.')
    return month, date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])


def employment(user_id):
    row = db.session.get(ManualEmployment, user_id)
    if not row:
        raise ValueError('An admin must confirm employment dates first.')
    return row


def save_employment(user_id, actor, joined_on, left_on, notice_days, released, version):
    require_admin(actor)
    profile = StaffProfile.query.filter_by(user_id=user_id).first()
    if not profile:
        raise ValueError('Select a staff account.')
    if not joined_on or (left_on and (left_on < joined_on or left_on > today())) or notice_days < 0:
        raise ValueError('Invalid employment dates or notice period.')
    if left_on and notice_days > (left_on-joined_on).days+1:
        raise ValueError('Notice days exceed the employment period.')
    row = db.session.get(ManualEmployment, user_id)
    if (row.version if row else 0) != version:
        raise ValueError('Employment was changed elsewhere. Reload before saving.')
    finalized = ManualPayrollMonth.query.filter_by(user_id=user_id).order_by(ManualPayrollMonth.month.desc()).first()
    if finalized and (joined_on != row.joined_on or row.left_on or (left_on and left_on <= bounds(finalized.month)[1])):
        raise ValueError('These dates affect finalized payroll. Historical employment is locked.')
    if ManualAttendance.query.filter(ManualAttendance.user_id == user_id,
        db.or_(ManualAttendance.day < joined_on, ManualAttendance.day > (left_on or date.max))).first():
        raise ValueError('Employment dates exclude reviewed attendance. Correct attendance first.')
    old = {'joined_on': row.joined_on, 'left_on': row.left_on, 'notice_days': row.notice_days,
           'management_released': row.management_released} if row else None
    if not row:
        row = ManualEmployment(user_id=user_id)
        db.session.add(row)
    row.joined_on, row.left_on = joined_on, left_on
    row.notice_days, row.management_released, row.confirmed_by = notice_days, released, actor.id
    audit(user_id, actor, 'employment', {'before': old, 'joined_on': joined_on, 'left_on': left_on,
        'notice_days': notice_days, 'management_released': released})
    db.session.flush()


def save_day(user_id, actor, day, status, notified_on, half_applied, bereavement, notes, version):
    require_admin(actor)
    emp = employment(user_id)
    if day < max(EFFECTIVE_DATE, emp.joined_on) or day > min(today(), emp.left_on or today()):
        raise ValueError('Attendance must be within employment dates, from October 1, and not in the future.')
    if ManualPayrollMonth.query.filter(ManualPayrollMonth.user_id == user_id,
        ManualPayrollMonth.month >= day.replace(day=1)).first():
        raise ValueError('This day affects finalized payroll and is locked.')
    entry = ManualDay(day, status, notified_on, half_applied, bereavement)
    calculate_days([entry], joined_on=emp.joined_on, left_on=emp.left_on)
    row = ManualAttendance.query.filter_by(user_id=user_id, day=day).first()
    if (row.version if row else 0) != version:
        raise ValueError('Attendance changed elsewhere. Reload before saving.')
    old = {key: getattr(row, key) for key in ('status','notified_on','half_day_applied','bereavement_eligible','notes')} if row else None
    if not row:
        row = ManualAttendance(user_id=user_id, day=day)
        db.session.add(row)
    row.status, row.notified_on = status, notified_on
    row.half_day_applied, row.bereavement_eligible = half_applied, bereavement
    row.notes, row.reviewed_by = notes[:500], actor.id
    audit(user_id, actor, 'attendance_review', {'day': day, 'before': old, 'after': {
        'status': status, 'notified_on': notified_on, 'half_day_applied': half_applied,
        'bereavement_eligible': bereavement, 'notes': notes[:500]}})
    db.session.flush()


def month_preview(user_id, month, salary=None):
    start, end = bounds(month)
    emp = employment(user_id)
    service_start, service_end = max(start, emp.joined_on, EFFECTIVE_DATE), min(end, emp.left_on or end)
    rows = ManualAttendance.query.filter(ManualAttendance.user_id == user_id,
        ManualAttendance.day >= max(EFFECTIVE_DATE, date(month.year,1,1)), ManualAttendance.day <= end).all()
    results = calculate_days([ManualDay(r.day, r.status, r.notified_on, r.half_day_applied,
        r.bereavement_eligible) for r in rows], joined_on=emp.joined_on, left_on=emp.left_on)
    selected = [r for r in results if start <= r.day <= end]
    reviewed = {r.day for r in selected}
    required = [service_start + timedelta(days=i) for i in range(max(0,(service_end-service_start).days+1))]
    missing = [d for d in required if d not in reviewed]
    profile = StaffProfile.query.filter_by(user_id=user_id).one()
    value = money(salary if salary is not None else profile.salary_amount or 0)
    payable = sum((r.payable for r in selected), Decimal(0))
    unpaid = sum((r.unpaid for r in selected), Decimal(0))
    cash_days, cash_value = month_cash_entitlement(month.year,month.month,value,results,
        joined_on=emp.joined_on,left_on=emp.left_on)
    return {'salary': value, 'payable_days': payable, 'unpaid_days': unpaid,
        'gross_pay': money(value*payable/Decimal(end.day)), 'missing': missing,
        'results': selected, 'allowance': service_allowance(month.year,month.month,emp.joined_on,emp.left_on),
        'cash_days': cash_days, 'cash_value': cash_value, 'service_end': service_end,
        'service_start': service_start, 'employment': emp}


def finalize(user_id, actor, month, salary):
    require_admin(actor)
    existing = ManualPayrollMonth.query.filter_by(user_id=user_id,month=month).first()
    if existing:
        return existing
    view = month_preview(user_id,month,salary)
    emp = view['employment']
    if view['service_start'] > view['service_end']:
        raise ValueError('No employment in this month.')
    if today() <= view['service_end']:
        raise ValueError('Finalize after the month or employment has ended, not before.')
    if view['missing']:
        raise ValueError(f"Review all employed days first: {len(view['missing'])} still missing.")
    # Chronological finalization protects annual caps and prevents partial March/exit payouts.
    cursor = max(EFFECTIVE_DATE,emp.joined_on.replace(day=1))
    while cursor < month:
        if not ManualPayrollMonth.query.filter_by(user_id=user_id,month=cursor).first():
            raise ValueError(f'Finalize {cursor:%Y-%m} first (salary snapshot and attendance review required).')
        cursor = (bounds(cursor)[1]+timedelta(days=1))
    row = ManualPayrollMonth(user_id=user_id,month=month,monthly_salary=view['salary'],
        payable_days=view['payable_days'],unpaid_days=view['unpaid_days'],gross_pay=view['gross_pay'],
        asset_charge=0,cash_days=view['cash_days'],cash_value=view['cash_value'],finalized_by=actor.id,
        details_json=json.dumps({'joined_on': emp.joined_on, 'left_on': emp.left_on,
            'allowance': view['allowance'], 'days': [vars(r) for r in view['results']]},default=str))
    db.session.add(row)
    db.session.flush()
    exit_month = emp.left_on and emp.left_on.replace(day=1) == month
    if month.month == 3 or exit_month:
        pending = ManualPayrollMonth.query.filter(ManualPayrollMonth.user_id == user_id,
            ManualPayrollMonth.month <= month, ~ManualPayrollMonth.id.in_(db.select(ManualCashAllocation.month_id))).all()
        forfeited = exit_month and not (emp.management_released or emp.notice_days >= 15)
        settlement = ManualCashSettlement(user_id=user_id,payroll_month_id=row.id,
            reason='forfeited' if forfeited else 'exit' if exit_month else 'march',
            amount=0 if forfeited else sum((r.cash_value for r in pending),Decimal(0)),actor_id=actor.id)
        db.session.add(settlement)
        db.session.flush()
        for source in pending:
            db.session.add(ManualCashAllocation(month_id=source.id,settlement_id=settlement.id))
    # Preserve existing asset-loss deductions, carrying unpaid remainder forward.
    settlement = ManualCashSettlement.query.filter_by(payroll_month_id=row.id).first()
    available = row.gross_pay + (settlement.amount if settlement else 0)
    allocations = ReusableInventoryLossAllocation.query.join(ReusableInventoryLossEvent).filter(
        ReusableInventoryLossAllocation.user_id == user_id,
        ReusableInventoryLossAllocation.settlement_status == 'pending',
        ReusableInventoryLossEvent.loss_date <= view['service_end']).order_by(ReusableInventoryLossEvent.loss_date).all()
    for allocation in allocations:
        reserved = db.session.query(db.func.sum(ManualPayrollAsset.amount)).filter_by(allocation_id=allocation.id).scalar() or 0
        take = min(available,max(Decimal(0),money(allocation.charge_amount)-Decimal(str(reserved))))
        if take > 0:
            db.session.add(ManualPayrollAsset(allocation_id=allocation.id,month_id=row.id,amount=take))
            row.asset_charge += take
            available -= take
    audit(user_id,actor,'finalize',{'month':month,'payroll_id':row.id,'salary':salary})
    db.session.flush()
    return row


def total_due(row):
    settlement = ManualCashSettlement.query.filter_by(payroll_month_id=row.id).first()
    return money(row.gross_pay + (settlement.amount if settlement else 0) - row.asset_charge)


def record_payment(row,actor,paid_on,mode,reference,funding_source):
    require_admin(actor)
    if ManualPayrollPayment.query.filter_by(payroll_month_id=row.id).first():
        return
    if paid_on > today() or paid_on < row.month or mode not in {'cash','card','upi','bank'}:
        raise ValueError('Invalid payment date or payment method.')
    if not reference.strip() or funding_source not in {'cafe_operations','owner_personal'}:
        raise ValueError('Payment reference and funding source are required.')
    amount = total_due(row)
    category = InventoryCategory.query.filter_by(name='Staff Payroll').first()
    if not category:
        category = InventoryCategory(name='Staff Payroll',active=True,icon='users',color='#6cab7a')
        db.session.add(category)
        db.session.flush()
    expense = None
    if amount:
        expense = InventoryExpenseLog(entry_date=paid_on,category_id=category.id,amount=float(amount),
            transaction_mode=mode,funding_source=funding_source,created_by_user_id=actor.id,
            note=f'Manual payroll #{row.id}; staff #{row.user_id}; {row.month:%Y-%m}; {reference[:100]}')
        db.session.add(expense)
        db.session.flush()
    db.session.add(ManualPayrollPayment(payroll_month_id=row.id,paid_on=paid_on,amount=amount,mode=mode,
        reference=reference[:160],expense_id=expense.id if expense else None,actor_id=actor.id))
    audit(row.user_id,actor,'payment',{'payroll_id':row.id,'amount':amount,'mode':mode,'reference':reference[:160]})
    db.session.flush()
    for item in ManualPayrollAsset.query.filter_by(month_id=row.id):
        allocation = db.session.get(ReusableInventoryLossAllocation,item.allocation_id)
        paid = db.session.query(db.func.sum(ManualPayrollAsset.amount)).join(ManualPayrollPayment,
            ManualPayrollPayment.payroll_month_id == ManualPayrollAsset.month_id).filter(
            ManualPayrollAsset.allocation_id == allocation.id).scalar() or 0
        if Decimal(str(paid)) >= money(allocation.charge_amount):
            allocation.settlement_status='deducted'
            allocation.settled_at=datetime.utcnow()
            allocation.settled_by_user_id=actor.id
            allocation.settlement_note='Recovered through recorded manual payroll payment(s).'


def salary_summary(user_id,month):
    row=ManualPayrollMonth.query.filter_by(user_id=user_id,month=month).first()
    if row:
        return {'salary_type':'monthly','salary_amount':float(row.monthly_salary),'days_in_month':bounds(month)[1].day,
            'per_day_salary':float(row.monthly_salary)/bounds(month)[1].day,'payable_days':float(row.payable_days),
            'unpaid_days':float(row.unpaid_days),'estimated_pay':float(total_due(row)),'payment_day':6,'review_required':False}
    try:
        view=month_preview(user_id,month)
        return {'salary_type':'monthly','salary_amount':float(view['salary']),'days_in_month':bounds(month)[1].day,
            'per_day_salary':float(view['salary'])/bounds(month)[1].day,'payable_days':float(view['payable_days']),
            'unpaid_days':float(view['unpaid_days']),'estimated_pay':float(view['gross_pay']),
            'payment_day':6,'review_required':True}
    except ValueError:
        return dict(salary_type='monthly',salary_amount=0,days_in_month=bounds(month)[1].day,
            per_day_salary=0,payable_days=0,unpaid_days=0,estimated_pay=0,payment_day=6,review_required=True)
