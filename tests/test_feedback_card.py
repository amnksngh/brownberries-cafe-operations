import unittest
from datetime import datetime
from test_kot import KitchenTicketTests
from app.extensions import db
from app.models import MenuItem, CafeOrder, CafeOrderItem, Workstation


class FeedbackCardTests(KitchenTicketTests):
    def prepare(self):
        item = db.session.get(MenuItem, self.mid)
        item.chef_user_id = self.uid
        item.prep_station = 'barista'
        if not Workstation.query.filter_by(slug='barista').first():
            db.session.add(Workstation(slug='barista', name='Barista test'))
        for line in CafeOrderItem.query.all():
            line.approval_status = 'approved'
        db.session.commit()

    def test_unique_both_assignments_water_and_approval(self):
        from app.cafe import _rating_card_items
        with self.app.app_context():
            self.prepare()
            source = db.session.get(MenuItem, self.mid)
            for name, station, chef, status in [
                ('Water', 'barista', self.uid, 'approved'),
                ('Ice cream', 'barista', None, 'approved'),
                ('Unassigned', '', self.uid, 'approved'),
                ('Pending', 'barista', self.uid, 'pending'),
                ('Rejected', 'barista', self.uid, 'rejected'),
                ('Watermelon Juice', 'barista', self.uid, 'approved')]:
                item = MenuItem(name=name, category_id=source.category_id, item_type='beverage', price=50, prep_station=station, chef_user_id=chef)
                db.session.add(item); db.session.flush()
                db.session.add(CafeOrderItem(order_id=self.oid, menu_item_id=item.id, quantity=2, unit_price=50, approval_status=status))
            db.session.commit()
            self.assertEqual([r['name'] for r in _rating_card_items([db.session.get(CafeOrder,self.oid)])], ['Coffee','Watermelon Juice'])
        response=self.client.get(f'/cafe/feedback-card/{self.oid}')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data.count(b'<td>Coffee</td>'),1)
        self.assertIn(b'cafe-logo.png',response.data)
        self.assertIn(b'no-store',response.headers['Cache-Control'].encode())

    def test_settlement_boundary_and_access(self):
        with self.app.app_context():
            self.prepare()
            order=db.session.get(CafeOrder,self.oid)
            order.status='paid'; order.paid_at=datetime.utcnow(); order.settlement_group_id='feedback-one'
            other=CafeOrder(table_id=order.table_id, ordered_by_user_id=self.uid, status='paid', paid_at=order.paid_at, settlement_group_id='feedback-two', display_code='OTHER-GUEST')
            db.session.add(other);db.session.commit()
        response=self.client.get(f'/cafe/feedback-card/{self.oid}')
        self.assertNotIn(b'OTHER-GUEST',response.data)
        with self.client.session_transaction() as session:session.clear()
        self.assertNotEqual(self.client.get(f'/cafe/feedback-card/{self.oid}').status_code,200)
        self.assertEqual(self.client.get(f'/cafe/reception/invalid/feedback-card/{self.oid}').status_code,403)

    def test_empty_card_no_print_button(self):
        response=self.client.get(f'/cafe/feedback-card/{self.oid}')
        self.assertIn(b'No eligible items',response.data)
        self.assertNotIn(b'onclick="printCard()"',response.data)

    def test_running_table_combines_orders_not_previous_guests(self):
        with self.app.app_context():
            self.prepare()
            order=db.session.get(CafeOrder,self.oid)
            source=db.session.get(MenuItem,self.mid)
            for code, status in [('SECOND-ORDER','open'),('PREVIOUS-GUEST','paid'),('CANCELLED-GUEST','cancelled')]:
                other=CafeOrder(table_id=order.table_id,ordered_by_user_id=self.uid,status=status,display_code=code)
                db.session.add(other);db.session.flush()
                db.session.add(CafeOrderItem(order_id=other.id,menu_item_id=source.id,quantity=3,unit_price=100,approval_status='approved'))
            db.session.commit()
        page=self.client.get(f'/cafe/feedback-card/{self.oid}?scope=table')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'SECOND-ORDER',page.data)
        self.assertNotIn(b'PREVIOUS-GUEST',page.data)
        self.assertNotIn(b'CANCELLED-GUEST',page.data)
        self.assertEqual(page.data.count(b'<td>Coffee</td>'),1)

    def test_cashier_button_renders(self):
        response=self.client.get('/cafe/cashier')
        self.assertEqual(response.status_code,200)
