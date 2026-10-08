"""Retire conflicting entry points without deleting historical records."""
from datetime import date
from flask import g, request, redirect, url_for, jsonify, abort
from .manual_payroll import active, today, EFFECTIVE_DATE
from .extensions import db


def guard_legacy_attendance():
    if not active():
        return
    endpoint = request.endpoint or ''
    action = request.form.get('action','')
    old_actions = {'attendance_for_user','attendance_status','check_in','check_out',
        'checkout_active_session','checkout_all_active_sessions','leave','leave_request',
        'leave_decision','cancel_leave_request','save_leave_settings','add_holiday','delete_holiday',
        'update_role_leave_rule'}
    mobile_writers = {'mobile_attendance.mobile_check_in','mobile_attendance.mobile_check_out',
        'mobile_attendance.mobile_heartbeat','mobile_staff.create_leave','mobile_staff.cancel_leave'}
    if endpoint in mobile_writers or endpoint in {'main.staff_attendance_check_in','cafe.update_attendance_settings'} or (
        endpoint in {'main.profile','cafe.staff','cafe.my_staff'} and request.method == 'POST' and action in old_actions):
        return jsonify(ok=False, manual_attendance=True,
            error='Attendance is managed manually by an administrator. Automatic attendance and old leave requests are disabled.'),409
    if endpoint == 'cafe.inventory' and action == 'update_reusable_loss_charge':
        from .manual_payroll_models import ManualPayrollAsset
        allocation_id=request.form.get('allocation_id',type=int)
        if allocation_id and ManualPayrollAsset.query.filter_by(allocation_id=allocation_id).first():
            abort(409,'This loss is already included in finalized payroll and is locked.')
    if endpoint == 'main.profile' and action == 'upload_salary_receipt':
        from .manual_payroll_models import ManualPayrollMonth
        from .manual_payroll import total_due, money
        try:
            month=date(int(request.form['salary_year']),int(request.form['salary_month']),1)
            if month >= EFFECTIVE_DATE:
                row=ManualPayrollMonth.query.filter_by(user_id=int(request.form['target_user_id']),month=month).first()
                if not row:
                    abort(409,'Finalize reviewed payroll before uploading its salary receipt.')
                if money(request.form.get('amount')) != total_due(row):
                    abort(409,'Receipt amount must match finalized net payroll.')
        except (ValueError,KeyError):
            abort(400,'Enter a valid salary month and amount.')
    if not getattr(g,'current_user',None) or request.method != 'GET':
        return
    if endpoint == 'cafe.my_staff':
        return redirect(url_for('manual_payroll.workspace', user_id=g.current_user.id))
    sections={'attendance_calendar','attendance_entry','leave_requests','leave_settings','payroll_summary','rulebook'}
    is_staff = endpoint == 'cafe.staff' and request.args.get('section') in sections
    is_profile = endpoint == 'main.profile' and request.args.get('section') in {'attendance','rulebook'}
    is_export = endpoint == 'cafe.export_staff_attendance'
    if is_staff or is_profile or is_export:
        month=request.args.get('payroll_month',type=int) or request.args.get('attendance_month',type=int) or request.args.get('month',type=int) or today().month
        year=request.args.get('payroll_year',type=int) or request.args.get('attendance_year',type=int) or request.args.get('year',type=int) or today().year
        try:
            selected=date(year,month,1)
        except ValueError:
            abort(400)
        if selected < EFFECTIVE_DATE:
            return  # Historical reports stay readable; their writers are disabled.
        return redirect(url_for('manual_payroll.workspace',
            user_id=request.args.get('attendance_user_id',type=int) or request.args.get('user_id',type=int),
            month=selected.strftime('%Y-%m'),export='csv' if is_export else None))
