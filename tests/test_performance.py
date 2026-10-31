import json
import unittest
from tempfile import TemporaryDirectory

from app import create_app
from app.extensions import db
from app.models import User, StaffProfile
from app.organisation import Organisation, starter_positions
from app.performance import PerformanceScorecard, PerformanceReview, PerformanceAudit


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app(instance_path=self.temp.name, initialize_legacy_leaves=False)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.url = '/cafe/staff/performance'
        with self.app.app_context():
            self.ids = {}
            for role in ('admin', 'owner', 'manager', 'staff'):
                user = User(full_name=role, email=role+'@review.test', password_hash='x', role=role)
                db.session.add(user)
                db.session.flush()
                db.session.add(StaffProfile(user_id=user.id, salary_amount=25000))
                self.ids[role] = user.id
            rows = starter_positions()
            rows[2]['employees'] = [self.ids['staff']]
            db.session.add(Organisation(id=1, revision=1, document=json.dumps(rows)))
            db.session.commit()
        self.login('admin')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp.cleanup()

    def login(self, role):
        with self.client.session_transaction() as state:
            state['user_id'] = self.ids[role]
            state['performance_csrf'] = 'token'

    def post(self, action, **kwargs):
        data = dict(action=action, csrf_token='token')
        data.update(kwargs)
        return self.client.post(self.url, data=data, follow_redirects=True)

    def card(self, **kwargs):
        data = dict(position_id='3', grade='G1', threshold='80', label_0='Hygiene',
                    target_0='Signed checks: compliance percentage is score', weight_0='60',
                    label_1='Accuracy', target_1='Correct preparations / all preparations * 100', weight_1='40')
        data.update(kwargs)
        return self.post('scorecard', **data)

    def create(self, **kwargs):
        data = dict(card_id=1, employee_id=self.ids['staff'], reviewer_id=self.ids['manager'], start='2026-09-01', end='2026-09-30')
        data.update(kwargs)
        return self.post('create', **data)

    def submit(self, **kwargs):
        data = dict(review_id=1, version=1, score_0='90', evidence_0='Signed checks: 90/100',
                    score_1='80', evidence_1='Quality register: 80/100', recommendation='Training plan')
        data.update(kwargs)
        return self.post('submit', **data)

    def test_full_workflow_and_locked_approval_no_payroll_change(self):
        self.assertEqual(self.card().status_code, 200)
        self.assertEqual(self.create().status_code, 200)
        self.login('manager')
        self.assertEqual(self.submit().status_code, 200)
        with self.app.app_context():
            self.assertEqual(PerformanceReview.query.one().score, 86)
        self.login('owner')
        self.post('approve', review_id=1, version=2, reason='Evidence verified')
        self.post('approve', review_id=1, version=3, reason='Duplicate')
        self.login('manager')
        self.post('save', review_id=1, version=3, score_0=0)
        with self.app.app_context():
            review = PerformanceReview.query.one()
            self.assertEqual(review.status, 'approved')
            self.assertEqual(review.score, 86)
            self.assertEqual(review.version, 3)
            self.assertEqual(PerformanceAudit.query.count(), 3)
            self.assertEqual(db.session.get(User, self.ids['staff']).staff_profile.salary_amount, 25000)
            self.assertEqual(db.session.get(User, self.ids['staff']).role, 'staff')

    def test_return_resubmit_and_immutable_scorecard(self):
        self.card()
        self.create()
        self.card(threshold='95')
        self.login('manager')
        self.submit()
        self.login('admin')
        self.post('return', review_id=1, version=2, reason='Clarify evidence')
        self.login('manager')
        self.submit(version=3)
        with self.app.app_context():
            review = PerformanceReview.query.one()
            self.assertEqual(review.status, 'submitted')
            self.assertEqual(json.loads(review.snapshot)['threshold'], '80')
            self.assertEqual(review.version, 4)

    def test_invalid_weights_evidence_scores_and_stale_updates(self):
        for changes in [dict(weight_0='59'), dict(weight_0='NaN'), dict(target_0=''), dict(grade='G9')]:
            self.card(**changes)
        with self.app.app_context():
            self.assertEqual(PerformanceScorecard.query.count(), 0)
        self.card()
        self.create()
        self.login('manager')
        for changes in [dict(evidence_0=''), dict(score_0='101'), dict(score_0='NaN'), dict(version=0)]:
            self.submit(**changes)
        with self.app.app_context():
            self.assertEqual(PerformanceReview.query.one().version, 1)
            self.assertEqual(PerformanceReview.query.one().status, 'draft')

    def test_authorization_and_self_review(self):
        self.assertEqual(self.post('scorecard', csrf_token='bad').status_code, 400)
        self.card()
        self.create(reviewer_id=self.ids['staff'])
        with self.app.app_context():
            self.assertEqual(PerformanceReview.query.count(), 0)
        self.create(reviewer_id=self.ids['admin'])
        self.submit()
        self.assertEqual(self.post('approve', review_id=1, version=2, reason='Self approval').status_code, 403)
        self.login('manager')
        self.assertNotIn(b'Signed checks:', self.client.get(self.url).data)
        self.assertEqual(self.submit(version=2).status_code, 403)
        self.assertEqual(self.card().status_code, 403)
        self.login('staff')
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_duplicate_period_and_future_submission(self):
        self.card()
        self.create(start='2099-01-01', end='2099-01-31')
        self.create(start='2099-01-01', end='2099-01-31')
        self.login('manager')
        self.submit()
        with self.app.app_context():
            self.assertEqual(PerformanceReview.query.count(), 1)
            self.assertEqual(PerformanceReview.query.one().status, 'draft')
