"""Manual policy calculations; deliberately not activated by import/startup.

All calculations retain attendance reason separately from paid/unpaid treatment.
No banked cash entitlement may subsidize a later month's leave.
"""
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

EFFECTIVE_DATE = date(2026, 10, 1)
MONTHLY_ALLOWANCE = Decimal('2')
ANNUAL_BEREAVEMENT_ALLOWANCE = Decimal('5')
STATUS_LABELS = {'P': 'Present', 'PL': 'Paid Leave', 'UL': 'Unpaid Leave',
                 'FH': 'First Half Present', 'SH': 'Second Half Present',
                 'BL': 'Bereavement Leave', 'SL': 'Sick Leave'}


@dataclass(frozen=True)
class ManualDay:
    day: date
    status: str
    notified_on: date | None = None
    half_day_applied: bool = False
    bereavement_eligible: bool = False


@dataclass(frozen=True)
class DayPay:
    day: date
    status: str
    worked: Decimal
    paid_leave: Decimal
    paid_bereavement: Decimal
    unpaid: Decimal

    @property
    def payable(self):
        return self.worked + self.paid_leave + self.paid_bereavement


def service_allowance(year, month, joined_on, left_on=None):
    """Calendar days employed: full month=2, 15+ partial=1, otherwise=0."""
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    if not joined_on or (left_on and left_on < joined_on):
        raise ValueError('Valid employment dates are required.')
    start, end = max(first, joined_on), min(last, left_on or last)
    count = max(0, (end-start).days + 1)
    return Decimal(2 if count == last.day else 1 if count >= 15 else 0)


def calculate_days(days, *, joined_on=None, left_on=None):
    """Evaluate a person's complete policy-period history in date order.

    Callers must supply preceding days in the relevant month/year, not merely
    the visible report range. Duplicate days are rejected rather than double paid.
    """
    monthly_used, annual_used, seen, results = {}, {}, set(), []
    for entry in sorted(days, key=lambda row: row.day):
        if entry.day < EFFECTIVE_DATE:
            raise ValueError('Pre-policy attendance must use the historical calculation.')
        if entry.status not in STATUS_LABELS or entry.day in seen:
            raise ValueError('Invalid status or duplicate attendance date.')
        if entry.notified_on and entry.notified_on > entry.day:
            raise ValueError('Notification cannot be later than the attendance date.')
        seen.add(entry.day)
        month = (entry.day.year, entry.day.month)
        worked = Decimal('1') if entry.status == 'P' else Decimal('.5') if entry.status in {'FH', 'SH'} else Decimal('0')
        missing = Decimal('1') - worked
        notice_ok = entry.notified_on is not None and (entry.day-entry.notified_on).days >= 2
        eligible = ((entry.status == 'PL' and notice_ok)
                    or (entry.status == 'SL' and entry.notified_on is not None)
                    or (entry.status in {'FH', 'SH'} and entry.half_day_applied and notice_ok))
        allowance = service_allowance(*month, joined_on, left_on) if joined_on else MONTHLY_ALLOWANCE
        paid = min(missing, max(Decimal('0'), allowance-monthly_used.get(month, Decimal('0')))) if eligible else Decimal('0')
        bereavement = Decimal('0')
        if entry.status == 'BL' and entry.bereavement_eligible:
            bereavement = min(missing, max(Decimal('0'), ANNUAL_BEREAVEMENT_ALLOWANCE-annual_used.get(entry.day.year, Decimal('0'))))
        monthly_used[month] = monthly_used.get(month, Decimal('0')) + paid
        annual_used[entry.day.year] = annual_used.get(entry.day.year, Decimal('0')) + bereavement
        results.append(DayPay(entry.day, entry.status, worked, paid, bereavement, missing-paid-bereavement))
    return results


def month_cash_entitlement(year, month, monthly_salary, results, *, joined_on=None, left_on=None):
    """Value UNUSED entitlement using that earning month's stored salary.

    Caller must finalize attendance for the entire month before posting this.
    Returns Decimal days/value; rounds money once, not the daily salary rate.
    """
    if date(year, month, 1) < EFFECTIVE_DATE:
        raise ValueError('Accrual starts October 2026.')
    salary = Decimal(str(monthly_salary))
    if not salary.is_finite() or salary < 0:
        raise ValueError('A valid monthly salary snapshot is required.')
    used = sum((row.paid_leave for row in results if (row.day.year, row.day.month) == (year, month)), Decimal('0'))
    allowance = service_allowance(year, month, joined_on, left_on) if joined_on else MONTHLY_ALLOWANCE
    days = max(Decimal('0'), allowance-used)
    value = (days*salary/Decimal(calendar.monthrange(year, month)[1])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return days, value


def exit_cash_entitlement(amount, *, notice_days, management_released=False):
    if notice_days < 0:
        raise ValueError('Notice days cannot be negative.')
    return Decimal(str(amount)) if management_released or notice_days >= 15 else Decimal('0')
