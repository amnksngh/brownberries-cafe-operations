"""Additive manual payroll records; legacy attendance remains untouched."""
from .extensions import db
from .models import TimestampMixin


class ManualEmployment(TimestampMixin, db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    joined_on = db.Column(db.Date, nullable=False)
    left_on = db.Column(db.Date)
    notice_days = db.Column(db.Integer, nullable=False, default=0)
    management_released = db.Column(db.Boolean, nullable=False, default=False)
    confirmed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    __mapper_args__ = {'version_id_col': version}


class ManualAttendance(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    day = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(2), nullable=False)
    notified_on = db.Column(db.Date)
    half_day_applied = db.Column(db.Boolean, nullable=False, default=False)
    bereavement_eligible = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.String(500))
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('user_id', 'day'),)
    __mapper_args__ = {'version_id_col': version}


class ManualPayrollMonth(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    month = db.Column(db.Date, nullable=False)
    monthly_salary = db.Column(db.Numeric(14, 2), nullable=False)
    payable_days = db.Column(db.Numeric(5, 2), nullable=False)
    unpaid_days = db.Column(db.Numeric(5, 2), nullable=False)
    gross_pay = db.Column(db.Numeric(14, 2), nullable=False)
    asset_charge = db.Column(db.Numeric(14, 2), nullable=False)
    cash_days = db.Column(db.Numeric(5, 2), nullable=False)
    cash_value = db.Column(db.Numeric(14, 2), nullable=False)
    finalized_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    details_json = db.Column(db.Text, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'month'),)


class ManualCashSettlement(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    payroll_month_id = db.Column(db.Integer, db.ForeignKey('manual_payroll_month.id'), nullable=False, unique=True)
    reason = db.Column(db.String(20), nullable=False)  # march / exit / forfeited
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class ManualCashAllocation(db.Model):
    # A month's entitlement can be consumed only once, even under concurrent requests.
    month_id = db.Column(db.Integer, db.ForeignKey('manual_payroll_month.id'), primary_key=True)
    settlement_id = db.Column(db.Integer, db.ForeignKey('manual_cash_settlement.id'), nullable=False)


class ManualPayrollPayment(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_month_id = db.Column(db.Integer, db.ForeignKey('manual_payroll_month.id'), nullable=False, unique=True)
    paid_on = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    mode = db.Column(db.String(20), nullable=False)
    reference = db.Column(db.String(160), nullable=False)
    expense_id = db.Column(db.Integer, db.ForeignKey('inventory_expense_log.id'), unique=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class ManualPayrollAudit(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(60), nullable=False)
    details_json = db.Column(db.Text, nullable=False)


class ManualPayrollAsset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    allocation_id = db.Column(db.Integer, db.ForeignKey('reusable_inventory_loss_allocation.id'), nullable=False)
    month_id = db.Column(db.Integer, db.ForeignKey('manual_payroll_month.id'), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    __table_args__ = (db.UniqueConstraint('allocation_id', 'month_id'),)
