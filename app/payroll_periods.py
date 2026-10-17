"""Display-only semi-monthly salary estimates; no payment or ledger writes."""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


def salary_periods(year, month, salary, days=()):
    """days contains (calendar date, payable fraction) from the active policy."""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    next_month = end + timedelta(days=1)
    first = Decimal(0)
    second = Decimal(0)
    for day, fraction in days:
        if start <= day <= end:
            if day.day <= 15:
                first += Decimal(str(fraction))
            else:
                second += Decimal(str(fraction))
    rate = Decimal(str(salary or 0)) / Decimal(end.day)
    cents = lambda value: value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    gross = cents((first + second) * rate)
    first_amount = cents(first * rate)
    return dict(start=start, end=end, first_end=date(year, month, 15),
                second_start=date(year, month, 16), first_due=date(year, month, 21),
                second_due=next_month.replace(day=6), gross=gross,
                first=first_amount, second=gross-first_amount)
