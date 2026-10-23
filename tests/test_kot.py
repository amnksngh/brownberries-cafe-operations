import json
import unittest
from tempfile import TemporaryDirectory
from app import create_app
from app.extensions import db
from app.models import User, MenuCategory, MenuItem, CafeTable, CafeOrder, CafeOrderItem, CafeKitchenTicket


class KitchenTicketTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.app=create_app(instance_path=self.temp.name, initialize_legacy_leaves=False)
        self.app.config['TESTING']=True
        with self.app.app_context():
            user=User(full_name='Cashier',email='kot@test',password_hash='x',role='cashier',active=True)
            table=CafeTable(name='T08',qr_slug='kot-test')
            category=MenuCategory(name='Fixture drinks')
            db.session.add(category);db.session.flush()
            menu=MenuItem(name='Coffee',description='Fresh brewed <coffee>',price=100,category_id=category.id,item_type='beverage')
            db.session.add_all([user,table,menu]);db.session.flush()
            order=CafeOrder(table_id=table.id,ordered_by_user_id=user.id,status='pending',daily_sequence=1)
            db.session.add(order);db.session.flush()
            lines=[CafeOrderItem(order_id=order.id,menu_item_id=menu.id,quantity=n,unit_price=100,approval_status='pending',size_label='Regular' if n == 1 else 'With Ice Cream') for n in (1,2)]
            db.session.add_all(lines);db.session.commit()
            self.uid,self.oid,self.mid=user.id,order.id,menu.id
            self.ids=[r.id for r in lines]
        self.client=self.app.test_client()
        with self.client.session_transaction() as s:s['user_id']=self.uid

    def tearDown(self):
        with self.app.app_context():db.session.remove();db.engine.dispose()
        self.temp.cleanup()

    def approve(self,ids,decision='approve'):
        return self.client.post(f'/cafe/orders/{self.oid}/approve',data=dict(
            pending_item_ids=[str(i) for i in ids],decision=decision,print_kot='1'))

    def test_partial_approval_snapshot_and_repeat(self):
        response=self.approve(self.ids[:1])
        self.assertIn('print_kot=',response.location)
        with self.app.app_context():
            ticket=CafeKitchenTicket.query.one();token=ticket.token
            data=json.loads(ticket.contents_json)
            self.assertEqual(len(data['items']),1)
            self.assertEqual(data['table'],'T08')
            self.assertEqual(data['items'][0]['quantity'],1)
            db.session.get(MenuItem,self.mid).description='Changed later'
            db.session.commit()
        self.assertNotIn('print_kot=',self.approve(self.ids[:1]).location)
        page=self.client.get('/cafe/kot/'+token)
        self.assertEqual(page.status_code,200)
        self.assertNotIn(b'Fresh brewed',page.data)
        self.assertIn(b'Regular',page.data)
        self.assertNotIn(b'With Ice Cream',page.data)
        self.assertNotIn(b'Changed later',page.data)
        self.assertIn(b'IST',page.data)
        self.assertIn('no-store',page.headers['Cache-Control'])
        self.approve(self.ids[1:])
        with self.app.app_context():self.assertEqual(CafeKitchenTicket.query.count(),2)

    def test_reject_empty_and_invalid_ticket(self):
        self.assertNotIn('print_kot=',self.approve([]).location)
        self.assertNotIn('print_kot=',self.approve(self.ids,'reject').location)
        with self.app.app_context():self.assertEqual(CafeKitchenTicket.query.count(),0)
        self.assertEqual(self.client.get('/cafe/kot/not-a-real-token').status_code,404)

    def test_excluded_items_still_approved_without_print_prompt(self):
        with self.app.app_context():
            db.session.get(MenuItem,self.mid).prep_station='barista'
            db.session.commit()
        response=self.approve(self.ids)
        self.assertNotIn('print_kot=',response.location)
        with self.app.app_context():
            self.assertEqual(CafeKitchenTicket.query.count(),0)
            self.assertTrue(all(r.approval_status=='approved' for r in CafeOrderItem.query.all()))

    def test_water_exclusions_are_specific(self):
        from app.cafe import _include_in_kot
        from types import SimpleNamespace
        for name in ['Water',' WATER ', 'Mineral Water (1 L)', 'Water 500 ml', 'Water (पानी)']:
            self.assertFalse(_include_in_kot(SimpleNamespace(name=name,prep_station='kitchen')))
        for name in ['Watermelon Juice','Cold Coffee','Coconut Water']:
            self.assertTrue(_include_in_kot(SimpleNamespace(name=name,prep_station='kitchen')))

    def test_mixed_batch_contains_only_kitchen_items(self):
        with self.app.app_context():
            source=db.session.get(MenuItem,self.mid)
            water=MenuItem(name='Water',category_id=source.category_id,item_type='beverage',price=20)
            db.session.add(water);db.session.flush()
            extra=CafeOrderItem(order_id=self.oid,menu_item_id=water.id,quantity=1,unit_price=20,approval_status='pending')
            db.session.add(extra);db.session.commit();extra_id=extra.id
        self.approve(self.ids+[extra_id])
        with self.app.app_context():
            items=json.loads(CafeKitchenTicket.query.one().contents_json)['items']
            self.assertEqual(len(items),2)
            self.assertTrue(all(r['name']=='Coffee' for r in items))
