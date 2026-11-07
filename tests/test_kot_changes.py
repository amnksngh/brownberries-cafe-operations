import json
from test_kot_printing import KitchenPrintQueueTests
from app.extensions import db
from app.models import CafeOrder,CafeOrderItem,CafeKitchenTicket,MenuItem
from app.kot_printing import KotPrintJob,KotOrderEdit,edit_version
from scripts.kitchen_print_agent import render

class KotChangesTests(KitchenPrintQueueTests):
    def form(self, quantities):
        with self.app.app_context():
            order=db.session.get(CafeOrder,self.oid)
            data=dict(csrf_token='csrf-test',edit_version=edit_version(order),change_reason='Guest request')
            for line in order.order_items:
                qty=quantities.get(line.id,line.quantity)
                data[f'qty_{line.id}']=str(qty)
                data[f'keep_{line.id}']='1' if qty else '0'
            return data

    def edit(self,data):
        return self.client.post(f'/cafe/orders/{self.oid}/edit-items',data=data)

    def latest(self):
        with self.app.app_context():
            return json.loads(CafeKitchenTicket.query.order_by(CafeKitchenTicket.id.desc()).first().contents_json)

    def test_claimed_add_delta_and_duplicate_submit(self):
        self.enqueue(self.ticket());self.poll()
        data=self.form({self.ids[0]:3})
        self.assertEqual(self.edit(data).status_code,302)
        ticket=self.latest()
        self.assertEqual(ticket['items'][0]['action'],'ADD')
        self.assertEqual(ticket['items'][0]['quantity'],2)
        self.assertEqual(ticket['items'][0]['current'],3)
        self.edit(data)
        with self.app.app_context():self.assertEqual(KotOrderEdit.query.count(),1)
        self.assertTrue(self.client.post('/api/kot-station/poll', json={'printers':['Kitchen Printer'],'protocol':1}, headers={'Authorization':'Bearer agent-secret'}).json['upgrade_required'])
        job=self.client.post('/api/kot-station/poll',json={'printers':['Kitchen Printer'],'protocol':3},headers={'Authorization':'Bearer agent-secret'}).json['job']
        self.assertEqual(job['ticket']['kind'],'ORDER CHANGE')

    def test_unclaimed_tickets_preserve_table_count_sequence(self):
        token=self.ticket();self.enqueue(token)
        self.edit(self.form({self.ids[0]:4}))
        with self.app.app_context():
            self.assertEqual(KotPrintJob.query.order_by(KotPrintJob.id).first().status,'queued')
        self.assertEqual(self.client.get('/cafe/kot/'+token).status_code,200)
        item=self.latest()['table_items'][0]
        self.assertEqual((item['previous'],item['current']),(1,4))

    def test_reduce_cancel_preserved_and_acknowledged(self):
        self.enqueue(self.ticket());self.poll()
        self.edit(self.form({self.ids[1]:1}))
        self.assertEqual(self.latest()['items'][0]['action'],'REDUCE BY')
        self.edit(self.form({self.ids[0]:0,self.ids[1]:0}))
        self.assertEqual(self.latest()['kind'],'CANCEL KITCHEN ORDER')
        with self.app.app_context():
            self.assertEqual(CafeOrderItem.query.count(),2)
            self.assertEqual(db.session.get(CafeOrder,self.oid).status,'cancelled')
            eid=KotOrderEdit.query.order_by(KotOrderEdit.id.desc()).first().id
        self.assertEqual(self.client.get('/cafe/kot-changes').status_code,200)
        self.client.post('/cafe/kot-changes',data={'csrf_token':'csrf-test','edit_id':eid})
        with self.app.app_context():self.assertIsNotNone(db.session.get(KotOrderEdit,eid).acknowledged_at)

    def test_non_kitchen_changes_and_reason_required(self):
        with self.app.app_context():
            db.session.get(MenuItem,self.mid).name='Water';db.session.commit()
        self.approve(self.ids)
        data=self.form({self.ids[0]:3});data['change_reason']=''
        self.edit(data)
        with self.app.app_context():self.assertEqual(db.session.get(CafeOrderItem,self.ids[0]).quantity,1)
        data['change_reason']='Water quantity';self.edit(data)
        with self.app.app_context():
            self.assertEqual(CafeKitchenTicket.query.count(),0)
            self.assertEqual(KotOrderEdit.query.count(),1)

    def test_layout_commands_and_changes_are_unambiguous(self):
        ticket=dict(kind='ORDER CHANGE',protocol=2,items=[dict(name='Paneer Tikka Sandwich',quantity=2,previous=3,current=1,action='REDUCE BY',size='Regular')])
        payload=render(ticket)
        self.assertIn(b'REDUCE BY: 2',payload)
        self.assertIn(b'NEW TOTAL: 1',payload)
        self.assertIn(b'\x1dL\x18\x00',payload)
        self.assertIn(b'\x1dW\x10\x02',payload)
        self.assertIn(b'\x1d!\x01',payload)
        self.assertNotIn(b'\x1d!\x11',payload)

    def test_sequential_changes_remain_actionable(self):
        from app.kot_printing import pending_changes
        self.enqueue(self.ticket())
        self.edit(self.form({self.ids[0]:4}))
        self.edit(self.form({self.ids[0]:2}))
        with self.app.app_context():self.assertEqual(pending_changes().count(),2)
        item=self.latest()['table_items'][0]
        self.assertEqual((item['previous'],item['current']),(4,2))

    def test_staff_table_order_approval_edit_and_cashier_sync(self):
        from app.models import User
        with self.app.app_context():
            db.session.get(User,self.uid).role='staff'
            table_id=db.session.get(CafeOrder,self.oid).table_id
            db.session.commit()
        page=self.client.get(f'/cafe/orders?table_id={table_id}')
        self.assertEqual(page.status_code,200)
        self.assertIn(f'/cafe/orders/{self.oid}/edit-items'.encode(),page.data)
        self.assertIn(b'change_reason',page.data)
        self.approve(self.ids)
        with self.app.app_context():self.assertEqual(KotPrintJob.query.count(),1)
        self.poll()
        self.edit(self.form({self.ids[0]:3}))
        with self.app.app_context():
            self.assertEqual(db.session.get(CafeOrderItem,self.ids[0]).quantity,3)
            self.assertEqual(KotPrintJob.query.count(),2)
            db.session.get(User,self.uid).role='cashier';db.session.commit()
        page=self.client.get(f'/cafe/cashier?table_id={table_id}')
        self.assertEqual(page.status_code,200)
        self.assertIn(f'name="qty_{self.ids[0]}" value="3"'.encode(),page.data)

    def test_dropdown_reason_validation_and_preparation_guard(self):
        self.approve(self.ids)
        data=self.form({self.ids[0]:3});data.pop('change_reason')
        data['change_reason_code']='invalid'
        self.edit(data)
        with self.app.app_context():
            self.assertEqual(db.session.get(CafeOrderItem,self.ids[0]).quantity,1)
            db.session.get(CafeOrderItem,self.ids[0]).prep_status='preparing';db.session.commit()
        data['change_reason_code']='change_before_preparation';self.edit(data)
        with self.app.app_context():self.assertEqual(db.session.get(CafeOrderItem,self.ids[0]).quantity,1)
        data['change_reason_code']='more';self.edit(data)
        with self.app.app_context():
            self.assertEqual(KotOrderEdit.query.one().reason,'Customer wants more!')
            self.assertEqual(db.session.get(CafeOrderItem,self.ids[0]).quantity,3)

    def test_table_totals_across_orders_and_settlement_reset(self):
        from app.kot_table import table_state
        from datetime import datetime
        with self.app.app_context():
            item=db.session.get(MenuItem,self.mid);item.hindi_name='चाय'
            # Same variety merges into one table total.
            for line in CafeOrderItem.query.all():line.size_label='Regular'
            db.session.get(CafeOrderItem,self.ids[0]).quantity=2
            db.session.commit()
        self.approve(self.ids[:1])
        self.assertEqual(self.latest()['print_label'],'NEW ORDER')
        with self.app.app_context():
            first=db.session.get(CafeOrder,self.oid)
            tid=first.table_id
            second=CafeOrder(table_id=first.table_id,ordered_by_user_id=self.uid,status='pending_approval')
            db.session.add(second);db.session.flush()
            line=CafeOrderItem(order_id=second.id,menu_item_id=self.mid,quantity=3,unit_price=100,approval_status='pending',size_label='Regular')
            db.session.add(line);db.session.commit();sid=second.id;lid=line.id
        self.client.post(f'/cafe/orders/{sid}/approve',data={'pending_item_ids':[str(lid)],'decision':'approve'})
        self.assertEqual((self.latest()['table_items'][0]['previous'],self.latest()['table_items'][0]['current']),(2,5))
        self.edit(self.form({self.ids[0]:1}))
        item=self.latest()['table_items'][0]
        self.assertEqual((item['previous'],item['current']),(5,4))
        self.assertEqual(item['hindi_name'],'चाय')
        with self.app.app_context():
            for order in CafeOrder.query.all():order.status='paid';order.paid_at=datetime.utcnow()
            db.session.commit()
            fresh=CafeOrder(table_id=tid,ordered_by_user_id=self.uid,status='pending_approval')
            db.session.add(fresh);db.session.flush()
            self.assertFalse(table_state(fresh)['has_ticket'])
