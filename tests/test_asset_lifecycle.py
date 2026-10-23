import json
import unittest
from tempfile import TemporaryDirectory
from uuid import uuid4
from flask import Flask
from app.extensions import db
from app.models import User, ReusableInventoryAsset, ReusableInventoryPurchase, ReusableInventoryLossAllocation
from app.asset_lifecycle_models import AssetMovement, AssetBatch
from app import asset_lifecycle as flow


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.app=Flask(__name__,instance_path=self.temp.name)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',SECRET_KEY='test',TESTING=True)
        db.init_app(self.app); self.ctx=self.app.app_context(); self.ctx.push(); db.create_all()
        self.admin=User(full_name='Admin',email='admin@test',role='admin',active=True,password_hash='x')
        self.staff=User(full_name='Staff',email='staff@test',role='staff',active=True,password_hash='x')
        self.asset=ReusableInventoryAsset(name='Cups',purchased_quantity=10,current_quantity=8,average_unit_price=100,area_scope='cafe')
        db.session.add_all([self.admin,self.staff,self.asset]); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def act(self,action,actor=None,**data):
        result=flow.execute(self.asset,dict(action=action,reason='Test evidence',**data),actor or self.admin,uuid4().hex)
        db.session.commit()
        values=flow.state(self.asset)
        self.assertEqual(values['total'],sum(v for k,v in values.items() if k!='total'))
        self.assertEqual(self.asset.current_quantity,values['store']+values['in_use'])
        return result

    def test_full_lifecycle_recovery_and_repeat_batches(self):
        self.act('initialize',store='5')
        self.assertEqual(flow.state(self.asset)['in_use'],3)
        self.assertEqual(ReusableInventoryLossAllocation.query.count(),0)
        self.act('order',quantity='20',unit_price='100',payment_mode='upi',funding_source='cafe_operations')
        self.assertEqual(self.asset.current_quantity,8)
        batch=AssetBatch.query.one()
        self.act('load',batch_id=str(batch.id))
        transit=self.act('receive',batch_id=str(batch.id),quantity='18',confirmed='yes')
        self.assertEqual(flow.state(self.asset)['store'],23)
        self.assertEqual(ReusableInventoryLossAllocation.query.count(),0)
        self.act('dispatch',quantity='10')
        reported=self.act('reported',actor=self.staff,quantity='2')
        charge=ReusableInventoryLossAllocation.query.one()
        self.assertEqual(charge.charge_amount,100)
        self.act('wear',quantity='3',source='in_use')
        self.assertEqual(ReusableInventoryLossAllocation.query.count(),1)
        recovered=self.act('recover',quantity='1',origin_id=str(reported.id))
        self.assertIn('charge_review',json.loads(recovered.details_json))
        self.assertEqual(charge.charge_amount,100)
        self.assertEqual(flow.recoverable(reported),1)
        self.act('recover',quantity='1',origin_id=str(transit.id))
        self.act('return',quantity='1')
        self.act('order',quantity='5',unit_price='120',payment_mode='cash',funding_source='owner_personal')
        self.assertEqual(self.asset.purchased_quantity,35)
        self.assertEqual(ReusableInventoryPurchase.query.count(),2)
        s=flow.state(self.asset)
        self.act('count',store=str(s['store']),in_use=str(s['in_use']))

    def test_invalid_transitions_permissions_and_over_recovery(self):
        self.act('initialize',store='8')
        opening=AssetMovement.query.one()
        for action,data,actor in [
            ('initialize',{'store':'0'},self.admin),
            ('dispatch',{'quantity':'9'},self.admin),
            ('reported',{'quantity':'1'},self.staff),
            ('wear',{'quantity':'1','source':'store'},self.staff),
            ('recover',{'quantity':'3','origin_id':str(opening.id)},self.admin),
            ('count',{'store':'7','in_use':'0'},self.admin),
        ]:
            with self.assertRaises(ValueError): self.act(action,actor=actor,**data)
            db.session.rollback()
        self.act('order',quantity='5',unit_price='10',payment_mode='card',funding_source='cafe_operations')
        batch=AssetBatch.query.one()
        with self.assertRaises(ValueError): self.act('receive',batch_id=str(batch.id),quantity='5',confirmed='yes')
        db.session.rollback()
        self.act('load',batch_id=str(batch.id))
        with self.assertRaises(ValueError): self.act('receive',batch_id=str(batch.id),quantity='6',confirmed='yes')
        db.session.rollback()
        self.act('receive',batch_id=str(batch.id),quantity='0',confirmed='yes')
        with self.assertRaises(ValueError): self.act('receive',batch_id=str(batch.id),quantity='0',confirmed='yes')
        db.session.rollback()
        self.assertEqual(self.asset.current_quantity,8)

    def test_shortage_excludes_admin_and_wear_is_cafe_borne(self):
        self.act('initialize',store='0')
        self.act('shortage',quantity='2')
        charges=ReusableInventoryLossAllocation.query.all()
        self.assertTrue(all(c.user_id!=self.admin.id for c in charges))
        self.act('wear',quantity='2',source='in_use')
        self.assertEqual(ReusableInventoryLossAllocation.query.count(),len(charges))


class LifecycleRouteTests(unittest.TestCase):
    def test_csrf_replay_stale_and_access(self):
        from app import create_app
        from app.asset_lifecycle_views import revision
        with TemporaryDirectory() as folder:
            app=create_app(instance_path=folder,initialize_legacy_leaves=False)
            app.config['TESTING']=True
            with app.app_context():
                admin=User(full_name='Admin',email='admin@test',role='admin',active=True,password_hash='x')
                db.session.add(admin); db.session.commit(); uid=admin.id
            client=app.test_client(); url='/cafe/inventory/reusable-lifecycle'
            with client.session_transaction() as s:
                s['user_id']=uid; s['asset_lifecycle_csrf']='test'
            self.assertEqual(client.get(url).status_code,200)
            self.assertEqual(client.post(url,data={'action':'create'}).status_code,400)
            data=dict(action='create',name='Test Cups',csrf_token='test',request_key=uuid4().hex)
            response=client.post(url,data=data)
            self.assertEqual(response.status_code,302)
            client.post(url,data=data)
            with app.app_context():
                asset=ReusableInventoryAsset.query.one(); aid=asset.id; rev=revision(asset)
                self.assertEqual(AssetMovement.query.count(),1)
            self.assertEqual(client.get(url+f'?asset_id={aid}').status_code,200)
            data=dict(action='order',asset_id=aid,revision=rev,csrf_token='test',request_key=uuid4().hex,
                quantity='10',unit_price='20',payment_mode='upi',funding_source='cafe_operations',reason='Invoice')
            client.post(url,data=data)
            data['request_key']=uuid4().hex
            response=client.post(url,data=data,follow_redirects=True)
            self.assertIn(b'Stock changed',response.data)
            with app.app_context():
                self.assertEqual(ReusableInventoryPurchase.query.count(),1)
                staff=User(full_name='Staff',email='staff-route@test',role='staff',active=True,password_hash='x')
                db.session.add(staff);db.session.commit();sid=staff.id
                asset=db.session.get(ReusableInventoryAsset,aid);rev=revision(asset)
            # Legacy stock endpoints must not bypass separate store/in-use accounting.
            for action in ('add_reusable_purchase','count_reusable_asset','report_reusable_breakage'):
                response=client.post('/cafe/inventory?section=reusable_assets',data={'action':action,'asset_id':aid})
                self.assertIn('reusable-lifecycle',response.headers['Location'])
            with client.session_transaction() as s:s['user_id']=sid
            response=client.get(url+f'?asset_id={aid}')
            self.assertEqual(response.status_code,200)
            self.assertNotIn(b'Record purchased batch',response.data)
            self.assertNotIn(b'Weekly physical count',response.data)
            data.update(action='dispatch',revision=rev,request_key=uuid4().hex)
            response=client.post(url,data=data,follow_redirects=True)
            self.assertIn(b'Only inventory managers',response.data)
            with app.app_context():
                self.assertEqual(AssetMovement.query.count(),2)
                db.session.remove(); db.engine.dispose()
