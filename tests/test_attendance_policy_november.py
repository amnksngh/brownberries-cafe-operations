import unittest
from datetime import date, timedelta
from decimal import Decimal as D

from app.manual_attendance_policy import ManualDay
from app.attendance_policy_november import (Probation, PoolLot, sales_allowance,
    cumulative_cap, employee_allowance, calculate_reviewed_days, pending_date_split,
    penalty_plan, previous_month, unused_cash)


class NovemberPolicyTests(unittest.TestCase):
    def test_sales_boundaries(self):
        for sales, expected in [(-1, 2), (0, 2), ('799999.99', 2), (800000, 3),
                                ('1199999.99', 3), (1200000, 4)]:
            self.assertEqual(sales_allowance(sales), expected)
        for invalid in [None, 'NaN', 'Infinity', 'unknown']:
            with self.assertRaises(ValueError):
                sales_allowance(invalid)

    def test_calendar_caps(self):
        for allowance, expected in [(2, [1]*15+[2]*16), (3, [1]*10+[2]*10+[3]*11),
                                    (4, [1]*7+[2]*7+[3]*7+[4]*10)]:
            self.assertEqual([cumulative_cap(date(2026, 12, d), allowance) for d in range(1, 32)], expected)

    def test_sickness_cannot_borrow_later_days(self):
        entries = [ManualDay(date(2026, 11, d), 'SL', date(2026, 11, d)) for d in [1, 2, 16, 17]]
        result = calculate_reviewed_days(entries, {date(2026, 10, 1): 0}, joined_on=date(2025, 1, 1))
        self.assertEqual([r.paid_leave for r in result], [1, 0, 1, 0])
        self.assertEqual([r.status for r in result], ['SL']*4)

    def test_half_days_share_allowance_with_sick(self):
        entries = [ManualDay(date(2026, 11, 3), 'FH', date(2026, 11, 1), True),
                   ManualDay(date(2026, 11, 4), 'SL', date(2026, 11, 4))]
        result = calculate_reviewed_days(entries, {date(2026, 10, 1): 0}, joined_on=date(2025, 1, 1))
        self.assertEqual([r.paid_leave for r in result], [D('.5'), D('.5')])

    def test_missing_sales_fails_closed_and_prior_month(self):
        self.assertEqual(previous_month(date(2027, 1, 1)), date(2026, 12, 1))
        with self.assertRaises(ValueError):
            calculate_reviewed_days([ManualDay(date(2026, 11, 1), 'P')], {}, joined_on=date(2025, 1, 1))

    def test_october_unchanged(self):
        entries = [ManualDay(date(2026, 10, d), 'SL', date(2026, 10, d)) for d in [1, 2, 3]]
        self.assertEqual([r.paid_leave for r in calculate_reviewed_days(entries, {}, joined_on=date(2025, 1, 1))], [1, 1, 0])

    def test_probation_new_hires_only_and_manual_confirmation(self):
        self.assertFalse(Probation(date(2026, 10, 31)).applies)
        probation = Probation(date(2026, 11, 1))
        self.assertEqual(probation.last_day, date(2027, 1, 29))
        self.assertIsNone(probation.allowance_starts)
        self.assertEqual(Probation(date(2026, 11, 1), date(2027, 1, 29)).allowance_starts, date(2027, 2, 1))
        with self.assertRaises(ValueError):
            _ = Probation(date(2026, 11, 1), date(2027, 1, 28)).allowance_starts
        with self.assertRaises(ValueError):
            _ = Probation(date(2026, 11, 1), date(2027, 1, 29), date(2027, 2, 15)).allowance_starts

    def test_probation_and_existing_partial_month(self):
        self.assertEqual(employee_allowance(date(2027, 2, 1), 1200000, joined_on=date(2026, 11, 1)), 0)
        self.assertEqual(employee_allowance(date(2027, 2, 1), 1200000, joined_on=date(2026, 11, 1), confirmed_on=date(2027, 1, 29)), 4)
        for last, expected in [(14, 0), (15, 1), (30, 4)]:
            self.assertEqual(employee_allowance(date(2026, 11, 1), 1200000, joined_on=date(2025, 1, 1), left_on=date(2026, 11, last)), expected)

    def test_unused_cash_uses_actual_allowance_and_earning_salary(self):
        self.assertEqual(unused_cash(date(2026, 11, 1), 30000, [], 1200000,
                                     joined_on=date(2025, 1, 1)), (4, D('4000.00')))
        self.assertEqual(unused_cash(date(2026, 12, 1), 62000, [], 0,
                                     joined_on=date(2025, 1, 1)), (2, D('4000.00')))
        self.assertEqual(unused_cash(date(2026, 11, 1), 30000, [], 1200000,
                                     joined_on=date(2026, 11, 1)), (0, D('0.00')))

    def test_bereavement_separate_during_probation(self):
        entries = [ManualDay(date(2026, 11, 1)+timedelta(days=i), 'BL', bereavement_eligible=True) for i in range(6)]
        result = calculate_reviewed_days(entries, {date(2026, 10, 1): 0}, joined_on=date(2026, 11, 1))
        self.assertEqual([r.paid_bereavement for r in result], [1, 1, 1, 1, 1, 0])

    def test_partial_blackout_only_pending_ordinary(self):
        first, last, blocked = date(2026, 11, 1), date(2026, 11, 3), date(2026, 11, 2)
        retained, cancelled = pending_date_split(first, last, [blocked], status='pending', leave_kind='PL')
        self.assertEqual(retained, (first, last))
        self.assertEqual(cancelled, (blocked,))
        for status, kind in [('approved', 'PL'), ('pending', 'SL'), ('pending', 'BL')]:
            self.assertEqual(pending_date_split(first, last, [blocked], status=status, leave_kind=kind)[1], ())

    def test_fifo_skips_settled_and_retains_debt(self):
        lots = [PoolLot(2, date(2026, 12, 1), D('1'), D('31000')),
                PoolLot(1, date(2026, 11, 1), D('.5'), D('15000')),
                PoolLot(0, date(2026, 10, 1), D('2'), D('31000'), True)]
        self.assertEqual(penalty_plan(lots, 2), (((1, D('.5')), (2, D('1'))), D('.5')))
        self.assertEqual(lots[1].days, D('.5'))
        self.assertEqual(penalty_plan([], '.5'), ((), D('.5')))
        for invalid in [0, -1, '.25', 'NaN']:
            with self.assertRaises(ValueError): penalty_plan(lots, invalid)
