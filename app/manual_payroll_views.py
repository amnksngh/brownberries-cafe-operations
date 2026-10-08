import csv
import io
import json
import secrets
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for, Response
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from .extensions import db
from .auth_helpers import login_required, user_has_any_role
from .models import User, StaffProfile, StaffAttendance
from .manual_payroll import (active, today, employment, save_employment, save_day,
    month_preview, bounds, finalize, record_payment, total_due, EFFECTIVE_DATE, STATUS_LABELS)
from .manual_payroll_models import (ManualEmployment, ManualAttendance, ManualPayrollMonth,
    ManualCashSettlement, ManualCashAllocation, ManualPayrollPayment, ManualPayrollAudit)

bp = Blueprint('manual_payroll', __name__)


def csrf_token():
    if 'manual_payroll_csrf' not in session:
        session['manual_payroll_csrf'] = secrets.token_urlsafe(32)
    return session['manual_payroll_csrf']


@bp.route('/cafe/manual-payroll', methods=['GET','POST'])
@login_required
def workspace():
    if not g.current_user.active:
        abort(403)
    if not active():
        abort(409, 'Manual payroll has not been activated.')
    manager = user_has_any_role(g.current_user,'admin','manager','owner')
    admin = user_has_any_role(g.current_user,'admin')
    profiles = StaffProfile.query.join(User).order_by(User.full_name).all() if manager else []
    user_id = request.values.get('user_id', type=int) if manager else g.current_user.id
    user_id = user_id or (profiles[0].user_id if profiles else g.current_user.id)
    user = db.session.get(User,user_id)
    if not user or not user.staff_profile:
        abort(404)
    raw_month = request.values.get('month') or today().strftime('%Y-%m')
    try:
        month = date.fromisoformat(raw_month+'-01')
        start,end = bounds(month)
    except ValueError:
        abort(400, 'Choose a valid month from October 2026.')
    if request.method == 'POST':
        if not admin:
            abort(403)
        if not secrets.compare_digest(request.form.get('csrf_token',''),csrf_token()):
            abort(400,'Session expired. Reload and try again.')
        try:
            # Serialize all policy writes including edits vs finalization on SQLite.
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            action=request.form.get('action')
            if action == 'employment':
                save_employment(user_id,g.current_user,date.fromisoformat(request.form['joined_on']),
                    date.fromisoformat(request.form['left_on']) if request.form.get('left_on') else None,
                    int(request.form.get('notice_days','0')),request.form.get('management_released')=='yes',
                    int(request.form.get('version','0')))
            elif action == 'day':
                save_day(user_id,g.current_user,date.fromisoformat(request.form['day']),request.form['status'],
                    date.fromisoformat(request.form['notified_on']) if request.form.get('notified_on') else None,
                    request.form.get('half_applied')=='yes',request.form.get('bereavement')=='yes',
                    request.form.get('notes',''),int(request.form.get('version','0')))
            elif action == 'finalize':
                if request.form.get('confirmed') != 'yes':
                    raise ValueError('Confirm the monthly salary and all reviewed days.')
                finalize(user_id,g.current_user,month,request.form['salary'])
            elif action == 'pay':
                if request.form.get('confirmed') != 'yes':
                    raise ValueError('Confirm that payment has actually been made.')
                row=ManualPayrollMonth.query.filter_by(user_id=user_id,month=month).first()
                if not row:
                    raise ValueError('Finalize payroll before recording payment.')
                record_payment(row,g.current_user,date.fromisoformat(request.form['paid_on']),
                    request.form['mode'],request.form.get('reference',''),request.form.get('funding_source',''))
            else:
                raise ValueError('Unknown action.')
            db.session.commit()
            flash('Saved. Historical records remain preserved.','success')
        except (ValueError, KeyError, PermissionError) as exc:
            db.session.rollback()
            flash(str(exc),'error')
        except (IntegrityError,OperationalError,StaleDataError):
            db.session.rollback()
            flash('The record changed or another update is in progress. Reload and try again.','error')
        return redirect(url_for('manual_payroll.workspace',user_id=user_id,month=raw_month))
    emp=db.session.get(ManualEmployment,user_id)
    error=None
    view=None
    try:
        view=month_preview(user_id,month)
    except ValueError as exc:
        error=str(exc)
    rows=ManualAttendance.query.filter(ManualAttendance.user_id==user_id,
        ManualAttendance.day>=start,ManualAttendance.day<=end).all()
    by_day={r.day:r for r in rows}
    legacy=StaffAttendance.query.filter(StaffAttendance.user_id==user_id,
        StaffAttendance.attendance_date>=start,StaffAttendance.attendance_date<=end).all()
    old={}
    for row in legacy:
        old.setdefault(row.attendance_date,[]).append(row.status)
    results={r.day:r for r in view['results']} if view else {}
    days=[start+timedelta(days=i) for i in range(end.day)]
    final=ManualPayrollMonth.query.filter_by(user_id=user_id,month=month).first()
    locked=ManualPayrollMonth.query.filter(ManualPayrollMonth.user_id==user_id,ManualPayrollMonth.month>=month).first() is not None
    settlement=ManualCashSettlement.query.filter_by(payroll_month_id=final.id).first() if final else None
    payment=ManualPayrollPayment.query.filter_by(payroll_month_id=final.id).first() if final else None
    ledger=ManualPayrollMonth.query.filter_by(user_id=user_id).order_by(ManualPayrollMonth.month).all()
    allocations={a.month_id:a for a in ManualCashAllocation.query.join(ManualPayrollMonth,
        ManualPayrollMonth.id==ManualCashAllocation.month_id).filter(ManualPayrollMonth.user_id==user_id)}
    audits=ManualPayrollAudit.query.filter_by(user_id=user_id).order_by(ManualPayrollAudit.id.desc()).limit(100).all() if admin else []
    if request.args.get('export') == 'csv':
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['Date','Status','Reviewed','Notification date','Worked','Paid PL/SL','Paid BL','Unpaid'])
        for day in days:
            r,p=by_day.get(day),results.get(day)
            writer.writerow([day,r.status if r else '',bool(r),r.notified_on if r else '',
                p.worked if p else '',p.paid_leave if p else '',p.paid_bereavement if p else '',p.unpaid if p else ''])
        writer.writerow([])
        writer.writerow(['Month','Finalized','Monthly salary','Gross pay','Asset deductions','Leave settlement','Net due','Paid on','Payment mode'])
        writer.writerow([month,bool(final),final.monthly_salary if final else '',final.gross_pay if final else '',
            final.asset_charge if final else '',settlement.amount if settlement else '',total_due(final) if final else '',
            payment.paid_on if payment else '',payment.mode if payment else ''])
        return Response(output.getvalue(),mimetype='text/csv',headers={
            'Content-Disposition':f'attachment; filename=payroll-{user_id}-{raw_month}.csv', 'Cache-Control':'no-store'})
    return render_template('cafe/manual_payroll.html',profiles=profiles,user=user,manager=manager,admin=admin,
        month=month,raw_month=raw_month,emp=emp,view=view,error=error,days=days,by_day=by_day,old=old,results=results,
        final=final,locked=locked,settlement=settlement,payment=payment,ledger=ledger,allocations=allocations,
        audits=audits,statuses=STATUS_LABELS,csrf=csrf_token(),today=today(),due=total_due(final) if final else None)
