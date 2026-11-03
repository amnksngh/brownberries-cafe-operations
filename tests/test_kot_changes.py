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
        self.assertTrue(self.poll().json['upgrade_required'])
        job=self.client.post('/api/kot-station/poll',json={'printers':['Kitchen Printer'],'protocol':2},headers={'Authorization':'Bearer agent-secret'}).json['job']
        self.assertEqual(job['ticket']['kind'],'ORDER CHANGE')

    def test_unclaimed_replaced_not_double_prepared(self):
        token=self.ticket();self.enqueue(token)
        self.edit(self.form({self.ids[0]:4}))
        with self.app.app_context():
            self.assertEqual(KotPrintJob.query.order_by(KotPrintJob.id).first().status,'superseded')
        self.assertEqual(self.client.get('/cafe/kot/'+token).status_code,409)
        ticket=self.latest()
        self.assertEqual(ticket['kind'],'REVISED ORDER - REPLACES QUEUED')
        self.assertEqual(sum(i['quantity'] for i in ticket['items']),6)

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

    def test_superseded_change_not_actionable_and_latest_full_list(self):
        from app.kot_printing import pending_changes
        self.enqueue(self.ticket())
        self.edit(self.form({self.ids[0]:4}))
        with self.app.app_context():
            old_id=KotOrderEdit.query.one().id
        self.edit(self.form({self.ids[0]:2}))
        with self.app.app_context():
            self.assertEqual(pending_changes().count(),1)
        self.assertEqual(self.client.post('/cafe/kot-changes',data={'csrf_token':'csrf-test','edit_id':old_id}).status_code,409)
        self.assertEqual(sum(i['quantity'] for i in self.latest()['items']),4)
