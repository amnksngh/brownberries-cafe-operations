import json
import unittest
from datetime import date,timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch
from werkzeug.security import check_password_hash
from app import create_app
from app.extensions import db
from app.models import User,StaffProfile,StaffAttendance,StaffLeaveRequest,LeaveBalance,SalaryReceipt
from app.staff_operations import StaffDayEntry,StaffAbsencePenalty,StaffDischarge,StaffPenaltyRecovery,month_revision,save_cells,approve_penalty,apply_due_discharges,penalty_totals
from app.manual_attendance_policy import ManualDay,calculate_days

class StaffOperationsTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.app=create_app(instance_path=self.temp.name,initialize_legacy_leaves=False);self.app.config['TESTING']=True
  with self.app.app_context():
   ids=[]
   for role in ('admin','manager','staff'):
    u=User(full_name=role,email=role+'@test.local',password_hash='x',role=role,active=True);db.session.add(u);db.session.flush();ids.append(u.id)
    db.session.add(StaffProfile(user_id=u.id,joining_date=date(2026,10,1),salary_type='monthly',salary_amount=31000))
    db.session.add(LeaveBalance(user_id=u.id,earned_balance=1,urgent_balance=0))
   db.session.commit();self.admin,self.manager,self.staff=ids
  self.client=self.app.test_client();self.login(self.admin)
  self.clock=patch('app.staff_operations.today',return_value=date(2026,10,20));self.clock.start()
  self.maintenance=patch('app.cafe.run_leave_maintenance');self.maintenance.start()
 def tearDown(self):
  self.clock.stop();self.maintenance.stop()
  with self.app.app_context():db.session.remove();db.engine.dispose()
  self.temp.cleanup()
 def login(self,uid):
  with self.client.session_transaction() as s:s['user_id']=uid;s['staff_ops_csrf']='test'
 def post_days(self,days,first='P',second='P',notified='2026-10-01',revision=None):
  with self.app.app_context():rev=month_revision(self.staff,date(2026,10,1))
  cells=[dict(day=f'2026-10-{day:02}',half=half,status=status) for day in days for half,status in [('first_half',first),('second_half',second)]]
  return self.client.post('/cafe/staff/attendance-grid',data=dict(user_id=self.staff,month='2026-10',csrf_token='test',revision=revision or rev,cells=json.dumps(cells),notified_on=notified,note='Reviewed register'))
 def test_manager_half_days_shared_cap_and_unpaid(self):
  self.login(self.manager)
  self.post_days([3,4,5],first='P',second='PL')
  self.post_days([6,7],first='P',second='SL')
  self.post_days([8],first='P',second='IU')
  with self.app.app_context():
   rows=StaffDayEntry.query.filter_by(user_id=self.staff).order_by(StaffDayEntry.day).all()
   self.assertEqual([r.payable for r in rows],[1,1,1,1,Decimal('.5'),Decimal('.5')])
   self.assertEqual(StaffAbsencePenalty.query.count(),0)
   self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff,attendance_date=date(2026,10,7)).one().status,'manual_half_paid')
  page=self.client.get(f'/cafe/staff?section=attendance_calendar&attendance_user_id={self.staff}&attendance_year=2026&attendance_month=10').get_data(as_text=True)
  self.assertIn('1st: P',page);self.assertIn('2nd: SL',page);self.assertNotIn('Selected Day Detail',page);self.assertNotIn('Team Day Timeline',page)
 def test_atomic_validation_stale_and_access(self):
  with self.app.app_context():rev=month_revision(self.staff,date(2026,10,1))
  self.post_days([5]);self.post_days([6],revision=rev)
  self.post_days([7,31])
  with self.app.app_context():self.assertEqual(StaffDayEntry.query.count(),1)
  self.login(self.staff);self.assertEqual(self.client.get('/cafe/staff/attendance-grid').status_code,403)
 def test_penalty_pool_cash_snapshot_revoke_and_no_duplicates(self):
  self.post_days([9,10],first='UA',second='UA',notified='')
  with self.app.app_context():
   actor=db.session.get(User,self.admin);ps=StaffAbsencePenalty.query.order_by(StaffAbsencePenalty.id).all()
   approve_penalty(ps[0],actor,Decimal(31000),'Confirmed unnotified absence')
   approve_penalty(ps[1],actor,Decimal(31000),'Confirmed unnotified absence');db.session.commit()
   self.assertEqual(ps[0].pool_days,1);self.assertEqual(ps[0].cash_amount,0)
   self.assertEqual(ps[1].cash_amount,1000);self.assertEqual(ps[1].due_date,date(2026,10,21));pid=ps[1].id
   with self.assertRaises(ValueError):approve_penalty(ps[1],actor,Decimal(31000),'Again')
   self.assertEqual(penalty_totals(self.staff,date(2026,10,21),date(2026,11,6)),(Decimal(1000),Decimal(0)))
  self.post_days([10])
  with self.app.app_context():self.assertEqual(StaffDayEntry.query.filter_by(day=date(2026,10,10)).one().first_half,'UA')
  self.login(self.manager)
  self.assertEqual(self.client.post('/cafe/staff/absence-penalties',data=dict(csrf_token='test',action='revoke',penalty_id=pid,reason='Special case')).status_code,403)
  self.login(self.admin)
  self.client.post('/cafe/staff/absence-penalties',data=dict(csrf_token='test',action='revoke',penalty_id=pid,reason='Special case'))
  with self.app.app_context():self.assertEqual(db.session.get(StaffAbsencePenalty,pid).status,'revoked')
 def test_planned_and_abrupt_discharge(self):
  response=self.client.post(f'/cafe/staff/{self.staff}/discharge',data=dict(csrf_token='test',kind='planned',notice_start='2026-10-10',reason='Resigned with notice'))
  self.assertEqual(response.status_code,302)
  with self.app.app_context():
   self.assertEqual(db.session.get(StaffDischarge,self.staff).last_working_day,date(2026,10,25))
   with patch('app.staff_operations.today',return_value=date(2026,10,25)):apply_due_discharges();self.assertTrue(db.session.get(User,self.staff).active)
   with patch('app.staff_operations.today',return_value=date(2026,10,26)):apply_due_discharges();self.assertFalse(db.session.get(User,self.staff).active)
  self.client.post(f'/cafe/staff/{self.manager}/discharge',data=dict(csrf_token='test',kind='abrupt',reason='Left without notice'))
  with self.app.app_context():self.assertFalse(db.session.get(User,self.manager).active)
 def test_new_id_and_password_and_ui(self):
  data=dict(csrf_token='test',action='create',username='Ravi.Kumar',full_name='Ravi Kumar',password='SecurePassword-123',roles='staff',joining_date='2026-10-01')
  response=self.client.post('/cafe/staff',data=data,headers={'X-Staff-Onboarding':'1'})
  self.assertEqual(response.json['email'],'ravi.kumar@brownberriescafe.com')
  with self.app.app_context():
   u=User.query.filter_by(email=response.json['email']).one();self.assertTrue(check_password_hash(u.password_hash,data['password']))
  data['username']='attacker@elsewhere.com';self.client.post('/cafe/staff',data=data)
  with self.app.app_context():self.assertIsNone(User.query.filter_by(email='attacker@elsewhere.com').first())
  for section in ('active_staff','add_new_staff','leave_requests','attendance_calendar','attendance_entry','payroll_summary'):
   response=self.client.get('/cafe/staff?section='+section)
   self.assertEqual(response.status_code,200,section)
  self.assertIn(b'staff-generate-password',self.client.get('/cafe/staff?section=add_new_staff').data)
 def test_mixed_half_policy(self):
  result=calculate_days([ManualDay(date(2026,10,5),'P',date(2026,10,1),True,True,'BL','SL')],joined_on=date(2026,10,1))[0]
  self.assertEqual(result.paid_bereavement,Decimal('.5'));self.assertEqual(result.paid_leave,Decimal('.5'));self.assertEqual(result.payable,1)

 def test_partial_recovery_carries_forward_and_revocation_refunds_once(self):
  self.post_days([9],first='UA',second='UA',notified='')
  with self.app.app_context():
   db.session.get(LeaveBalance,LeaveBalance.query.filter_by(user_id=self.staff).one().id).earned_balance=0
   p=StaffAbsencePenalty.query.one();approve_penalty(p,db.session.get(User,self.admin),Decimal(31000),'Verified');db.session.commit();pid=p.id
  with patch('app.staff_operations.today',return_value=date(2026,10,21)),patch('app.leave_logic.run_leave_maintenance'):
   self.client.post('/cafe/staff/absence-penalties',data={'csrf_token':'test','action':'close_period','user_id':self.staff,'due_date':'2026-10-21','reason':'Salary payment #1','take_'+str(pid):'400'})
   self.client.post('/cafe/staff/absence-penalties',data={'csrf_token':'test','action':'close_period','user_id':self.staff,'due_date':'2026-10-21','reason':'Duplicate','take_'+str(pid):'400'})
  with self.app.app_context():
   self.assertEqual(StaffPenaltyRecovery.query.count(),1)
   p=db.session.get(StaffAbsencePenalty,pid);self.assertEqual(p.due_date,date(2026,11,6))
   self.assertEqual(penalty_totals(self.staff,date(2026,10,21),date(2026,11,6)),(Decimal(400),Decimal(600)))
  with patch('app.leave_logic.run_leave_maintenance'):
   self.client.post('/cafe/staff/absence-penalties',data=dict(csrf_token='test',action='revoke',penalty_id=pid,reason='Management exception'))
  with self.app.app_context():
   refunds=StaffAbsencePenalty.query.filter(StaffAbsencePenalty.cash_amount<0).all();self.assertEqual(len(refunds),1);self.assertEqual(refunds[0].cash_amount,-400)
 def test_active_manual_policy_half_days_and_locked_finalization(self):
  from app.models import InitialSetupState
  from app.manual_payroll import KEY,month_preview
  from app.manual_payroll_models import ManualEmployment,ManualAttendance
  with self.app.app_context():
   db.session.add(InitialSetupState(key=KEY));db.session.add(ManualEmployment(user_id=self.staff,joined_on=date(2026,10,1),notice_days=0,management_released=False,confirmed_by=self.admin));db.session.commit()
  self.login(self.manager);self.post_days([8],first='P',second='IU')
  with self.app.app_context():
   m=ManualAttendance.query.filter_by(user_id=self.staff).one();self.assertEqual((m.first_half,m.second_half),('P','IU'))
   view=month_preview(self.staff,date(2026,10,1));self.assertEqual(view['payable_days'],Decimal('.5'))
   self.assertEqual(StaffDayEntry.query.one().payable,Decimal('.5'))
   self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).count(),0)
