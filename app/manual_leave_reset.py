"""Explicit cutover operation; NOT wired to startup or any route yet."""
from .extensions import db
from .models import User, StaffProfile, LeaveBalance, LeaveTransaction, ManualLeaveOpeningReset
from .manual_attendance_policy import EFFECTIVE_DATE


def reset_staff_opening_balances(actor, *, include_admins=False):
    """Caller must commit once alongside policy activation; otherwise rollback.

    All staff profiles (including archived profiles) are included. An admin
    in ANY assigned role is excluded unless explicitly included at cutover.
    Non-staff service accounts are untouched.
    """
    if not actor or not actor.has_role('admin'):
        raise PermissionError('Only an administrator can activate the reset.')
    changed = []
    users = User.query.join(StaffProfile, StaffProfile.user_id == User.id).all()
    for user in users:
        if (user.has_role('admin') and not include_admins) or db.session.get(ManualLeaveOpeningReset, user.id):
            continue
        balance = LeaveBalance.query.filter_by(user_id=user.id).first()
        earned = float(balance.earned_balance or 0) if balance else 0.0
        urgent = float(balance.urgent_balance or 0) if balance else 0.0
        db.session.add(ManualLeaveOpeningReset(user_id=user.id, effective_date=EFFECTIVE_DATE,
            previous_earned=earned, previous_urgent=urgent, actor_user_id=actor.id))
        for kind, previous in (('earned',earned),('urgent',urgent)):
            db.session.add(LeaveTransaction(user_id=user.id, leave_type=kind, amount=-previous,
                transaction_type='manual_policy_opening_reset', period_key=f'manual-opening:2026-10-01:{kind}',
                note='Opening balance reset; previous transactions retained; new accrual begins 2026-10-01.',
                created_by_user_id=actor.id))
        if balance:
            balance.earned_balance = 0
            balance.urgent_balance = 0
        else:
            db.session.add(LeaveBalance(user_id=user.id, earned_balance=0, urgent_balance=0))
        changed.append(user.id)
    db.session.flush()
    return changed
