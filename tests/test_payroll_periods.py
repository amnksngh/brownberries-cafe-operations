import calendar
import unittest
from datetime import date
from decimal import Decimal
from app.payroll_periods import salary_periods


class PayrollPeriodsTests(unittest.TestCase):
    def test_all_month_lengths_and_year_rollover(self):
        for year, month in ((2026, 2), (2028, 2), (2026, 4), (2026, 10), (2026, 12)):
            count = calendar.monthrange(year, month)[1]
            result = salary_periods(year, month, count * 1000,
                [(date(year, month, day), 1) for day in range(1, count + 1)])
            self.assertEqual(result['first'], Decimal('15000'))
            self.assertEqual(result['second'], (count - 15) * 1000)
            self.assertEqual(result['gross'], count * 1000)
            self.assertEqual(result['first_due'], date(year, month, 21))
            self.assertEqual(result['second_due'], date(year + (month == 12), month % 12 + 1, 6))

    def test_boundaries_missing_unpaid_half_days_and_rounding(self):
        result = salary_periods(2026, 10, 10000, [(date(2026, 10, 15), 1),
            (date(2026, 10, 16), .5), (date(2026, 10, 31), 0), (date(2026, 11, 1), 1)])
        self.assertEqual(result['first'], Decimal('322.58'))
        self.assertEqual(result['second'], Decimal('161.29'))
        self.assertEqual(result['gross'], result['first'] + result['second'])
        self.assertEqual(salary_periods(2026, 10, 10000)['gross'], 0)
