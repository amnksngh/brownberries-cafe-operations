import hashlib
import json
from datetime import datetime, timedelta

from test_kot import KitchenTicketTests
from app.extensions import db
from app.models import User, CafeKitchenTicket
from app.kot_printing import KotPrintStation, KotPrintJob
from scripts.kitchen_print_agent import render


class KitchenPrintQueueTests(KitchenTicketTests):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            user = db.session.get(User, self.uid)
            user.role = 'admin'
            db.session.add(KotPrintStation(id=1, name='Kitchen', token_hash=hashlib.sha256(b'agent-secret').hexdigest(),
                enabled=True, printer='Kitchen Printer', test_mode=True))
            db.session.commit()
        with self.client.session_transaction() as s:
            s['kot_print_csrf'] = 'csrf-test'

    def ticket(self):
        self.approve(self.ids)
        with self.app.app_context():
            return CafeKitchenTicket.query.one().token

    def enqueue(self, token):
        return self.client.post('/cafe/kot-print/jobs/'+token, headers={'X-CSRF-Token':'csrf-test'})

    def poll(self, key='agent-secret'):
        return self.client.post('/api/kot-station/poll', json={'printers':['Kitchen Printer']}, headers={'Authorization':'Bearer '+key})

    def test_queue_idempotent_claim_and_ack(self):
        token = self.ticket()
        self.assertEqual(self.enqueue(token).status_code, 200)
        self.assertIn('offline', self.enqueue(token).json['message'])
        self.assertEqual(self.poll('bad').status_code, 401)
        job = self.poll().json['job']
        self.assertEqual(job['printer'], 'Kitchen Printer')
        self.assertTrue(job['test_mode'])
        self.assertIsNone(self.poll().json['job'])
        args = dict(claim_key=job['claim_key'], status='test_complete', detail='No physical print')
        for _ in range(2):
            response = self.client.post(f'/api/kot-station/result/{job["id"]}', json=args,
                headers={'Authorization':'Bearer agent-secret'})
            self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertEqual(KotPrintJob.query.count(), 1)
            self.assertEqual(KotPrintJob.query.one().status, 'test_complete')

    def test_csrf_missing_and_disabled_queue(self):
        token = self.ticket()
        self.assertEqual(self.client.post('/cafe/kot-print/jobs/'+token).status_code, 400)
        with self.app.app_context():
            db.session.get(KotPrintStation, 1).enabled = False
            db.session.commit()
        self.assertEqual(self.enqueue(token).status_code, 409)
        with self.app.app_context():
            self.assertEqual(KotPrintJob.query.count(), 0)

    def test_cancelled_job_is_not_delivered(self):
        token = self.ticket()
        job_id = self.enqueue(token).json['job_id']
        self.client.post('/cafe/kot-print', data=dict(csrf_token='csrf-test', action='cancel',
            job_id=job_id, reason='Order no longer needs ticket'))
        self.assertIsNone(self.poll().json['job'])
        self.assertEqual(self.enqueue(token).json['status'], 'cancelled')

    def test_admin_reprint_does_not_automatically_retry_claim(self):
        token = self.ticket()
        self.enqueue(token)
        job = self.poll().json['job']
        form = dict(csrf_token='csrf-test', action='reprint', job_id=job['id'], confirm='yes', reason='Checked printer')
        self.client.post('/cafe/kot-print', data=form)
        with self.app.app_context():
            self.assertEqual(KotPrintJob.query.count(), 1)
            KotPrintJob.query.one().claimed_at = datetime.utcnow()-timedelta(minutes=3)
            db.session.commit()
        self.client.post('/cafe/kot-print', data=form)
        self.client.post('/cafe/kot-print', data=form)
        with self.app.app_context():
            self.assertEqual(KotPrintJob.query.count(), 2)
        self.assertEqual(self.poll().json['job']['attempt'], 2)

    def test_station_settings_and_revocation(self):
        self.assertEqual(self.client.get('/cafe/kot-print').status_code, 200)
        self.poll()
        self.client.post('/cafe/kot-print', data=dict(csrf_token='csrf-test', action='configure',
            printer='Kitchen Printer', enabled='yes', test_mode='test'))
        self.client.post('/cafe/kot-print', data=dict(csrf_token='csrf-test', action='rotate'))
        self.assertEqual(self.poll().status_code, 401)
        with self.app.app_context():
            self.assertFalse(db.session.get(KotPrintStation, 1).enabled)
            db.session.get(User, self.uid).role = 'staff'
            db.session.commit()
        self.assertEqual(self.client.get('/cafe/kot-print').status_code, 403)

    def test_ticket_render_variety_and_control_sanitization(self):
        payload = render(dict(table='T1', order_id='D01', approved_at='01 Nov 2026',
            items=[dict(name='Coffee\x1b', size='With Ice Cream', quantity=2)]), 2)
        self.assertIn(b'With Ice Cream', payload)
        self.assertIn(b'REPRINT', payload)
        self.assertNotIn(b'Coffee\x1b', payload)
        self.assertLess(len(payload), 1000)
