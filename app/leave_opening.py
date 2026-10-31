"""Explicit, non-destructive legacy balance capture for the November cutover.

No startup hook: call inside the reviewed cutover transaction after backup.
The snapshot is not spendable leave and has no guessed cash value.
"""
from datetime import date
from decimal import Decimal

from .extensions import db
from .models import LeaveBalance, LeaveTransaction, TimestampMixin


class LeaveOpeningSnapshot(TimestampMixin, db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    effective_date = db.Column(db.Date, nullable=False)
    earned_days = db.Column(db.Numeric(12, 4), nullable=False)
    urgent_days = db.Column(db.Numeric(12, 4), nullable=False)
    source_transaction_id = db.Column(db.Integer, nullable=True)
    captured_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    valuation_status = db.Column(db.String(30), nullable=False, default='pending_admin_review')
    cash_value = db.Column(db.Numeric(14, 2), nullable=True)


def capture_opening(actor):
    """Caller owns locking/commit. Repeated calls never overwrite a snapshot.

    Capture every existing account, including inactive/admin accounts, without
    changing legacy balances, transactions, attendance or paid settlements.
    """
    if not actor or not actor.active or not actor.has_role('admin'):
        raise PermissionError('An active admin must capture opening balances.')
    captured = []
    for balance in LeaveBalance.query.order_by(LeaveBalance.user_id).all():
        if db.session.get(LeaveOpeningSnapshot, balance.user_id):
            continue
        earned = Decimal(str(balance.earned_balance or 0))
        urgent = Decimal(str(balance.urgent_balance or 0))
        if not earned.is_finite() or not urgent.is_finite():
            raise ValueError('Non-finite legacy balance requires review before cutover.')
        latest = db.session.query(db.func.max(LeaveTransaction.id)).filter_by(user_id=balance.user_id).scalar()
        db.session.add(LeaveOpeningSnapshot(user_id=balance.user_id,
            effective_date=date(2026, 11, 1), earned_days=earned, urgent_days=urgent,
            source_transaction_id=latest, captured_by=actor.id))
        captured.append(balance.user_id)
    db.session.flush()
    return captured
