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

    def test_inactive_staff_hidden_and_records_preserved(self):
        self.post('attendance',day='2026-10-15',status='present_all_day')
        with self.app.app_context():
            user = db.session.get(User, self.staff)
            user.active = False
            user.full_name = 'Inactive fixture employee'
            db.session.commit()
        self.assertEqual(self.client.get(self.url+'?user_id='+str(self.staff)).status_code,404)
        html = self.client.get(self.url).get_data(as_text=True)
        self.assertNotIn('Inactive fixture employee', html)
        with patch('app.cafe.render_template', return_value='ok') as render:
            response = self.client.get('/cafe/staff?section=payroll_summary&payroll_month=10&payroll_year=2026')
            self.assertEqual(response.status_code, 200)
            context = render.call_args.kwargs
            self.assertNotIn(self.staff, [row['user'].id for row in context['payroll_rows']])
            self.assertNotIn(self.staff, [row.user_id for row in context['active_profiles']])
            self.assertIn(self.staff, [row.user_id for row in context['archived_profiles']])
            self.assertEqual(context['payroll_period']['second_due'], date(2026,11,6))
        with self.app.app_context():
            self.assertEqual(StaffAttendance.query.filter_by(user_id=self.staff).count(),1)

    def test_payroll_summary_splits_at_fifteenth(self):
        with self.app.app_context():
            db.session.get(User, self.staff).staff_profile.salary_amount = 31000
            db.session.add_all([StaffAttendance(user_id=self.staff, attendance_date=date(2026,10,day),
                status='present_all_day', manager_override=True) for day in (15,16,31)])
            db.session.commit()
        with patch('app.cafe.render_template', return_value='ok') as render:
            response = self.client.get('/cafe/staff?section=payroll_summary&payroll_month=10&payroll_year=2026')
            self.assertEqual(response.status_code,200)
            row = next(r for r in render.call_args.kwargs['payroll_rows'] if r['user'].id == self.staff)
            self.assertEqual(row['estimated_pay'],3000)
            self.assertEqual(row['first_half_estimate'],1000)
            self.assertEqual(row['second_half_estimate'],2000)

    def test_register_split_sessions_edit_and_overlap(self):
        self.post('register', day='2026-10-15',check_in='2026-10-15T09:00',
                  check_out='2026-10-15T13:00',status='present_all_day')
        self.post('register', day='2026-10-15',check_in='2026-10-15T14:00',
                  check_out='2026-10-15T18:00',status='first_half')
        self.post('register', day='2026-10-15',check_in='2026-10-15T12:00',
                  check_out='2026-10-15T15:00',status='present_all_day')
        with self.app.app_context():
            rows=StaffAttendance.query.filter_by(user_id=self.staff).order_by(StaffAttendance.id).all()
            self.assertEqual(len(rows),2)
            self.assertTrue(all(r.status=='present_all_day' for r in rows))
            rid=rows[0].id
        self.post('register', attendance_id=rid,day='2026-10-15',check_in='2026-10-15T08:30',
                  check_out='2026-10-15T13:00')
        with self.app.app_context():
            row=db.session.get(StaffAttendance,rid)
            self.assertEqual(row.check_in_at.hour,8)
            self.assertEqual(row.check_in_method,'admin_register')
            audits=ManualPayrollAudit.query.filter_by(user_id=self.staff,action='staff_proxy_register').all()
            self.assertEqual(len(audits),3)
            self.assertIn('09:00',audits[-1].details_json)
        self.login(self.manager)
        self.assertEqual(self.post('register',day='2026-10-15').status_code,403)

    def test_register_validation_and_status_only_edit(self):
        self.post('attendance',day='2026-10-15',status='first_half')
        with self.app.app_context():
            rid=StaffAttendance.query.filter_by(user_id=self.staff).one().id
        for start,end in [('2026-10-15T13:00','2026-10-15T09:00'),
                          ('2026-10-15T09:00','2026-10-17T09:00'),
                          ('2026-10-14T09:00','2026-10-15T09:00')]:
            self.post('register',attendance_id=rid,day='2026-10-15',check_in=start,check_out=end)
        with self.app.app_context():
            self.assertIsNone(db.session.get(StaffAttendance,rid).check_in_at)
        self.post('register',attendance_id=rid,day='2026-10-15',check_in='2026-10-15T22:00',
                  check_out='2026-10-16T02:00')
        with self.app.app_context():
            row=db.session.get(StaffAttendance,rid)
            self.assertEqual(row.status,'first_half')
            self.assertEqual(row.check_out_at.day,16)

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
