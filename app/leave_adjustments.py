"""Explicit, audited corrections to the legacy accumulated earned-leave balance."""
import secrets
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import OperationalError

from .auth_helpers import login_required
from .extensions import db
from .models import User, StaffProfile, LeaveBalance, LeaveTransaction
from .manual_payroll import active
from .leave_logic import run_leave_maintenance, ensure_leave_balance

bp = Blueprint('leave_adjustments', __name__)


def days(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Enter a valid number of days.')
    if not number.is_finite() or number < 0 or number > 10000 or number % Decimal('0.5'):
        raise ValueError('Use 0 to 10,000 days in half-day increments.')
    return number


@bp.route('/cafe/staff/accumulated-leave', methods=['GET', 'POST'])
@login_required
def workspace():
    if not g.current_user.active or not g.current_user.has_role('admin'):
        abort(403)
    if active():
        abort(409, 'The new payroll ledger is active. Legacy balance adjustments are disabled; do not overwrite earned cash settlements.')
    token = session.setdefault('leave_adjustment_csrf', secrets.token_urlsafe(32))
    if request.method == 'POST':
        if not secrets.compare_digest(token, request.form.get('csrf_token', '')):
            abort(400, 'Reload the page before saving.')
        try:
            target_id = int(request.form.get('user_id', ''))
            desired = days(request.form.get('balance', ''))
            expected = Decimal(request.form.get('expected', ''))
            reason = request.form.get('reason', '').strip()
            if not reason or len(reason) > 180:
                raise ValueError('Provide a reason of 1–180 characters.')
            # Catch up scheduled credits before comparison, never erase their history.
            run_leave_maintenance()
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            g.pop('_manual_payroll_active', None)
            if active():
                raise ValueError('Payroll policy changed. Reload before continuing.')
            user = db.session.get(User, target_id)
            if not user or not user.staff_profile:
                abort(404)
            latest = db.session.query(db.func.max(LeaveTransaction.id)).filter_by(user_id=target_id).scalar() or 0
            balance = ensure_leave_balance(user)
            previous = Decimal(str(balance.earned_balance))
            if (not expected.is_finite() or previous != expected
                    or int(request.form.get('revision', '-1')) != latest):
                raise ValueError('The balance changed since you opened this page. Review the latest balance and try again.')
            if previous != desired:
                balance.earned_balance = float(desired)
                db.session.add(LeaveTransaction(user_id=target_id, leave_type='earned',
                    amount=float(desired - previous), transaction_type='admin_adjustment',
                    created_by_user_id=g.current_user.id,
                    note=f'{previous} → {desired} days. {reason}'))
            db.session.commit()
            flash('Accumulated leave saved. Attendance and prior transactions are preserved.', 'success')
        except (ValueError, InvalidOperation) as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        except OperationalError:
            db.session.rollback()
            flash('Another update is in progress. Reload and try again.', 'error')
        return redirect(url_for('.workspace'))
    run_leave_maintenance()
    users = User.query.join(StaffProfile).order_by(User.full_name).all()
    balances = {row.user_id: row for row in LeaveBalance.query.all()}
    revisions = dict(db.session.query(LeaveTransaction.user_id, db.func.max(LeaveTransaction.id)).group_by(LeaveTransaction.user_id).all())
    history = LeaveTransaction.query.filter_by(transaction_type='admin_adjustment').order_by(LeaveTransaction.id.desc()).limit(100).all()
    return render_template('cafe/leave_adjustments.html', users=users, balances=balances,
                           history=history, csrf_token=token, revisions=revisions)
