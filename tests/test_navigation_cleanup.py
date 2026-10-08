import re
import unittest
from tempfile import TemporaryDirectory

from flask import g, render_template
from app import create_app
from app.extensions import db
from app.models import User


class NavigationCleanupTests(unittest.TestCase):
    def test_shared_header_and_cafe_modules_for_all_roles(self):
        with TemporaryDirectory() as folder:
            app=create_app(instance_path=folder,initialize_legacy_leaves=False)
            try:
                for role in ('admin','manager','staff','owner','cashier','inventory_manager',
                             'accountant','barista','chef','librarian','server','delivery_partner'):
                    with self.subTest(role=role), app.test_request_context('/cafe/'):
                        g.current_user=User(id=1,full_name='Fixture',email='fixture@test',
                                            password_hash='test',role=role,active=True)
                        html=render_template('cafe/home.html',workstation_options=[
                            {'slug':'kitchen','name':'Kitchen'},{'slug':'barista','name':'Barista'}])
                        nav=re.search(r'<nav>(.*?)</nav>',html,re.S).group(1)
                        for label in ('To Purchase','Report Breakage','Inventory','Kitchen Display','Staff','Library'):
                            self.assertNotRegex(nav,r'>'+re.escape(label)+r'</a>')
                        self.assertIn('Table Ordering',nav)
                        self.assertIn('Items Availability',nav)
                        self.assertIn('Logout',nav)
                        for label in ('Staff Attendance QR &amp; Geofence','Staff Attendance QR & Geofence',
                                      'Staff QR Check-In URL','Attendance QR','attendance-geofence-map','leaflet.js'):
                            self.assertNotIn(label,html)
                        modules=re.search(r'<div class="links">(.*?)</div>',html,re.S).group(1)
                        self.assertNotIn('Display</a>',modules)
                        self.assertIn('Inventory Management',modules)
                        self.assertIn('Menu Management',modules)
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()
