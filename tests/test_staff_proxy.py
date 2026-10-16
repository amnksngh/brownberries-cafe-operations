import unittest
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from app import create_app
from app.extensions import db
from app.models import User, StaffProfile, StaffAttendance, StaffLeaveRequest, LeaveBalance, InitialSetupState
from app.manual_payroll_models import ManualPayrollAudit
from app.manual_payroll import KEY
from app.staff_proxy import revision


class StaffProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app(instance_path=self.temp.name, initialize_legacy_leaves=False)
        self.app.config['TESTING'] = True
        with self.app.app_context():
            users=[]
            for role in ('admin','manager','staff'):
                user=User(full_name=role,email=role+'@fixture',password_hash='x',role=role,active=True)
                db.session.add(user); db.session.flush()
                db.session.add(StaffProfile(user_id=user.id,joining_date=date(2026,10,1)))
                db.session.add(LeaveBalance(user_id=user.id,earned_balance=10,urgent_balance=0))
                users.append(user.id)
            db.session.commit(); self.admin,self.manager,self.staff=users
        self.client=self.app.test_client()
        self.url='/cafe/staff/on-behalf'
        self.clock=patch('app.staff_proxy.today',return_value=date(2026,10,20));self.clock.start()
        self.maintenance=patch('app.staff_proxy.run_leave_maintenance');self.maintenance.start()
        self.login(self.admin)

    def login(self,uid):
        with self.client.session_transaction() as s:
            s['user_id']=uid;s['staff_proxy_csrf']='test'

    def post(self,action,**values):
        with self.app.app_context(): rev=revision(self.staff)
        data=dict(user_id=self.staff,action=action,reason='Employee notified admin',csrf_token='test',revision=rev)
        data.update(values)
        return self.client.post(self.url,data=data)

    def tearDown(self):
        self.clock.stop();self.maintenance.stop()
        with self.app.app_context():db.session.remove();db.engine.dispose()
        self.temp.cleanup()

    def test_admin_records_present_absent_and_audit(self):
        self.assertEqual(self.client.get(self.url+'?user_id='+str(self.staff)).status_code,200)
        self.post('attendance',day='2026-10-15',status='present_all_day')
        self.post('attendance',day='2026-10-15',status='absent')
        with self.app.app_context():
            self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).one().status,'absent')
            audits=ManualPayrollAudit.query.filter_by(user_id=self.staff).all()
            self.assertEqual(len(audits),2)
            self.assertEqual(audits[1].actor_id,self.admin)
            self.assertIn('present_all_day',audits[1].details_json)

    def test_submit_approve_cancel_leave_and_mark_absent(self):
        self.post('leave',start='2026-10-15',end='2026-10-15',leave_type='earned',from_shift='first_half',to_shift='second_half')
        with self.app.app_context():
            leave=StaffLeaveRequest.query.filter_by(user_id=self.staff).one();lid=leave.id
            self.assertEqual(leave.status,'pending')
            self.assertEqual(LeaveBalance.query.filter_by(user_id=self.staff).one().earned_balance,10)
        self.post('decision',leave_id=lid,decision='approved')
        self.post('attendance',day='2026-10-15',status='absent')
        with self.app.app_context():
            self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).one().status,'earned_leave')
            self.assertEqual(LeaveBalance.query.filter_by(user_id=self.staff).one().earned_balance,9)
        self.post('decision',leave_id=lid,decision='cancelled')
        self.post('attendance',day='2026-10-15',status='absent')
        with self.app.app_context():
            self.assertEqual(LeaveBalance.query.filter_by(user_id=self.staff).one().earned_balance,10)
            self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).one().status,'absent')

    def test_immediate_approval_and_duplicate_blocked(self):
        data=dict(start='2026-10-15',end='2026-10-15',leave_type='earned',from_shift='first_half',to_shift='second_half',approve_now='yes')
        self.post('leave',**data);self.post('leave',**data)
        with self.app.app_context():
            self.assertEqual(StaffLeaveRequest.query.filter_by(user_id=self.staff).count(),1)
            self.assertEqual(LeaveBalance.query.filter_by(user_id=self.staff).one().earned_balance,9)

    def test_rights_csrf_future_and_stale_checks(self):
        for uid in (self.manager,self.staff):
            self.login(uid)
            self.assertEqual(self.client.get(self.url).status_code,403)
            self.assertEqual(self.post('attendance',day='2026-10-15',status='absent').status_code,403)
        self.login(self.admin)
        self.assertEqual(self.post('attendance',csrf_token='bad').status_code,400)
        self.post('attendance',day='2026-11-01',status='absent')
        self.post('attendance',day='2026-10-15',status='absent',revision='stale')
        with self.app.app_context():self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).count(),0)

    def test_new_policy_does_not_write_legacy_records(self):
        with self.app.app_context():db.session.add(InitialSetupState(key=KEY));db.session.commit()
        response=self.post('attendance',day='2026-10-15',status='absent')
        self.assertIn('/cafe/manual-payroll',response.headers['Location'])
        with self.app.app_context():self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).count(),0)
