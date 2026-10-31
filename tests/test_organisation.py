import json
import unittest
from tempfile import TemporaryDirectory

from app import create_app
from app.extensions import db
from app.models import User, StaffProfile
from app.organisation import Organisation, OrganisationAudit, starter_positions, validate_structure


class OrganisationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app(instance_path=self.temp.name, initialize_legacy_leaves=False)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.url = '/cafe/staff/organisation'
        with self.app.app_context():
            self.users = {}
            for role in ('admin', 'owner', 'manager', 'staff'):
                user = User(full_name=role, email=role+'@test', password_hash='x', role=role)
                db.session.add(user)
                db.session.flush()
                db.session.add(StaffProfile(user_id=user.id, salary_amount=20000))
                self.users[role] = user.id
            db.session.commit()
        self.login('admin')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp.cleanup()

    def login(self, role):
        with self.client.session_transaction() as state:
            state['user_id'] = self.users[role]
            state['organisation_csrf'] = 'token'

    def post(self, **kwargs):
        data = dict(csrf_token='token', revision=0, action='seed', reason='Initial setup')
        data.update(kwargs)
        return self.client.post(self.url, data=data, follow_redirects=True)

    def form(self, **changes):
        data = dict(action='save', revision=1, position_id='1', title='Owner', department='Leadership',
                    enabled='yes', parent='', progression='', scope='', responsibilities='Strategy')
        for i in range(3):
            data.update({f'grade_{i}': f'G{i+1}', f'min_{i}': '', f'max_{i}': '', f'criteria_{i}': ''})
        data.update(changes)
        return data

    def test_seed_is_explicit_and_rendered_with_blank_grades(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        with self.app.app_context():
            self.assertEqual(Organisation.query.count(), 0)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'CDP', response.data)
        self.assertIn(b'Timestamps below are IST', response.data)
        with self.app.app_context():
            rows = json.loads(db.session.get(Organisation, 1).document)
            validate_structure(rows)
            self.assertTrue(all(len(p['grades']) == 3 for p in rows))
            self.assertTrue(all(g['minimum'] == '' for p in rows for g in p['grades']))
            self.assertFalse(next(p for p in rows if p['title'] == 'Executive Chef')['enabled'])
            self.assertEqual(OrganisationAudit.query.count(), 1)

    def test_permissions_csrf_and_no_duplicate_seed(self):
        for role in ('manager', 'staff'):
            self.login(role)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.post().status_code, 403)
        self.login('owner')
        self.assertEqual(self.post(csrf_token='invalid').status_code, 400)
        self.post()
        self.post(revision=1)
        with self.app.app_context():
            self.assertEqual(OrganisationAudit.query.count(), 1)

    def test_save_assignment_and_grades_never_changes_payroll_or_roles(self):
        self.post()
        self.post(**self.form(employees=str(self.users['staff']), min_0='10000', max_0='20000'))
        with self.app.app_context():
            state = db.session.get(Organisation, 1)
            self.assertEqual(state.revision, 2)
            row = json.loads(state.document)[0]
            self.assertEqual(row['employees'], [self.users['staff']])
            self.assertEqual(row['grades'][0]['minimum'], '10000')
            user = db.session.get(User, self.users['staff'])
            self.assertEqual(user.role, 'staff')
            self.assertEqual(user.staff_profile.salary_amount, 20000)
            self.assertEqual(OrganisationAudit.query.count(), 2)

    def test_invalid_and_stale_edits_leave_snapshot_unchanged(self):
        self.post()
        for changes in [dict(parent='1'), dict(parent='3'), dict(progression='1'),
                        dict(enabled='no'), dict(min_0='NaN'), dict(min_0='20', max_0='10'),
                        dict(grade_0='G2'), dict(scope='s:999999'), dict(revision=0),
                        dict(employees='999999')]:
            self.post(**self.form(**changes))
        with self.app.app_context():
            self.assertEqual(db.session.get(Organisation, 1).revision, 1)
            self.assertEqual(OrganisationAudit.query.count(), 1)

    def test_reporting_and_progression_separate(self):
        rows = starter_positions()
        commis = next(p for p in rows if p['title'] == 'Commis I — South Indian')
        lookup = {p['id']: p for p in rows}
        self.assertEqual(lookup[commis['parent']]['title'], 'CDP — South Indian')
        self.assertEqual(lookup[commis['progression']]['title'], 'DCDP — South Indian')
        lookup[commis['progression']]['progression'] = commis['id']
        with self.assertRaises(ValueError):
            validate_structure(rows)
