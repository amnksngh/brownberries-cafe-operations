"""Admin-assisted attendance and leave using the current legacy policy engine."""
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError
from .auth_helpers import login_required
from .extensions import db
from .models import User, StaffProfile, StaffAttendance, StaffLeaveRequest, LeaveBalance, StaffNotification
from .manual_payroll_models import ManualPayrollAudit
from .manual_payroll import active, today
from .leave_logic import validate_leave_request, apply_leave_decision, run_leave_maintenance

bp = Blueprint('staff_proxy', __name__)
STATUSES = {'present_all_day':'Present', 'absent':'Absent (unpaid)',
            'first_half':'First half present', 'second_half':'Second half present'}


def snapshot(user_id):
    def rows(model):
        return [{column.name: str(getattr(row, column.name)) for column in model.__table__.columns}
                for row in model.query.filter_by(user_id=user_id).order_by(model.id).all()]
    return dict(attendance=rows(StaffAttendance), leaves=rows(StaffLeaveRequest), balances=rows(LeaveBalance))


def revision(user_id):
    return hashlib.sha256(json.dumps(snapshot(user_id), sort_keys=True).encode()).hexdigest()


def check_open_sessions(leave):
    if StaffAttendance.query.filter(StaffAttendance.user_id == leave.user_id,
        StaffAttendance.attendance_date >= leave.start_date, StaffAttendance.attendance_date <= leave.end_date,
        StaffAttendance.check_in_at.isnot(None), StaffAttendance.check_out_at.is_(None)).first():
        raise ValueError('An open check-in overlaps this leave. Close it before approving leave.')


@bp.route('/cafe/staff/on-behalf', methods=['GET','POST'])
@login_required
def workspace():
    if not g.current_user.active or not g.current_user.has_role('admin'):
        abort(403)
    if active():
        # Never write the obsolete attendance/balance tables after policy cutover.
        flash('The manual payroll policy is active. Record reviewed attendance and leave there.', 'info')
        return redirect(url_for('manual_payroll.workspace', user_id=request.values.get('user_id', type=int)))
    token = session.setdefault('staff_proxy_csrf', secrets.token_urlsafe(32))
    users = User.query.join(StaffProfile).filter(User.active.is_(True), StaffProfile.archived.is_(False)).order_by(User.full_name).all()
    uid = request.values.get('user_id', type=int) or (users[0].id if users else 0)
    user = db.session.get(User, uid)
    if not user or not user.active or not user.staff_profile or user.staff_profile.archived:
        abort(404)
    if request.method == 'POST':
        if not secrets.compare_digest(token, request.form.get('csrf_token','')):
            abort(400, 'Reload the page before saving.')
        try:
            run_leave_maintenance()
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            g.pop('_manual_payroll_active', None)
            if active():
                raise ValueError('Payroll policy changed. Reload before editing.')
            if request.form.get('revision') != revision(uid):
                raise ValueError('This employee’s records changed. Review the latest information before saving.')
            reason = request.form.get('reason','').strip()
            if not reason or len(reason) > 255:
                raise ValueError('Provide a reason of 1–255 characters.')
            before = snapshot(uid)
            action = request.form.get('action')
            if action == 'register':
                day = date.fromisoformat(request.form.get('day',''))
                if day > today() or (user.staff_profile.joining_date and day < user.staff_profile.joining_date):
                    raise ValueError('Register date must be between joining date and today.')
                check_in = datetime.fromisoformat(request.form.get('check_in',''))
                check_out = datetime.fromisoformat(request.form.get('check_out',''))
                now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
                if check_in.tzinfo or check_out.tzinfo or check_in.date() != day:
                    raise ValueError('Enter local IST times; check-in must be on the attendance date.')
                if check_out <= check_in or check_out-check_in > timedelta(hours=24) or check_out > now_ist:
                    raise ValueError('Check-out must follow check-in, be within 24 hours, and not be in the future.')
                if StaffLeaveRequest.query.filter(StaffLeaveRequest.user_id == uid,
                    StaffLeaveRequest.status == 'approved', StaffLeaveRequest.start_date <= check_out.date(),
                    StaffLeaveRequest.end_date >= day).first():
                    raise ValueError('Approved leave overlaps these times. Review the leave before entering a session.')
                row_id = request.form.get('attendance_id', type=int)
                row = db.session.get(StaffAttendance, row_id) if row_id else None
                if row_id and (not row or row.user_id != uid or row.attendance_date != day):
                    raise ValueError('Choose an attendance entry for this employee and date.')
                others = StaffAttendance.query.filter_by(user_id=uid).all()
                for other in others:
                    if other.id == row_id or not other.check_in_at:
                        continue
                    if other.check_in_at < check_out and (not other.check_out_at or other.check_out_at > check_in):
                        raise ValueError('These times overlap an existing or open session. Edit that entry instead.')
                same_day = [r for r in others if r.attendance_date == day]
                if not row and any(not r.check_in_at for r in same_day):
                    raise ValueError('This date already has a status-only entry. Use its Edit register times form.')
                if not row:
                    from .attendance_logic import aggregate_attendance_rows
                    status = aggregate_attendance_rows(same_day)[0].status if same_day else request.form.get('status')
                    if status not in STATUSES or status == 'absent':
                        raise ValueError('Choose Present or a half-day status; review any existing absence first.')
                    row = StaffAttendance(user_id=uid, attendance_date=day, status=status)
                elif row.status not in STATUSES or row.status == 'absent':
                    raise ValueError('Review the attendance status before adding worked times to this entry.')
                row.check_in_at, row.check_out_at = check_in, check_out
                row.check_in_method = row.check_out_method = 'admin_register'
                row.manager_override = True
                row.notes = reason
                db.session.add(row)
            elif action == 'attendance':
                day = date.fromisoformat(request.form.get('day',''))
                if day > today() or (user.staff_profile.joining_date and day < user.staff_profile.joining_date):
                    raise ValueError('Attendance must be between the joining date and today.')
                status = request.form.get('status')
                if status not in STATUSES:
                    raise ValueError('Choose a valid attendance status.')
                if StaffLeaveRequest.query.filter(StaffLeaveRequest.user_id == uid,
                    StaffLeaveRequest.status == 'approved', StaffLeaveRequest.start_date <= day,
                    StaffLeaveRequest.end_date >= day).first():
                    raise ValueError('Approved leave covers this date. Cancel or correct that leave before overriding attendance.')
                rows = StaffAttendance.query.filter_by(user_id=uid, attendance_date=day).all()
                if len(rows) > 1:
                    raise ValueError('Duplicate attendance exists for this date; review it before editing.')
                row = rows[0] if rows else StaffAttendance(user_id=uid, attendance_date=day)
                if row.check_in_at and not row.check_out_at:
                    raise ValueError('This employee still has an open check-in. Close that session before overriding attendance.')
                row.status = status
                row.manager_override = True
                row.notes = reason
                db.session.add(row)
            elif action == 'leave':
                start, end = date.fromisoformat(request.form.get('start','')), date.fromisoformat(request.form.get('end',''))
                if end < start or (end-start).days > 31:
                    raise ValueError('Choose an ordered date range of no more than 32 days; the current leave policy may allow fewer.')
                if user.staff_profile.joining_date and start < user.staff_profile.joining_date:
                    raise ValueError('Leave cannot start before the employee joined.')
                kind = request.form.get('leave_type','')
                first, last = request.form.get('from_shift',''), request.form.get('to_shift','')
                if StaffLeaveRequest.query.filter(StaffLeaveRequest.user_id == uid,
                    StaffLeaveRequest.status.in_(['pending','approved','requested_changes']),
                    StaffLeaveRequest.start_date <= end, StaffLeaveRequest.end_date >= start).first():
                    raise ValueError('An existing request overlaps these dates. Review it instead of submitting a duplicate.')
                errors, duration = validate_leave_request(user,kind,start,end,first,last)
                if errors:
                    raise ValueError(' '.join(errors))
                if duration <= 0:
                    raise ValueError('Choose a valid start/end half-day range.')
                leave = StaffLeaveRequest(user_id=uid,leave_type=kind,start_date=start,end_date=end,
                    from_shift=first,to_shift=last,duration_days=duration,reason=reason,status='pending')
                db.session.add(leave); db.session.flush()
                if request.form.get('approve_now') == 'yes':
                    check_open_sessions(leave)
                    ok, errors = apply_leave_decision(leave,'approved',g.current_user)
                    if not ok: raise ValueError(' '.join(errors))
            elif action == 'decision':
                leave = db.session.get(StaffLeaveRequest, int(request.form.get('leave_id','0')))
                if not leave or leave.user_id != uid:
                    raise ValueError('Choose a leave request belonging to this employee.')
                decision = request.form.get('decision')
                if decision not in ('approved','rejected','cancelled'):
                    raise ValueError('Choose a valid decision.')
                if leave.status == decision:
                    raise ValueError('This request already has that status.')
                leave.admin_remarks = reason
                if decision == 'approved':
                    check_open_sessions(leave)
                ok, errors = apply_leave_decision(leave,decision,g.current_user)
                if not ok: raise ValueError(' '.join(errors))
            else:
                raise ValueError('Unknown action.')
            db.session.flush()
            after = snapshot(uid)
            # Keep only changed sections/rows in the append-only admin audit.
            changes = {key: {'before':[r for r in before[key] if r not in after[key]],
                             'after':[r for r in after[key] if r not in before[key]]}
                       for key in before if before[key] != after[key]}
            db.session.add(ManualPayrollAudit(user_id=uid,actor_id=g.current_user.id,
                action='staff_proxy_'+action, details_json=json.dumps(dict(reason=reason,changes=changes))))
            db.session.add(StaffNotification(user_id=uid,category='attendance' if action in ('attendance','register') else 'leave',
                message=f'{g.current_user.full_name} recorded {action} on your behalf. Reason: {reason}'))
            db.session.commit()
            flash('Saved on the employee’s behalf. Attendance, leave balances and audit history are synchronized.', 'success')
        except (ValueError, TypeError) as exc:
            db.session.rollback(); flash(str(exc),'error')
        except (IntegrityError, OperationalError):
            db.session.rollback(); flash('Another update is in progress. Reload and review the records.', 'error')
        return redirect(url_for('.workspace',user_id=uid))
    run_leave_maintenance()
    return render_template('cafe/staff_proxy.html',users=users,target=user,token=token,
        revision=revision(uid),statuses=STATUSES,today=today(),
        attendance=StaffAttendance.query.filter_by(user_id=uid).order_by(StaffAttendance.attendance_date.desc()).limit(60).all(),
        leaves=StaffLeaveRequest.query.filter_by(user_id=uid).order_by(StaffLeaveRequest.id.desc()).limit(60).all(),
        audits=ManualPayrollAudit.query.filter(ManualPayrollAudit.user_id==uid,
            ManualPayrollAudit.action.like('staff_proxy_%')).order_by(ManualPayrollAudit.id.desc()).limit(60).all())
