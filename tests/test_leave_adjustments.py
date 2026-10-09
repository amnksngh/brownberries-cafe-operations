import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import User, StaffProfile, LeaveBalance, LeaveTransaction, InitialSetupState
from app.manual_payroll import KEY


class LeaveAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app(instance_path=self.temp.name, initialize_legacy_leaves=False)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        with self.app.app_context():
            users = []
            for role in ('admin', 'manager', 'staff', 'owner'):
                user = User(full_name=role, email=role+'@test', role=role, password_hash='x', active=True)
                db.session.add(user)
                db.session.flush()
                db.session.add(StaffProfile(user_id=user.id))
                db.session.add(LeaveBalance(user_id=user.id, earned_balance=12, urgent_balance=4))
                users.append(user.id)
            self.admin, self.manager, self.staff, self.owner = users
            db.session.commit()
        self.maintenance = patch('app.leave_adjustments.run_leave_maintenance')
        self.maintenance.start()
        self.url = '/cafe/staff/accumulated-leave'
        self.login(self.admin)

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
            session['leave_adjustment_csrf'] = 'test-token'

    def post(self, **changes):
        data = dict(user_id=self.staff, balance='0', expected='12', revision='0',
                    reason='Correct opening balance', csrf_token='test-token')
        data.update(changes)
        return self.client.post(self.url, data=data)

    def tearDown(self):
        self.maintenance.stop()
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp.cleanup()

    def test_reset_audited_and_repeat_or_stale_write_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.post().status_code, 302)
        self.post()
        self.post(balance='5')
        with self.app.app_context():
            balance = LeaveBalance.query.filter_by(user_id=self.staff).one()
            self.assertEqual(balance.earned_balance, 0)
            self.assertEqual(balance.urgent_balance, 4)
            row = LeaveTransaction.query.filter_by(transaction_type='admin_adjustment').one()
            self.assertEqual(row.amount, -12)
            self.assertEqual(row.created_by_user_id, self.admin)
            self.assertIn('Correct opening balance', row.note)

    def test_non_admin_cannot_read_or_write(self):
        for user_id in (self.manager, self.staff, self.owner):
            self.login(user_id)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.post().status_code, 403)

    def test_csrf_validation_and_invalid_values(self):
        self.assertEqual(self.post(csrf_token='bad').status_code, 400)
        for value in ('-1', 'NaN', 'Infinity', '0.1', 'abc', '10001'):
            self.post(balance=value)
        self.post(reason=' ')
        with self.app.app_context():
            self.assertEqual(LeaveBalance.query.filter_by(user_id=self.staff).one().earned_balance, 12)
            self.assertEqual(LeaveTransaction.query.filter_by(transaction_type='admin_adjustment').count(), 0)

    def test_admin_employee_can_be_adjusted(self):
        self.post(user_id=self.admin, balance='3.5')
        with self.app.app_context():
            self.assertEqual(LeaveBalance.query.filter_by(user_id=self.admin).one().earned_balance, 3.5)

    def test_active_new_policy_blocks_legacy_adjustment(self):
        with self.app.app_context():
            db.session.add(InitialSetupState(key=KEY))
            db.session.commit()
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.client.get(self.url).status_code, 409)
