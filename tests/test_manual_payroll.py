import calendar
import unittest
from datetime import date, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Flask, g
from app.extensions import db
from app.models import (User, StaffProfile, StaffAttendance, LeaveBalance, ManualLeaveOpeningReset,
    InventoryExpenseLog, ReusableInventoryAsset, ReusableInventoryLossEvent, ReusableInventoryLossAllocation,
    ReusableInventoryCount)
from app.manual_attendance_policy import service_allowance, calculate_days, ManualDay
from app.manual_payroll_models import (ManualEmployment, ManualAttendance, ManualPayrollMonth,
    ManualPayrollPayment, ManualCashAllocation, ManualCashSettlement, ManualPayrollAudit)
from app import manual_payroll as payroll
from app.leave_logic import run_leave_maintenance


class ManualPayrollTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.app=Flask(__name__,instance_path=self.temp.name)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',TESTING=True,SECRET_KEY='test')
        db.init_app(self.app)
        self.context=self.app.app_context()
        self.context.push()
        db.create_all()
        self.admin=User(full_name='Admin',email='a@test',password_hash='x',role='admin',active=True)
        self.staff=User(full_name='Staff',email='s@test',password_hash='x',role='staff',active=True)
        self.mixed=User(full_name='Mixed',email='m@test',password_hash='x',role='manager',active=True)
        self.mixed.set_assigned_roles(['manager','admin'])
        db.session.add_all([self.admin,self.staff,self.mixed]);db.session.flush()
        for u in (self.admin,self.staff,self.mixed):
            db.session.add(StaffProfile(user_id=u.id,joining_date=date(2026,9,1),salary_amount=31000))
            db.session.add(LeaveBalance(user_id=u.id,earned_balance=12,urgent_balance=4))
        db.session.commit()
        self.clock=patch('app.manual_payroll.today',return_value=date(2027,5,1));self.clock.start()

    def tearDown(self):
        self.clock.stop();db.session.remove();db.engine.dispose();self.context.pop();self.temp.cleanup()

    def employment(self,join=date(2026,10,1),left=None,notice=0,released=False):
        payroll.save_employment(self.staff.id,self.admin,join,left,notice,released,0)
        db.session.commit()

    def fill(self,month,status='P'):
        emp=payroll.employment(self.staff.id)
        start=max(month,emp.joined_on)
        end=min(payroll.bounds(month)[1],emp.left_on or date.max)
        for n in range((end-start).days+1):
            day=start+timedelta(days=n)
            payroll.save_day(self.staff.id,self.admin,day,status,None,False,False,'reviewed',0)
        db.session.commit()

    def test_allowance_full_partial_14_15_16_and_leap_month(self):
        self.assertEqual(service_allowance(2026,10,date(2026,10,1)),2)
        self.assertEqual(service_allowance(2026,10,date(2026,10,18)),0)
        self.assertEqual(service_allowance(2026,10,date(2026,10,17)),1)
        self.assertEqual(service_allowance(2026,10,date(2026,10,16)),1)
        self.assertEqual(service_allowance(2028,2,date(2028,2,1)),2)
        self.assertEqual(service_allowance(2026,10,date(2026,10,1),date(2026,10,15)),1)

    def test_partial_month_paid_leave_cap(self):
        rows=calculate_days([ManualDay(date(2026,10,19),'SL',date(2026,10,19)),
            ManualDay(date(2026,10,20),'SL',date(2026,10,20))],joined_on=date(2026,10,17))
        self.assertEqual([r.payable for r in rows],[1,0])

    def test_atomic_reset_admin_exclusions_and_legacy_credits_disabled(self):
        changed=payroll.activate(self.admin);db.session.commit()
        self.assertEqual(changed,[self.staff.id])
        self.assertEqual(self.staff.leave_balance.earned_balance,0)
        self.assertEqual(self.admin.leave_balance.earned_balance,12)
        self.assertEqual(self.mixed.leave_balance.earned_balance,12)
        run_leave_maintenance(date(2027,4,30))
        self.assertEqual(self.staff.leave_balance.earned_balance,0)
        self.assertEqual(payroll.activate(self.admin),[])
        self.assertEqual(ManualLeaveOpeningReset.query.count(),1)

    def test_reset_rollback_is_atomic(self):
        payroll.activate(self.admin);db.session.rollback()
        self.assertFalse(payroll.active())
        self.assertEqual(self.staff.leave_balance.earned_balance,12)

    def test_explicit_reset_includes_admin_and_multi_role_admin_staff(self):
        changed=payroll.activate(self.admin,include_admins=True);db.session.commit()
        self.assertEqual(set(changed),{self.admin.id,self.staff.id,self.mixed.id})
        for user in (self.admin,self.staff,self.mixed):
            self.assertEqual(user.leave_balance.earned_balance,0)
            self.assertEqual(user.leave_balance.urgent_balance,0)
            record=db.session.get(ManualLeaveOpeningReset,user.id)
            self.assertEqual(record.previous_earned,12)
            self.assertEqual(record.effective_date,date(2026,10,1))
        self.assertEqual(payroll.activate(self.admin,include_admins=True),[])
        run_leave_maintenance(date(2027,4,30))
        self.assertEqual(self.admin.leave_balance.earned_balance,0)

    def test_no_legacy_auto_conversion_or_finalization_without_review(self):
        self.employment()
        db.session.add(StaffAttendance(user_id=self.staff.id,attendance_date=date(2026,10,1),status='present_all_day'))
        db.session.commit()
        self.assertEqual(len(payroll.month_preview(self.staff.id,date(2026,10,1))['missing']),31)
        with self.assertRaisesRegex(ValueError,'Review all'):
            payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000)

    def test_day_edit_version_and_history(self):
        self.employment()
        payroll.save_day(self.staff.id,self.admin,date(2026,10,1),'P',None,False,False,'first',0)
        db.session.commit()
        with self.assertRaisesRegex(ValueError,'elsewhere'):
            payroll.save_day(self.staff.id,self.admin,date(2026,10,1),'UL',None,False,False,'stale',0)
        payroll.save_day(self.staff.id,self.admin,date(2026,10,1),'UL',None,False,False,'corrected',1)
        db.session.commit()
        self.assertEqual(ManualAttendance.query.one().version,2)
        self.assertEqual(ManualPayrollAudit.query.filter_by(action='attendance_review').count(),2)

    def test_chronological_finalization_and_immutable_salary_snapshot(self):
        self.employment();self.fill(date(2026,11,1))
        with self.assertRaisesRegex(ValueError,'2026-10'):
            payroll.finalize(self.staff.id,self.admin,date(2026,11,1),60000)
        self.fill(date(2026,10,1))
        row=payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000);db.session.commit()
        self.staff.staff_profile.salary_amount=60000;db.session.commit()
        self.assertEqual(row.cash_value,2000)
        with self.assertRaisesRegex(ValueError,'locked'):
            payroll.save_day(self.staff.id,self.admin,date(2026,10,1),'UL',None,False,False,'',1)
        november=payroll.finalize(self.staff.id,self.admin,date(2026,11,1),60000);db.session.commit()
        self.assertEqual(november.cash_value,4000)
        self.assertEqual(ManualCashSettlement.query.count(),0)

    def test_march_cash_settlement_and_payment_idempotency(self):
        self.employment()
        for year,month in [(2026,10),(2026,11),(2026,12),(2027,1),(2027,2),(2027,3)]:
            first=date(year,month,1);self.fill(first)
            row=payroll.finalize(self.staff.id,self.admin,first,calendar.monthrange(year,month)[1]*1000)
            db.session.commit()
        self.assertEqual(ManualCashSettlement.query.one().amount,12000)
        self.assertEqual(payroll.total_due(row),43000)
        self.assertEqual(ManualCashAllocation.query.count(),6)
        payroll.record_payment(row,self.admin,date(2027,4,6),'upi','salary-march','cafe_operations')
        db.session.commit()
        payroll.record_payment(row,self.admin,date(2027,4,6),'upi','salary-march','cafe_operations')
        db.session.commit()
        self.assertEqual(ManualPayrollPayment.query.count(),1)
        self.assertEqual(InventoryExpenseLog.query.one().amount,43000)
        self.assertEqual(InventoryExpenseLog.query.one().transaction_mode,'upi')

    def test_exit_notice_and_forfeiture(self):
        self.employment(left=date(2026,10,15),notice=14)
        self.fill(date(2026,10,1));row=payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000)
        db.session.commit()
        self.assertEqual(row.cash_days,1)
        self.assertEqual(ManualCashSettlement.query.one().reason,'forfeited')
        self.assertEqual(payroll.total_due(row),15000)

    def test_exit_15_days_notice_paid(self):
        self.employment(left=date(2026,10,15),notice=15)
        self.fill(date(2026,10,1));row=payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000)
        db.session.commit()
        self.assertEqual(ManualCashSettlement.query.one().amount,1000)
        self.assertEqual(payroll.total_due(row),16000)

    def test_exit_management_released_paid(self):
        self.employment(left=date(2026,10,15),released=True)
        self.fill(date(2026,10,1));payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000)
        self.assertEqual(ManualCashSettlement.query.one().amount,1000)

    def test_admin_required(self):
        with self.assertRaises(PermissionError): payroll.activate(self.staff)
        with self.assertRaises(PermissionError): payroll.save_employment(self.staff.id,self.staff,date(2026,10,1),None,0,False,0)

    def test_cannot_finalize_open_month_or_invalid_amount(self):
        self.employment();self.fill(date(2026,10,1))
        with patch('app.manual_payroll.today',return_value=date(2026,10,31)):
            with self.assertRaisesRegex(ValueError,'ended'):
                payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000)
        for amount in ('NaN','Infinity','-1','not-money'):
            with self.assertRaises(ValueError): payroll.money(amount)

    def test_backdated_review_recomputes_cap(self):
        self.employment()
        for day in (5,6,7):
            payroll.save_day(self.staff.id,self.admin,date(2026,10,day),'SL',date(2026,10,day),False,False,'',0)
        db.session.commit()
        results=payroll.month_preview(self.staff.id,date(2026,10,1))['results']
        self.assertEqual([r.payable for r in results],[1,1,0])
        payroll.save_day(self.staff.id,self.admin,date(2026,10,4),'PL',date(2026,10,1),False,False,'',0)
        db.session.commit()
        results=payroll.month_preview(self.staff.id,date(2026,10,1))['results']
        self.assertEqual([r.payable for r in results],[1,1,0,0])

    def test_asset_deductions_are_reserved_once_and_carried_forward(self):
        self.employment();self.fill(date(2026,10,1))
        asset=ReusableInventoryAsset(name='Cups')
        db.session.add(asset);db.session.flush()
        count=ReusableInventoryCount(asset_id=asset.id)
        db.session.add(count);db.session.flush()
        event=ReusableInventoryLossEvent(asset_id=asset.id,count_id=count.id,loss_type='reported',loss_date=date(2026,10,3))
        db.session.add(event);db.session.flush()
        allocation=ReusableInventoryLossAllocation(loss_event_id=event.id,user_id=self.staff.id,charge_amount=40000)
        db.session.add(allocation);db.session.commit()
        october=payroll.finalize(self.staff.id,self.admin,date(2026,10,1),31000);db.session.commit()
        self.assertEqual(october.asset_charge,31000)
        self.assertEqual(payroll.total_due(october),0)
        payroll.record_payment(october,self.admin,date(2026,11,6),'cash','fully offset','cafe_operations');db.session.commit()
        self.assertEqual(allocation.settlement_status,'pending')
        self.fill(date(2026,11,1))
        november=payroll.finalize(self.staff.id,self.admin,date(2026,11,1),30000);db.session.commit()
        self.assertEqual(november.asset_charge,9000)
        payroll.record_payment(november,self.admin,date(2026,12,6),'upi','november','cafe_operations');db.session.commit()
        self.assertEqual(allocation.settlement_status,'deducted')
        self.assertEqual(InventoryExpenseLog.query.one().amount,21000)

    def test_new_year_resets_bereavement_and_does_not_spend_cash_bank(self):
        self.employment()
        for month in (10,11):
            self.fill(date(2026,month,1));payroll.finalize(self.staff.id,self.admin,date(2026,month,1),30000);db.session.commit()
        self.fill(date(2026,12,1))
        for day in range(1,7):
            payroll.save_day(self.staff.id,self.admin,date(2026,12,day),'BL',None,False,True,'',1)
        db.session.commit()
        december=payroll.finalize(self.staff.id,self.admin,date(2026,12,1),31000);db.session.commit()
        self.assertEqual(december.unpaid_days,1)
        payroll.save_day(self.staff.id,self.admin,date(2027,1,1),'BL',None,False,True,'',0)
        db.session.commit()
        self.assertEqual(payroll.month_preview(self.staff.id,date(2027,1,1))['results'][0].payable,1)


class ManualPayrollRouteTests(unittest.TestCase):
    def test_admin_form_roundtrip_permissions_and_mobile_workspace(self):
        from app import create_app
        from app.models import StaffMobileSession
        from app.mobile_attendance import _token_hash
        with TemporaryDirectory() as folder:
            app=create_app(instance_path=folder,initialize_legacy_leaves=False)
            app.config.update(TESTING=True,SECRET_KEY='test')
            with app.app_context():
                admin=User(full_name='Admin',email='admin@test',role='admin',active=True,password_hash='x')
                staff=User(full_name='Worker',email='worker@test',role='staff',active=True,password_hash='x')
                db.session.add_all([admin,staff]);db.session.flush()
                aid,sid=admin.id,staff.id
                db.session.add_all([StaffProfile(user_id=aid),StaffProfile(user_id=sid,salary_amount=31000)])
                db.session.add(StaffMobileSession(user_id=sid,device_id='fixture',token_hash=_token_hash('fixture-token'),active=True))
                db.session.commit();payroll.activate(admin);db.session.commit()
            try:
                client=app.test_client()
                with client.session_transaction() as s:s['user_id']=aid
                url=f'/cafe/manual-payroll?user_id={sid}&month=2026-10'
                self.assertEqual(client.get(url).status_code,200)
                with client.session_transaction() as s:token=s['manual_payroll_csrf']
                self.assertEqual(client.post(url,data={'action':'employment'}).status_code,400)
                self.assertEqual(client.post(url,data={'action':'employment','csrf_token':token,'version':0,
                    'joined_on':'2026-10-01','notice_days':0},follow_redirects=True).status_code,200)
                result=client.post(url,data={'action':'day','csrf_token':token,'version':0,'day':'2026-10-01',
                    'status':'P','notes':'Reviewed legacy record'},follow_redirects=True)
                self.assertIn(b'Saved.',result.data)
                with app.app_context():
                    self.assertEqual(ManualAttendance.query.one().status,'P')
                self.assertEqual(client.post('/cafe/my-staff',data={'action':'check_in'}).status_code,409)
                self.assertEqual(client.post('/cafe/staff',data={'action':'leave_decision'}).status_code,409)
                with client.session_transaction() as s:s['user_id']=sid
                self.assertEqual(client.post(url,data={'action':'day','csrf_token':token}).status_code,403)
                own=client.get(f'/cafe/manual-payroll?user_id={aid}&month=2026-10')
                self.assertIn(b'Worker',own.data)
                self.assertNotIn(b'Confirm employment / exit',own.data)
                mobile=client.get('/api/mobile/staff/workspace',headers={'Authorization':'Bearer fixture-token'})
                self.assertEqual(mobile.status_code,200)
                self.assertTrue(mobile.json['manual_attendance'])
                self.assertEqual(mobile.json['attendance']['history'][0]['status'],'P')
                bootstrap=client.get('/api/mobile/attendance/bootstrap',headers={'Authorization':'Bearer fixture-token'})
                self.assertEqual(bootstrap.status_code,200)
                self.assertTrue(bootstrap.json['manual_attendance'])
                self.assertIsNone(bootstrap.json['active_session'])
            finally:
                with app.app_context():db.session.remove();db.engine.dispose()
