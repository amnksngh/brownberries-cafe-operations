"""November 2026 policy primitives, not an activation or balance-reset script.

Sales inputs must be completed prior-month net sales, not receipts including tax.
Database adapters must snapshot those inputs and enforce authorization/auditing.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .manual_attendance_policy import calculate_days, month_cash_entitlement, service_allowance

EFFECTIVE_DATE = date(2026, 11, 1)
ZERO = Decimal('0')


def sales_allowance(net_sales):
    try:
        value = Decimal(str(net_sales))
    except InvalidOperation as exc:
        raise ValueError('Completed net sales are required.') from exc
    if not value.is_finite():
        raise ValueError('Completed net sales must be finite.')
    # A refund-heavy negative month still belongs to the lowest sales band.
    return Decimal(4 if value >= 1200000 else 3 if value >= 800000 else 2)


def previous_month(month):
    if month.day != 1:
        raise ValueError('Use the first day of the payroll month.')
    return (month - timedelta(days=1)).replace(day=1)


def cumulative_cap(day, allowance):
    """Maximum total paid ordinary/sick leave usable by this calendar date."""
    if allowance not in (0, 1, 2, 3, 4):
        raise ValueError('Invalid monthly allowance.')
    interval = {2: 15, 3: 10, 4: 7}.get(allowance)
    return min(Decimal(allowance), Decimal((day.day - 1) // interval + 1)) if interval else Decimal(allowance)


@dataclass(frozen=True)
class Probation:
    joined_on: date
    confirmed_on: date | None = None
    extended_until: date | None = None

    @property
    def applies(self):
        return self.joined_on >= EFFECTIVE_DATE

    @property
    def last_day(self):
        initial = self.joined_on + timedelta(days=89)
        if self.extended_until and self.extended_until < initial:
            raise ValueError('An extension cannot shorten the 90-day probation.')
        return max(initial, self.extended_until or initial)

    @property
    def allowance_starts(self):
        if not self.applies:
            return EFFECTIVE_DATE
        if not self.confirmed_on:
            return None  # Time alone does not confirm an employee.
        if self.confirmed_on < self.last_day:
            raise ValueError('Confirmation cannot precede completion of probation.')
        end = self.confirmed_on.replace(day=calendar.monthrange(self.confirmed_on.year, self.confirmed_on.month)[1])
        return end + timedelta(days=1)


def employee_allowance(month, net_sales, *, joined_on, left_on=None,
                       confirmed_on=None, extended_until=None):
    previous_month(month)  # Validate month anchor.
    employed = service_allowance(month.year, month.month, joined_on, left_on)
    if month < EFFECTIVE_DATE:
        return service_allowance(month.year, month.month, joined_on, left_on)
    probation = Probation(joined_on, confirmed_on, extended_until)
    starts = probation.allowance_starts
    if starts is None or month < starts:
        return ZERO
    return sales_allowance(net_sales) if employed == 2 else employed


def unused_cash(month, monthly_salary, results, net_sales, *, joined_on,
                left_on=None, confirmed_on=None, extended_until=None):
    allowance = employee_allowance(month, net_sales, joined_on=joined_on,
        left_on=left_on, confirmed_on=confirmed_on, extended_until=extended_until)
    return month_cash_entitlement(month.year, month.month, monthly_salary, results,
        joined_on=joined_on, left_on=left_on, monthly_allowance=allowance)


def calculate_reviewed_days(entries, sales_by_month, *, joined_on, left_on=None,
                            confirmed_on=None, extended_until=None):
    """Explicit v2 entry point. Missing prior-month sales never silently become zero.

    sales_by_month keys are source sales months (first-of-month dates). The caller
    must supply the entire relevant attendance year to enforce bereavement limits.
    Blackout/overlap approval checks are separate from this payroll calculation.
    """
    def cap(day):
        month = day.replace(day=1)
        if month < EFFECTIVE_DATE:
            return service_allowance(day.year, day.month, joined_on, left_on)
        source = previous_month(month)
        if source not in sales_by_month:
            raise ValueError(f'Completed sales snapshot required for {source:%Y-%m}.')
        allowance = employee_allowance(month, sales_by_month[source], joined_on=joined_on,
            left_on=left_on, confirmed_on=confirmed_on, extended_until=extended_until)
        return cumulative_cap(day, allowance)

    return calculate_days(entries, joined_on=joined_on, left_on=left_on, allowance_for_day=cap)


def pending_date_split(start, end, blocked_dates, *, status, leave_kind):
    """Plan per-date cancellation without rewriting the original request.

    The caller must retain request history and persist cancelled dates atomically
    with the new blackout. Approved, sick and bereavement leave is untouched.
    """
    if end < start:
        raise ValueError('Leave end precedes start.')
    dates = tuple(start + timedelta(days=i) for i in range((end-start).days + 1))
    cancel = status in {'pending', 'requested_changes'} and leave_kind not in {'SL', 'BL'}
    blocked = set(blocked_dates) if cancel else set()
    return tuple(d for d in dates if d not in blocked), tuple(d for d in dates if d in blocked)


@dataclass(frozen=True)
class PoolLot:
    source_id: int
    month: date
    days: Decimal
    monthly_salary: Decimal
    settled: bool = False


def penalty_plan(lots, days):
    """FIFO reservation plan; never edits a lot or a previously paid settlement.

    Uncovered days are debt against future accrual, not a negative cash payment.
    Persist reservations and revocations as audited events, not overwritten lots.
    """
    days = Decimal(str(days))
    if not days.is_finite() or days <= 0 or days % Decimal('.5'):
        raise ValueError('Penalty must be positive in half-day increments.')
    remaining, allocations, seen = days, [], set()
    for lot in sorted(lots, key=lambda row: (row.month, row.source_id)):
        if lot.source_id in seen:
            raise ValueError('Duplicate source lot.')
        seen.add(lot.source_id)
        if not lot.days.is_finite() or lot.days < 0 or not lot.monthly_salary.is_finite() or lot.monthly_salary < 0:
            raise ValueError('Invalid source lot.')
        if lot.settled or not remaining:
            continue
        take = min(remaining, lot.days)
        if take:
            allocations.append((lot.source_id, take))
            remaining -= take
    return tuple(allocations), remaining
