"""Reviewed half-day attendance, exits and separately audited absence penalties."""
import calendar
import hashlib
import json
import secrets
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError
from .extensions import db
from .models import (TimestampMixin, User, StaffProfile, StaffAttendance, StaffLeaveRequest,
    StaffMobileSession, LeaveTransaction, SalaryReceipt)
from .manual_payroll_models import ManualPayrollAudit, ManualPayrollMonth, ManualAttendance, ManualEmployment
from .manual_payroll import today, active, audit
from .auth_helpers import login_required

bp = Blueprint('staff_operations', __name__)
HALVES = {'P':'Present', 'PL':'Applied leave (paid within allowance)', 'SL':'Sick leave',
          'BL':'Eligible bereavement leave', 'IU':'Informed unpaid leave', 'UA':'Unnotified absence'}

class StaffDayEntry(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    day = db.Column(db.Date, nullable=False)
    first_half = db.Column(db.String(2), nullable=False)
    second_half = db.Column(db.String(2), nullable=False)
    notified_on = db.Column(db.Date)
    note = db.Column(db.String(500), nullable=False)
    payable = db.Column(db.Numeric(3,2), nullable=False, default=0)
    paid_leave = db.Column(db.Numeric(3,2), nullable=False, default=0)
    version = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('user_id','day'),)
    __mapper_args__ = {'version_id_col': version}

class StaffDischarge(TimestampMixin, db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    kind = db.Column(db.String(20), nullable=False)
    notice_start = db.Column(db.Date)
    last_working_day = db.Column(db.Date, nullable=False, index=True)
    reason = db.Column(db.String(500), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    completed = db.Column(db.Boolean, nullable=False, default=False)

class StaffAbsencePenalty(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey('staff_day_entry.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    days = db.Column(db.Numeric(5,2), nullable=False)
    pool_days = db.Column(db.Numeric(5,2), nullable=False, default=0)
    monthly_salary = db.Column(db.Numeric(14,2))
    cash_amount = db.Column(db.Numeric(14,2), nullable=False, default=0)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), nullable=False, default='review')
    reason = db.Column(db.String(500))
    deduction_reference = db.Column(db.String(160))
    entry = db.relationship(StaffDayEntry)

class StaffPenaltyLot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    penalty_id = db.Column(db.Integer, db.ForeignKey('staff_absence_penalty.id'), nullable=False)
    month_id = db.Column(db.Integer, db.ForeignKey('manual_payroll_month.id'), nullable=False)
    days = db.Column(db.Numeric(5,2), nullable=False)
    amount = db.Column(db.Numeric(14,2), nullable=False)


def cents(value):
    return Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def token():
    return session.setdefault('staff_ops_csrf', secrets.token_urlsafe(32))


def authorized(admin=False):
    if not g.current_user or not g.current_user.active or not g.current_user.has_any_role(*(('admin',) if admin else ('admin','manager'))):
        abort(403)


def check_token():
    if not secrets.compare_digest(token(), request.form.get('csrf_token','')):abort(400)


def month_revision(uid, month):
    end = month.replace(day=calendar.monthrange(month.year,month.month)[1])
    data=[]
    for model, field in ((StaffDayEntry,StaffDayEntry.day),(StaffAttendance,StaffAttendance.attendance_date),(ManualAttendance,ManualAttendance.day)):
        data.extend([[model.__name__, r.id, str(r.updated_at), str(getattr(r,'version','')), str(getattr(r,'status',''))] for r in model.query.filter(model.user_id==uid,field>=month,field<=end).order_by(model.id)])
    data.extend([['leave',r.id,r.status,str(r.updated_at)] for r in StaffLeaveRequest.query.filter_by(user_id=uid).order_by(StaffLeaveRequest.id)])
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def month_results(uid, month):
    from .manual_attendance_policy import ManualDay, calculate_days
    from .attendance_logic import aggregate_attendance_rows
    start=date(month.year,1,1)
    end=month.replace(day=calendar.monthrange(month.year,month.month)[1])
    entries=StaffDayEntry.query.filter_by(user_id=uid).filter(StaffDayEntry.day>=start,StaffDayEntry.day<=end).all()
    own={e.day:e for e in entries}
    rows=[]
    legacy=StaffAttendance.query.filter_by(user_id=uid).filter(StaffAttendance.attendance_date>=max(start,date(2026,10,1)),StaffAttendance.attendance_date<=end).all()
    mapping={'present_all_day':'P','late_entry':'P','early_exit':'P','first_half':'FH','second_half':'SH',
        'earned_leave':'PL','on_leave':'PL','sick_leave':'SL','half_day_earned_leave':'FH','half_day_leave':'FH'}
    for r in aggregate_attendance_rows(legacy):
        if r.attendance_date in own:continue
        status=mapping.get(r.status,'UL')
        rows.append(ManualDay(r.attendance_date,status,r.attendance_date-timedelta(days=2), status in ('FH','SH')))
    rows.extend(ManualDay(e.day,'P',e.notified_on,True,True,e.first_half,e.second_half) for e in entries)
    profile=StaffProfile.query.filter_by(user_id=uid).one()
    discharge=db.session.get(StaffDischarge,uid)
    return calculate_days(rows,joined_on=profile.joining_date or date(2026,10,1),
        left_on=discharge.last_working_day if discharge else None)


def refresh_legacy(uid,month):
    results={r.day:r for r in month_results(uid,month)}
    for e in StaffDayEntry.query.filter_by(user_id=uid).filter(StaffDayEntry.day>=month,StaffDayEntry.day<=month.replace(day=calendar.monthrange(month.year,month.month)[1])):
        previous_paid=e.paid_leave or Decimal(0)
        e.payable=results[e.day].payable
        e.paid_leave=results[e.day].paid_leave
        difference=e.paid_leave-previous_paid
        if difference:
            from .leave_logic import ensure_leave_balance
            balance=ensure_leave_balance(db.session.get(User,uid))
            balance.earned_balance=float(Decimal(str(balance.earned_balance or 0))-difference)
            db.session.add(LeaveTransaction(user_id=uid,leave_type='earned',amount=float(-difference),transaction_type='calendar_paid',note=f'Reviewed paid leave {e.day}'))
        rows=StaffAttendance.query.filter_by(user_id=uid,attendance_date=e.day).all()
        if len(rows)>1:raise ValueError('Multiple register sessions exist. Review them before bulk attendance edits.')
        r=rows[0] if rows else StaffAttendance(user_id=uid,attendance_date=e.day)
        if r.check_in_at and not r.check_out_at:raise ValueError('Close the open register session before editing attendance.')
        r.status={Decimal('1'):'manual_paid',Decimal('.5'):'manual_half_paid',Decimal('0'):'manual_unpaid'}[e.payable]
        r.manager_override=True
        r.notes=f'First half: {e.first_half}; second half: {e.second_half}. {e.note}'[:255]
        db.session.add(r)


def save_cells(uid,actor,month,cells,notified,note):
    user=db.session.get(User,uid)
    if not user or not user.active or not user.staff_profile or user.staff_profile.archived:raise ValueError('Select an active staff member.')
    if not cells or len(cells)>62 or not note or len(note)>500:raise ValueError('Select up to 62 half-days and provide a note (1–500 characters).')
    if ManualPayrollMonth.query.filter(ManualPayrollMonth.user_id==uid,ManualPayrollMonth.month>=month).first() or SalaryReceipt.query.filter_by(user_id=uid,salary_month=month.month,salary_year=month.year).first():
        raise ValueError('Payroll has been finalized or a salary receipt exists. Attendance is locked for review.')
    changes={}
    for cell in cells:
        day=date.fromisoformat(cell['day']);half=cell['half'];value=cell['status']
        if day.replace(day=1)!=month or day>today() or day<max(date(2026,10,1),user.staff_profile.joining_date or date(2026,10,1)):raise ValueError('Choose past/present dates within employment, from October 2026.')
        discharge=db.session.get(StaffDischarge,uid)
        if discharge and day>discharge.last_working_day:raise ValueError('Attendance is after the last working day.')
        if half not in ('first_half','second_half') or value not in HALVES:raise ValueError('Invalid half-day status.')
        if notified and notified>day:raise ValueError('Notification cannot be after an attendance date.')
        if value in ('PL','SL','IU') and not notified:raise ValueError('Enter when the employee informed management.')
        if StaffLeaveRequest.query.filter_by(user_id=uid,status='approved').filter(StaffLeaveRequest.start_date<=day,StaffLeaveRequest.end_date>=day).first():raise ValueError('Approved leave overlaps this date. Correct/cancel that request before replacing it.')
        changes.setdefault(day,{})[half]=value
    for day,parts in sorted(changes.items()):
        row=StaffDayEntry.query.filter_by(user_id=uid,day=day).first()
        if row and StaffAbsencePenalty.query.filter_by(entry_id=row.id).filter(StaffAbsencePenalty.status.in_(['approved','deducted'])).first():raise ValueError('An approved penalty covers this day. An admin must revoke/reconcile it before attendance is changed.')
        if not row and len(parts)!=2:raise ValueError('For an unreviewed date, select both halves first. Existing attendance is never guessed.')
        old={k:getattr(row,k) for k in ('first_half','second_half','notified_on','note')} if row else None
        if not row:row=StaffDayEntry(user_id=uid,day=day);db.session.add(row)
        for half,value in parts.items():setattr(row,half,value)
        row.notified_on,row.note=notified,note
        db.session.flush()
        for penalty in StaffAbsencePenalty.query.filter_by(entry_id=row.id,status='review'):penalty.status='superseded'
        ua=Decimal('.5')*sum(v=='UA' for v in (row.first_half,row.second_half))
        if ua:db.session.add(StaffAbsencePenalty(entry_id=row.id,user_id=uid,days=ua))
        audit(uid,actor,'calendar_attendance',dict(day=day,before=old,first_half=row.first_half,second_half=row.second_half,notified_on=notified,note=note))
        if active():
            from .manual_payroll import employment
            emp=employment(uid)
            if day<emp.joined_on or (emp.left_on and day>emp.left_on):raise ValueError('Attendance is outside confirmed employment.')
            m=ManualAttendance.query.filter_by(user_id=uid,day=day).first()
            if not m:m=ManualAttendance(user_id=uid,day=day);db.session.add(m)
            m.status='P';m.first_half=row.first_half;m.second_half=row.second_half
            m.notified_on=notified;m.half_day_applied=True;m.bereavement_eligible=True;m.notes=note;m.reviewed_by=actor.id
    db.session.flush()
    if not active():refresh_legacy(uid,month)
    else:
        from .manual_payroll import month_preview
        results={r.day:r for r in month_preview(uid,month)['results']}
        for entry in StaffDayEntry.query.filter_by(user_id=uid).filter(StaffDayEntry.day>=month,StaffDayEntry.day<=month.replace(day=calendar.monthrange(month.year,month.month)[1])):
            entry.payable=results[entry.day].payable;entry.paid_leave=results[entry.day].paid_leave


@bp.route('/cafe/staff/attendance-grid',methods=['GET','POST'])
@login_required
def attendance_grid():
    authorized()
    profiles=StaffProfile.query.join(User).filter(User.active.is_(True),StaffProfile.archived.is_(False)).order_by(User.full_name).all()
    uid=request.values.get('user_id',type=int) or (profiles[0].user_id if profiles else 0)
    if uid not in [p.user_id for p in profiles]:abort(404)
    try:month=date.fromisoformat(request.values.get('month',today().strftime('%Y-%m'))+'-01')
    except ValueError:abort(400)
    if month<date(2026,10,1):abort(400)
    if request.method=='POST':
        check_token()
        try:
            db.session.rollback()
            if db.engine.dialect.name=='sqlite':db.session.execute(db.text('BEGIN IMMEDIATE'))
            if request.form.get('revision')!=month_revision(uid,month):raise ValueError('Records changed. Reload before saving your selection.')
            cells=json.loads(request.form.get('cells','[]'))
            if not isinstance(cells,list):raise ValueError('Invalid selection.')
            save_cells(uid,g.current_user,month,cells,date.fromisoformat(request.form['notified_on']) if request.form.get('notified_on') else None,request.form.get('note','').strip())
            db.session.commit();flash('Selected half-days saved. Salary fractions recalculated; penalties require admin review.','success')
        except (ValueError,KeyError,TypeError,IntegrityError,OperationalError) as exc:
            db.session.rollback();flash(str(exc) if isinstance(exc,ValueError) else 'Unable to save. Reload and review the selection.','error')
        return redirect(url_for('.attendance_grid',user_id=uid,month=month.strftime('%Y-%m')))
    rows={r.day.day:r for r in StaffDayEntry.query.filter_by(user_id=uid).filter(StaffDayEntry.day>=month,StaffDayEntry.day<=month.replace(day=calendar.monthrange(month.year,month.month)[1]))}
    return render_template('cafe/staff_attendance_grid.html',profiles=profiles,uid=uid,month=month,weeks=calendar.monthcalendar(month.year,month.month),rows=rows,statuses=HALVES,csrf=token(),revision=month_revision(uid,month),today=today(),legacy={r.attendance_date.day:r.status for r in StaffAttendance.query.filter_by(user_id=uid).filter(StaffAttendance.attendance_date>=month,StaffAttendance.attendance_date<=month.replace(day=calendar.monthrange(month.year,month.month)[1]))})


def apply_due_discharges():
    due=StaffDischarge.query.filter(StaffDischarge.completed.is_(False),StaffDischarge.last_working_day<today()).all()
    for discharge in due:
        complete_discharge(discharge)
    if due:db.session.commit()


def complete_discharge(row):
    user=db.session.get(User,row.user_id)
    user.active=False
    user.staff_profile.archived=True
    for mobile in StaffMobileSession.query.filter_by(user_id=user.id,active=True):mobile.active=False
    emp=db.session.get(ManualEmployment,user.id)
    if emp:
        emp.left_on=row.last_working_day
        emp.notice_days=15 if row.kind=='planned' else 0
        emp.management_released=False
    row.completed=True
    audit(user.id,db.session.get(User,row.actor_id),'discharge_completed',dict(kind=row.kind,last_working_day=row.last_working_day,reason=row.reason))


@bp.route('/cafe/staff/<int:uid>/discharge',methods=['POST'])
@login_required
def discharge(uid):
    authorized(admin=True);check_token()
    try:
        db.session.rollback()
        if db.engine.dialect.name=='sqlite':db.session.execute(db.text('BEGIN IMMEDIATE'))
        user=db.session.get(User,uid)
        if not user or not user.staff_profile or not user.active:raise ValueError('Choose an active staff member.')
        if user.id==g.current_user.id or user.has_role('admin'):raise ValueError('Admin accounts are protected from this discharge operation.')
        reason=request.form.get('reason','').strip()
        if not reason or len(reason)>500:raise ValueError('Provide the discharge reason (1–500 characters).')
        old=db.session.get(StaffDischarge,uid)
        if request.form.get('kind')=='cancel':
            if not old or old.completed:raise ValueError('No pending notice to cancel.')
            audit(uid,g.current_user,'discharge_cancelled',dict(last_working_day=old.last_working_day,reason=reason))
            db.session.delete(old)
        else:
            kind=request.form.get('kind')
            if kind not in ('planned','abrupt'):raise ValueError('Choose planned notice or abrupt departure.')
            start=date.fromisoformat(request.form['notice_start']) if kind=='planned' else today()
            end=start+timedelta(days=15) if kind=='planned' else today()
            if start>today() or (user.staff_profile.joining_date and start<user.staff_profile.joining_date):raise ValueError('Notice starts between joining date and today.')
            if ManualPayrollMonth.query.filter(ManualPayrollMonth.user_id==uid,ManualPayrollMonth.month>=end.replace(day=1)).first():raise ValueError('Finalized payroll overlaps this departure. Review payroll first.')
            if StaffDayEntry.query.filter(StaffDayEntry.user_id==uid,StaffDayEntry.day>end).first():raise ValueError('Reviewed attendance exists after departure. Review it first.')
            row=old or StaffDischarge(user_id=uid)
            row.kind,row.notice_start,row.last_working_day=kind,start if kind=='planned' else None,end
            row.reason,row.actor_id,row.completed=reason,g.current_user.id,False
            db.session.add(row)
            audit(uid,g.current_user,'discharge_scheduled',dict(kind=kind,notice_start=row.notice_start,last_working_day=end,reason=reason))
            if kind=='abrupt' or end<today():complete_discharge(row)
        db.session.commit();flash('Staff departure saved. History is preserved. Planned access ends after the last working day.','success')
    except (ValueError,KeyError,IntegrityError,OperationalError) as exc:
        db.session.rollback();flash(str(exc) if isinstance(exc,ValueError) else 'Could not save departure. Reload and try again.','error')
    return redirect(url_for('cafe.staff',section='active_staff'))


class StaffPenaltyRecovery(TimestampMixin,db.Model):
    id=db.Column(db.Integer,primary_key=True)
    penalty_id=db.Column(db.Integer,db.ForeignKey('staff_absence_penalty.id'),nullable=False)
    amount=db.Column(db.Numeric(14,2),nullable=False)
    due_date=db.Column(db.Date,nullable=False)
    reference=db.Column(db.String(160),nullable=False)
    actor_id=db.Column(db.Integer,db.ForeignKey('user.id'),nullable=False)

class StaffPayrollClose(TimestampMixin,db.Model):
    id=db.Column(db.Integer,primary_key=True)
    user_id=db.Column(db.Integer,db.ForeignKey('user.id'),nullable=False)
    due_date=db.Column(db.Date,nullable=False)
    reference=db.Column(db.String(160),nullable=False)
    __table_args__=(db.UniqueConstraint('user_id','due_date'),)


def next_instalment(uid,not_before=None):
    cursor=not_before or today()
    for _ in range(800):
        if cursor.day in (6,21):
            source=cursor.replace(day=1) if cursor.day==21 else (cursor.replace(day=1)-timedelta(days=1)).replace(day=1)
            if not StaffPayrollClose.query.filter_by(user_id=uid,due_date=cursor).first() and not SalaryReceipt.query.filter_by(user_id=uid,salary_year=source.year,salary_month=source.month).first():return cursor
        cursor+=timedelta(days=1)
    raise ValueError('No unpaid salary instalment found.')


def recovered(penalty):
    return db.session.query(db.func.sum(StaffPenaltyRecovery.amount)).filter_by(penalty_id=penalty.id).scalar() or Decimal(0)


def approve_penalty(p,actor,salary,reason):
    if p.status!='review':raise ValueError('This penalty is already reviewed.')
    if not salary.is_finite() or salary<0 or salary>10000000:raise ValueError('Confirm a valid monthly salary for the absence month.')
    if p.days!=Decimal('.5')*sum(v=='UA' for v in (p.entry.first_half,p.entry.second_half)):raise ValueError('Attendance changed. Review it first.')
    remaining=p.days
    if active():
        from .manual_payroll_models import ManualCashAllocation
        lots=ManualPayrollMonth.query.filter(ManualPayrollMonth.user_id==p.user_id,~ManualPayrollMonth.id.in_(db.select(ManualCashAllocation.month_id))).order_by(ManualPayrollMonth.month).all()
        for lot in lots:
            used=db.session.query(db.func.sum(StaffPenaltyLot.days)).join(StaffAbsencePenalty).filter(StaffPenaltyLot.month_id==lot.id,StaffAbsencePenalty.status!='revoked').scalar() or Decimal(0)
            take=min(remaining,max(Decimal(0),lot.cash_days-used))
            if take:
                db.session.add(StaffPenaltyLot(penalty_id=p.id,month_id=lot.id,days=take,amount=cents(take*lot.monthly_salary/calendar.monthrange(lot.month.year,lot.month.month)[1])))
                remaining-=take
            if not remaining:break
    else:
        from .leave_logic import ensure_leave_balance
        balance=ensure_leave_balance(db.session.get(User,p.user_id))
        # Current-month credits are not accumulated prior-month leave.
        current_credits=db.session.query(db.func.sum(LeaveTransaction.amount)).filter(LeaveTransaction.user_id==p.user_id,LeaveTransaction.transaction_type=='credit',LeaveTransaction.period_key.like(f'earned:{today():%Y-%m}%')).scalar() or 0
        current_paid=db.session.query(db.func.sum(StaffDayEntry.paid_leave)).filter(StaffDayEntry.user_id==p.user_id,StaffDayEntry.day>=today().replace(day=1),StaffDayEntry.day<=today()).scalar() or Decimal(0)
        current_unused=max(Decimal(0),Decimal(str(current_credits))-current_paid)
        available=max(Decimal(0),Decimal(str(balance.earned_balance or 0))-current_unused)
        take=min(remaining,available)
        if take:
            balance.earned_balance=float(Decimal(str(balance.earned_balance))-take)
            db.session.add(LeaveTransaction(user_id=p.user_id,leave_type='earned',amount=float(-take),transaction_type='absence_penalty',created_by_user_id=actor.id,note=f'Penalty #{p.id}: {reason}'[:255]))
            remaining-=take
    p.pool_days=p.days-remaining;p.monthly_salary=salary
    p.cash_amount=cents(remaining*salary/calendar.monthrange(p.entry.day.year,p.entry.day.month)[1])
    p.due_date=next_instalment(p.user_id) if p.cash_amount else None
    p.status='approved';p.reason=reason
    audit(p.user_id,actor,'absence_penalty_approved',dict(id=p.id,absence_date=p.entry.day,days=p.days,pool_days=p.pool_days,cash=p.cash_amount,salary=salary,due_date=p.due_date,reason=reason))


def penalty_totals(uid,first_due,second_due):
    values={first_due:Decimal(0),second_due:Decimal(0)}
    for p in StaffAbsencePenalty.query.filter_by(user_id=uid,status='approved'):
        if p.due_date in values:values[p.due_date]+=p.cash_amount-recovered(p)
    for due in values:
        actual=db.session.query(db.func.sum(StaffPenaltyRecovery.amount)).join(StaffAbsencePenalty,StaffAbsencePenalty.id==StaffPenaltyRecovery.penalty_id).filter(StaffAbsencePenalty.user_id==uid,StaffPenaltyRecovery.due_date==due).scalar() or Decimal(0)
        values[due]+=actual
    return values[first_due],values[second_due]


@bp.route('/cafe/staff/absence-penalties',methods=['GET','POST'])
@login_required
def penalties():
    authorized()
    if request.method=='POST':
        authorized(admin=True);check_token()
        try:
            from .leave_logic import run_leave_maintenance,ensure_leave_balance
            run_leave_maintenance()
            db.session.rollback()
            if db.engine.dialect.name=='sqlite':db.session.execute(db.text('BEGIN IMMEDIATE'))
            action=request.form.get('action');reason=request.form.get('reason','').strip()
            if not reason or len(reason)>160:raise ValueError('Provide a reason/reference of 1–160 characters.')
            if action=='close_period':
                uid=int(request.form['user_id']);due=date.fromisoformat(request.form['due_date'])
                if due.day not in (6,21) or due>today():raise ValueError('Choose a due date (6th or 21st) on or before today.')
                if StaffPayrollClose.query.filter_by(user_id=uid,due_date=due).first():raise ValueError('This instalment is already closed.')
                # This only records reconciled deductions, not a new cash expense/payment.
                for p in StaffAbsencePenalty.query.filter_by(user_id=uid,status='approved').filter(StaffAbsencePenalty.due_date<=due):
                    balance=p.cash_amount-recovered(p)
                    actual=cents(request.form.get('take_'+str(p.id),'0'))
                    if not actual.is_finite() or (balance>=0 and not Decimal(0)<=actual<=balance) or (balance<0 and not balance<=actual<=Decimal(0)):raise ValueError('Deduction/refund exceeds the outstanding amount.')
                    if actual:db.session.add(StaffPenaltyRecovery(penalty_id=p.id,amount=actual,due_date=due,reference=reason,actor_id=g.current_user.id))
                    if actual!=balance:p.due_date=next_instalment(uid,due+timedelta(days=1))
                    else:p.status='deducted'
                db.session.add(StaffPayrollClose(user_id=uid,due_date=due,reference=reason))
                audit(uid,g.current_user,'salary_instalment_reconciled',dict(due_date=due,reference=reason))
            else:
                p=db.session.get(StaffAbsencePenalty,int(request.form['penalty_id']))
                if not p:raise ValueError('Penalty not found.')
                if action=='approve':approve_penalty(p,g.current_user,cents(request.form['salary']),reason)
                elif action=='revoke':
                    if p.status not in ('review','approved','deducted'):raise ValueError('This penalty cannot be revoked again.')
                    if p.pool_days:
                        if StaffPenaltyLot.query.filter_by(penalty_id=p.id).first():
                            from .manual_payroll_models import ManualCashAllocation
                            if StaffPenaltyLot.query.join(ManualCashAllocation,ManualCashAllocation.month_id==StaffPenaltyLot.month_id).filter(StaffPenaltyLot.penalty_id==p.id).first():raise ValueError('This pool was already settled. A payroll correction is required before revocation.')
                        else:
                            balance=ensure_leave_balance(db.session.get(User,p.user_id));balance.earned_balance=float(Decimal(str(balance.earned_balance or 0))+p.pool_days)
                            db.session.add(LeaveTransaction(user_id=p.user_id,leave_type='earned',amount=float(p.pool_days),transaction_type='penalty_revocation',created_by_user_id=g.current_user.id,note=f'Penalty #{p.id}: {reason}'))
                    paid=recovered(p)
                    if paid:
                        db.session.add(StaffAbsencePenalty(entry_id=p.entry_id,user_id=p.user_id,days=0,pool_days=0,cash_amount=-paid,due_date=next_instalment(p.user_id),status='approved',reason=f'Refund for revoked penalty #{p.id}: {reason}'))
                    p.status='revoked'
                    audit(p.user_id,g.current_user,'absence_penalty_revoked',dict(id=p.id,refund=paid,reason=reason))
                else:raise ValueError('Unknown action.')
            db.session.commit();flash('Penalty ledger updated. No payment or expense was created.','success')
        except (ValueError,KeyError,ArithmeticError,IntegrityError,OperationalError) as exc:
            db.session.rollback();flash(str(exc) if isinstance(exc,ValueError) else 'Unable to save. Reload and check values.','error')
        return redirect(url_for('.penalties'))
    rows=StaffAbsencePenalty.query.order_by(StaffAbsencePenalty.id.desc()).all()
    users={u.id:u for u in User.query.join(StaffProfile).all()}
    periods={}
    for p in rows:
        if p.status=='approved' and p.due_date:periods.setdefault((p.user_id,p.due_date),[]).append(p)
    return render_template('cafe/staff_penalties.html',rows=rows,users=users,periods=periods,recovered=recovered,csrf=token(),admin=g.current_user.has_role('admin'),today=today())


def manual_penalty_plan(row,base):
    from .payroll_periods import salary_periods
    until=salary_periods(row.month.year,row.month.month,0)['second_due']
    penalties=StaffAbsencePenalty.query.filter_by(user_id=row.user_id,status='approved').filter(StaffAbsencePenalty.due_date<=until).order_by(StaffAbsencePenalty.cash_amount,StaffAbsencePenalty.id).all()
    available=Decimal(base);plan=[]
    for p in penalties:
        outstanding=p.cash_amount-recovered(p)
        take=min(available,outstanding) if outstanding>0 else outstanding
        if take:plan.append((p,take));available-=take
    return plan
