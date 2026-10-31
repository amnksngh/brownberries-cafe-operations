import unittest
from tempfile import TemporaryDirectory

from app import create_app
from app.extensions import db
from app.models import User, LeaveBalance, LeaveTransaction
from app.leave_opening import LeaveOpeningSnapshot, capture_opening


class OpeningSnapshotTests(unittest.TestCase):
    def test_preserves_balances_idempotent_and_transactional(self):
        with TemporaryDirectory() as directory:
            app = create_app(instance_path=directory, initialize_legacy_leaves=False)
            with app.app_context():
                try:
                    self.assertEqual(LeaveOpeningSnapshot.query.count(), 0)
                    admin = User(full_name='Admin', email='opening@test', role='admin', password_hash='x')
                    db.session.add(admin)
                    db.session.flush()
                    balance = LeaveBalance(user_id=admin.id, earned_balance=5.5, urgent_balance=2)
                    db.session.add(balance)
                    db.session.commit()
                    self.assertEqual(capture_opening(admin), [admin.id])
                    self.assertEqual(capture_opening(admin), [])
                    row = db.session.get(LeaveOpeningSnapshot, admin.id)
                    self.assertEqual(row.earned_days, 5.5)
                    self.assertIsNone(row.cash_value)
                    self.assertEqual(row.valuation_status, 'pending_admin_review')
                    self.assertEqual(balance.earned_balance, 5.5)
                    self.assertEqual(LeaveTransaction.query.count(), 0)
                    db.session.rollback()
                    self.assertEqual(LeaveOpeningSnapshot.query.count(), 0)
                    capture_opening(admin)
                    db.session.commit()
                    balance.earned_balance = 7
                    db.session.commit()
                    self.assertEqual(capture_opening(admin), [])
                    self.assertEqual(db.session.get(LeaveOpeningSnapshot, admin.id).earned_days, 5.5)
                    admin.role = 'manager'
                    with self.assertRaises(PermissionError):
                        capture_opening(admin)
                finally:
                    db.session.remove()
                    db.engine.dispose()
