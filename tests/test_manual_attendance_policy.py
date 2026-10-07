import unittest
from datetime import date, timedelta
from decimal import Decimal
from app.manual_attendance_policy import ManualDay, calculate_days, month_cash_entitlement, exit_cash_entitlement


class ManualPolicyTests(unittest.TestCase):
    def test_paid_and_sick_share_monthly_allowance(self):
        rows = calculate_days([ManualDay(date(2026,10,3),'PL',date(2026,10,1)),
                               ManualDay(date(2026,10,4),'SL',date(2026,10,4)),
                               ManualDay(date(2026,10,5),'SL',date(2026,10,5))])
        self.assertEqual([row.payable for row in rows], [1,1,0])
        self.assertEqual(rows[-1].status, 'SL')

    def test_late_notice_and_unnotified_sickness_unpaid(self):
        rows=calculate_days([ManualDay(date(2026,10,3),'PL',date(2026,10,2)),ManualDay(date(2026,10,4),'SL')])
        self.assertEqual(sum(row.payable for row in rows),0)

    def test_half_day_work_plus_eligible_half_day_leave(self):
        rows=calculate_days([ManualDay(date(2026,10,3),'FH',date(2026,10,1),True),ManualDay(date(2026,10,4),'SH')])
        self.assertEqual(rows[0].payable,1)
        self.assertEqual(rows[0].paid_leave,Decimal('.5'))
        self.assertEqual(rows[1].payable,Decimal('.5'))

    def test_bereavement_separate_limit_resets_january(self):
        rows=calculate_days([ManualDay(date(2026,12,1)+timedelta(days=i),'BL',bereavement_eligible=True) for i in range(6)] + [ManualDay(date(2027,1,1),'BL',bereavement_eligible=True)])
        self.assertEqual([row.payable for row in rows],[1,1,1,1,1,0,1])
        self.assertEqual(month_cash_entitlement(2026,12,31000,rows),(2,Decimal('2000.00')))

    def test_unused_days_cash_only_and_source_month_salary(self):
        rows=calculate_days([ManualDay(date(2026,11,3)+timedelta(days=i),'SL',date(2026,11,3)+timedelta(days=i)) for i in range(3)])
        self.assertEqual(rows[-1].payable,0)
        self.assertEqual(month_cash_entitlement(2026,10,31000,rows),(2,Decimal('2000.00')))
        self.assertEqual(month_cash_entitlement(2026,11,60000,rows),(0,Decimal('0.00')))

    def test_exit_conditions(self):
        self.assertEqual(exit_cash_entitlement(1000,notice_days=15),1000)
        self.assertEqual(exit_cash_entitlement(1000,notice_days=14),0)
        self.assertEqual(exit_cash_entitlement(1000,notice_days=0,management_released=True),1000)

    def test_historical_and_duplicate_days_rejected(self):
        with self.assertRaises(ValueError): calculate_days([ManualDay(date(2026,9,30),'P')])
        with self.assertRaises(ValueError): calculate_days([ManualDay(date(2026,10,1),'P')]*2)
