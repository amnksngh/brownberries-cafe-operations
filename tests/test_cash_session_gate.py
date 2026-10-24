import unittest
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
from app import create_app
from app.extensions import db
from app.models import User, CashCounterEntry
from app.cash_desk_models import CashDeskSession
from app import cash_desk as desk


class SessionGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app(instance_path=self.temp.name, initialize_legacy_leaves=False)
        self.app.config['TESTING'] = True
        with self.app.app_context():
            user = User(full_name='Cashier',email='gate@test',password_hash='x',role='cashier',active=True)
            db.session.add(user); db.session.commit(); self.uid=user.id
        self.client=self.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id']=self.uid; s['cash_desk_csrf']='test'
        self.day=date(2026,10,23)
        self.clock=patch('app.cash_desk.today',side_effect=lambda:self.day)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        with self.app.app_context(): db.session.remove(); db.engine.dispose()
        self.temp.cleanup()

    def control(self,action,**data):
        with self.app.app_context(): revision=desk.revision()
        return self.client.post('/cafe/cash-counter/control', data=dict(action=action,
            csrf_token='test',request_key=uuid4().hex,revision=revision,reason='Test',**data),follow_redirects=True)

    def assert_blocked_transactions(self, phrase):
        for action in ('refill','expense','handover','end_withdrawal','exchange','reconcile','request_correction','approve','reject','count'):
            self.assertIn(phrase,self.control(action,new_cash_confirmed='yes',in_note_100='1').data)
        for path in ('orders/999/approve','orders/999/mark-paid','tables/999/clear-orders',
                     'tables/999/move-orders','orders/999/edit-items','cash-counter/999/delete',
                     'orders/999/send-receipt-sms','orders'):
            # Route implementations check the session before loading an order or mutating cash.
            response=self.client.post('/cafe/'+path)
            self.assertEqual(response.status_code,302,path)
            self.assertIn('/cash-counter/control',response.location,path)
        with self.app.app_context(): self.assertEqual(CashCounterEntry.query.count(),0)

    def test_missing_open_overdue_close_and_next_day_open(self):
        self.assert_blocked_transactions(b'Open today')
        self.control('open',count_note_100='2')
        with self.app.app_context():
            desk.require_open_session()
            self.assertEqual(CashCounterEntry.query.count(),0)
        self.day=date(2026,10,24)
        self.assert_blocked_transactions(b'Close the outstanding')
        self.control('open')
        with self.app.app_context(): self.assertEqual(CashDeskSession.query.count(),1)
        self.control('close',count_note_100='2')
        with self.app.app_context():
            self.assertIsNotNone(CashDeskSession.query.one().closing_id)
            self.assertEqual(CashCounterEntry.query.count(),0)
        self.assert_blocked_transactions(b'Open today')
        self.control('open',count_note_100='2')
        self.control('refill',new_cash_confirmed='yes',in_note_100='1')
        with self.app.app_context():
            self.assertEqual(CashDeskSession.query.count(),2)
            self.assertEqual(desk.snapshot()['counts']['note_100'],1)
        self.control('close',count_note_100='3')
        response=self.control('refill',new_cash_confirmed='yes',in_note_100='1')
        self.assertIn(b'Open today',response.data)
        with self.app.app_context(): self.assertEqual(CashCounterEntry.query.count(),1)
