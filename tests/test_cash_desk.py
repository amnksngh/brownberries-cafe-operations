import json
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from uuid import uuid4
from flask import Flask
from werkzeug.datastructures import MultiDict
from app.extensions import db
from app.models import User, CafeTable, CafeOrder, CashCounterEntry, InventoryCategory, InventoryExpenseLog, InventoryPurchase
from app.cash_desk_models import CashDeskRecord, CashDeskSession, CashPaymentCorrection
from app import cash_desk as desk


class CashDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = Flask(__name__, instance_path=self.temp.name)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', SECRET_KEY='test', TESTING=True)
        db.init_app(self.app)
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.cashier = User(full_name='Cashier', email='c@test', password_hash='x', role='cashier', active=True)
        self.admin = User(full_name='Admin', email='a@test', password_hash='x', role='admin', active=True)
        self.table = CafeTable(name='Test', qr_slug='fixture')
        self.category = InventoryCategory(name='Test Expenses')
        db.session.add_all([self.cashier, self.admin, self.table, self.category]); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def act(self, action, actor=None, **data):
        if action == 'refill':
            data.setdefault('new_cash_confirmed', 'yes')
        form = MultiDict(dict(action=action, reason='Fixture evidence', **data))
        desk.execute(form, actor or self.cashier, uuid4().hex)
        db.session.commit()

    def order(self, method='UPI', total=100, group=None):
        row = CafeOrder(table_id=self.table.id, ordered_by_user_id=self.cashier.id, status='paid',
            paid_at=datetime.utcnow(), payment_type=method, total_amount=total, settlement_group_id=group)
        db.session.add(row); db.session.commit(); return row

    def test_open_refill_exchange_expense_handover_close(self):
        self.act('open', count_note_100='2')
        # Opening is evidence, not a deposit or sale.
        self.assertEqual(desk.snapshot()['total'], 0)
        self.act('refill', in_note_100='5')
        self.act('exchange', out_note_100='1', in_note_50='2')
        self.assertEqual(desk.snapshot()['total'], 500)
        self.act('expense', out_note_50='2', category_id=str(self.category.id))
        self.assertEqual(InventoryExpenseLog.query.one().amount, 100)
        self.act('handover', out_note_100='2')
        self.act('end_withdrawal', out_note_100='1')
        self.act('close', count_note_100='3')
        closing = json.loads(CashDeskSession.query.one().closing.details_json)
        self.assertEqual(closing['expected_from_opening'], 300)
        self.assertEqual(closing['session_variance'], 0)
        self.assertEqual(desk.day_summary(desk.today())['payments']['total_collected'], 0)
        self.assertEqual(CashDeskRecord.query.count(), 7)

    def test_invalid_exchange_and_overdraw_do_not_write(self):
        self.act('refill', in_note_100='2')
        before = CashCounterEntry.query.count()
        with self.assertRaises(ValueError): self.act('exchange', out_note_100='1', in_note_50='1')
        db.session.rollback()
        with self.assertRaises(ValueError): self.act('handover', out_note_100='3')
        db.session.rollback()
        self.assertEqual(CashCounterEntry.query.count(), before)

    def test_duplicate_open_close_blocked(self):
        self.act('open')
        with self.assertRaises(ValueError): self.act('open')
        db.session.rollback()
        self.act('close')
        with self.assertRaises(ValueError): self.act('close')
        db.session.rollback()
        self.assertEqual(CashDeskSession.query.count(), 1)

    def test_link_expense_once_and_no_duplicate_cost(self):
        self.act('refill', in_note_100='5')
        expense = InventoryExpenseLog(entry_date=desk.today(), category_id=self.category.id,
            amount=100, transaction_mode='cash', funding_source='cafe_operations')
        db.session.add(expense); db.session.commit()
        self.act('expense', out_note_100='1', expense_id=str(expense.id))
        with self.assertRaises(ValueError): self.act('expense', out_note_100='1', expense_id=str(expense.id))
        db.session.rollback()
        self.assertEqual(InventoryExpenseLog.query.count(), 1)
        self.assertEqual(desk.snapshot()['total'], 400)

    def test_purchase_link_does_not_create_expense(self):
        self.act('refill', in_note_100='2')
        purchase = InventoryPurchase(total_amount=100, payment_status='paid', payment_mode='cash', funding_source='cafe_operations')
        db.session.add(purchase); db.session.commit()
        self.act('expense', out_note_100='1', purchase_id=str(purchase.id))
        self.assertEqual(InventoryExpenseLog.query.count(), 0)
        with self.assertRaises(ValueError): self.act('expense', out_note_100='1', purchase_id=str(purchase.id))
        db.session.rollback()

    def test_upi_to_cash_is_approved_and_audited(self):
        order = self.order()
        original_date = order.paid_at
        self.act('request_correction', order_id=str(order.id), cash='100', in_note_100='1')
        request = CashPaymentCorrection.query.one()
        self.assertEqual(desk.snapshot()['total'], 0)
        with self.assertRaises(ValueError): self.act('approve', correction_id=str(request.id))
        db.session.rollback()
        self.act('approve', actor=self.admin, correction_id=str(request.id))
        self.assertEqual(desk.snapshot()['total'], 100)
        self.assertEqual(order.payment_type, 'Cash')
        self.assertEqual(order.paid_at, original_date)
        self.assertEqual(order.total_amount, 100)
        self.assertEqual(request.status, 'approved')
        with self.assertRaises(ValueError): self.act('approve', actor=self.admin, correction_id=str(request.id))
        db.session.rollback()

    def test_cash_to_card_reverses_tender_and_change_not_sales_total(self):
        order = self.order('Cash', 100)
        desk.movement(self.cashier, 'deposit', {'note_200':1}, 'Customer Payment', '', order)
        desk.movement(self.cashier, 'withdrawal', {'note_100':1}, 'Return to Customer', '', order)
        db.session.commit()
        self.act('request_correction', order_id=str(order.id), card='100', reference='Bank123')
        self.act('approve', actor=self.admin, correction_id=str(CashPaymentCorrection.query.one().id))
        self.assertEqual(desk.snapshot()['total'], 0)
        self.assertEqual(desk.snapshot()['tracked_cash'], 0)
        self.assertEqual(CashCounterEntry.query.count(), 4)
        self.assertEqual(CashCounterEntry.query.filter_by(is_deleted=True).count(), 0)

    def test_group_split_once_and_stale_request_rejected(self):
        first, second = self.order(total=60, group='settlement'), self.order(total=40, group='settlement')
        self.act('request_correction', order_id=str(second.id), cash='50', card='50', in_note_50='1', reference='123')
        self.act('approve', actor=self.admin, correction_id=str(CashPaymentCorrection.query.one().id))
        self.assertEqual(first.payment_breakdown_json, second.payment_breakdown_json)
        summary = desk.day_summary(desk.today())['payments']
        self.assertEqual(summary['total_collected'], 100)
        self.assertEqual({r['key']:r['amount'] for r in summary['rows']}, {'cash':50, 'card':50, 'upi':0})
        self.act('request_correction', order_id=str(first.id), upi='100', reference='UPI1')
        first.payment_reference = 'concurrent change'; db.session.commit()
        with self.assertRaises(ValueError): self.act('approve', actor=self.admin, correction_id=str(CashPaymentCorrection.query.order_by(CashPaymentCorrection.id.desc()).first().id))
        db.session.rollback()

    def test_own_request_cannot_be_approved_and_rejection_preserves_payment(self):
        order = self.order()
        self.act('request_correction', actor=self.admin, order_id=str(order.id), cash='100', in_note_100='1')
        with self.assertRaises(ValueError): self.act('approve', actor=self.admin, correction_id=str(CashPaymentCorrection.query.one().id))
        db.session.rollback()
        self.assertEqual(order.payment_type, 'UPI')

    def test_bad_amounts_and_denominations_rejected(self):
        for amount in ('NaN', 'Infinity', '-1', '1.001'):
            with self.assertRaises(ValueError): desk.money(amount)
        for count in ('-1', '1.5', 'abc', '100001'):
            with self.assertRaises(ValueError): desk.counts({'in_note_100':count}, 'in_')

    def test_every_count_preserves_existing_cash_even_with_discrepancies(self):
        self.act('refill', in_note_200='9', in_note_100='11', in_coin_5='22')
        before = desk.snapshot()
        entries = CashCounterEntry.query.count()
        for action, count in [('open','9'), ('count','0'), ('count','99'), ('close','1')]:
            self.act(action, operation_scope='count', count_note_200=count,
                     in_note_200='100', out_coin_5='22', new_cash_confirmed='yes')
            self.assertEqual(desk.snapshot(), before)
            self.assertEqual(CashCounterEntry.query.count(), entries)

    def test_refill_requires_new_cash_confirmation_and_cannot_use_count_form(self):
        for data in ({'new_cash_confirmed':''}, {'operation_scope':'count'}):
            with self.assertRaises(ValueError):
                self.act('refill', in_note_200='9', **data)
            db.session.rollback()
        self.assertEqual(CashCounterEntry.query.count(), 0)

    def test_reconcile_legacy_gap_preserves_sales_and_originals(self):
        order = self.order('Cash', 750)
        desk.movement(self.cashier, 'deposit', {}, 'Legacy cash', '', order, amount=750)
        db.session.commit()
        self.act('reconcile', actor=self.admin, confirmed='yes', confirmed_adjustment='-750')
        self.assertEqual(desk.snapshot()['total'], 0)
        self.assertEqual(desk.snapshot()['unallocated'], 0)
        self.assertEqual(order.total_amount, 750)
        self.assertEqual(order.payment_type, 'Cash')
        self.assertEqual(CashCounterEntry.query.filter_by(is_deleted=True).count(), 0)

    def test_reconcile_denominations_and_permissions(self):
        desk.movement(self.cashier, 'deposit', {}, 'Legacy cash', '', amount=100)
        db.session.commit()
        for actor, confirmation in ((self.cashier, '0'), (self.admin, '100')):
            with self.assertRaises(ValueError):
                self.act('reconcile', actor=actor, count_note_100='1', confirmed='yes', confirmed_adjustment=confirmation)
            db.session.rollback()
        self.act('reconcile', actor=self.admin, count_note_100='1', confirmed='yes', confirmed_adjustment='0')
        self.assertEqual(desk.snapshot()['tracked_cash'], 100)
        self.assertEqual(desk.snapshot()['unallocated'], 0)

    def test_reconciliation_does_not_hide_session_shortage(self):
        self.act('open')
        self.act('refill', in_note_100='2')
        self.act('reconcile', actor=self.admin, count_note_100='1', confirmed='yes', confirmed_adjustment='-100')
        self.act('close', count_note_100='1')
        closing = json.loads(CashDeskSession.query.one().closing.details_json)
        self.assertEqual(closing['session_variance'], -100)
        self.assertEqual(closing['reconciliation_adjustment'], -100)

    def test_closing_evidence_survives_later_correction(self):
        order = self.order()
        self.act('open'); self.act('close')
        frozen = CashDeskSession.query.one().closing.details_json
        self.act('request_correction', order_id=str(order.id), cash='100', in_note_100='1')
        self.act('approve', actor=self.admin, correction_id=str(CashPaymentCorrection.query.one().id))
        self.assertEqual(CashDeskSession.query.one().closing.details_json, frozen)

    def test_legacy_unallocated_cash_and_repeated_corrections(self):
        order = self.order('Cash')
        desk.movement(self.cashier, 'deposit', {}, 'Cash received', '', order, amount=100)
        db.session.commit()
        self.assertEqual(desk.snapshot()['unallocated'], 100)
        for method in ('upi', 'cash', 'upi', 'cash'):
            data = {'order_id':str(order.id), method:'100', 'reference':'bank-ref'}
            if method == 'cash': data['in_note_100'] = '1'
            self.act('request_correction', **data)
            correction = CashPaymentCorrection.query.order_by(CashPaymentCorrection.id.desc()).first()
            self.act('approve', actor=self.admin, correction_id=str(correction.id))
            self.assertEqual(desk.snapshot()['total'], 100 if method == 'cash' else 0)
            self.assertEqual(desk.snapshot()['unallocated'], 0)
        self.assertLessEqual(CashCounterEntry.query.count(), 6)

    def test_rejected_request_does_not_change_cash_or_sales(self):
        order = self.order()
        self.act('request_correction', order_id=str(order.id), cash='100', in_note_100='1')
        self.act('reject', actor=self.admin, correction_id=str(CashPaymentCorrection.query.one().id))
        self.assertEqual(order.payment_type, 'UPI')
        self.assertEqual(desk.snapshot()['total'], 0)
        self.assertEqual(CashPaymentCorrection.query.one().status, 'rejected')


class CashDeskRouteTests(unittest.TestCase):
    def test_ist_display_does_not_mutate_audit(self):
        from app.cash_desk_views import cash_ist, cash_audit_ist
        self.assertEqual(cash_ist(datetime(2026,10,11,20,0)), '12 Oct 2026, 01:30:00 AM IST')
        original = {'before':[{'paid_at':'2026-10-11 20:00:00'}]}
        self.assertIn('12 Oct 2026', cash_audit_ist(original)['before'][0]['paid_at'])
        self.assertEqual(original['before'][0]['paid_at'], '2026-10-11 20:00:00')

    def test_http_access_csrf_idempotency_and_render(self):
        from app import create_app
        with TemporaryDirectory() as folder:
            app = create_app(instance_path=folder, initialize_legacy_leaves=False)
            app.config['TESTING'] = True
            with app.app_context():
                user = User(full_name='Fixture Cashier', email='cash@test', password_hash='x', role='cashier', active=True)
                staff = User(full_name='Fixture Staff', email='staff@test', password_hash='x', role='staff', active=True)
                db.session.add_all([user, staff]); db.session.commit()
                uid, sid = user.id, staff.id
            client = app.test_client(); url = '/cafe/cash-counter/control'
            with client.session_transaction() as s: s['user_id'] = sid
            self.assertEqual(client.get(url).status_code, 403)
            with client.session_transaction() as s:
                s['user_id'] = uid; s['cash_desk_csrf'] = 'test'
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Daily Cash Control', response.data)
            self.assertEqual(client.post(url, data={'action':'refill'}).status_code, 400)
            with app.app_context():
                desk.execute(MultiDict({'action':'open','reason':'Test opening'}), db.session.get(User,uid), uuid4().hex)
                db.session.commit()
                revision = desk.revision()
            data = dict(action='refill', new_cash_confirmed='yes', csrf_token='test', request_key=uuid4().hex, revision=revision, reason='Morning float', in_note_100='2')
            self.assertEqual(client.post(url, data=data).status_code, 302)
            self.assertEqual(client.post(url, data=data).status_code, 302)
            data['request_key'] = uuid4().hex
            self.assertEqual(client.post(url, data=data).status_code, 302)
            with app.app_context():
                self.assertEqual(CashDeskRecord.query.count(), 2)
                self.assertEqual(desk.snapshot()['total'], 200)
                table = CafeTable(name='Guard fixture', qr_slug='guard-fixture')
                db.session.add(table); db.session.flush()
                order = CafeOrder(table_id=table.id, ordered_by_user_id=uid, status='paid',
                                  paid_at=datetime.utcnow(), payment_type='Cash', total_amount=100)
                db.session.add(order); db.session.flush()
                entry = CashCounterEntry(entry_type='deposit', amount=100, reason='Customer Payment',
                    denominations_json='{"note_100":1}', source_order_id=order.id)
                db.session.add(entry); db.session.commit()
                oid, eid = order.id, entry.id
            # The old paid endpoint cannot bypass correction approval.
            response = client.post(f'/cafe/orders/{oid}/mark-paid', data={'payment_type':'UPI'})
            self.assertEqual(response.status_code, 302)
            with app.app_context():
                self.assertEqual(db.session.get(CafeOrder, oid).payment_type, 'Cash')
            # Existing counter now has CSRF; settlement entries cannot be deleted.
            counter = client.get('/cafe/cash-counter')
            self.assertEqual(counter.status_code, 200)
            self.assertNotIn(b'id="cash-counter-form"', counter.data)
            self.assertNotIn(b'Unreconciled ledger difference', counter.data)
            with client.session_transaction() as s: token = s['cash_counter_csrf']
            with app.app_context(): before = CashCounterEntry.query.count()
            retired = client.post('/cafe/cash-counter', data={'csrf_token':token,'entry_type':'deposit','note_100':'1'})
            self.assertTrue(retired.headers['Location'].endswith('/cash-counter/control'))
            with app.app_context(): self.assertEqual(CashCounterEntry.query.count(), before)
            self.assertEqual(client.post('/cafe/cash-counter', data={'entry_type':'deposit'}).status_code, 400)
            response = client.post(f'/cafe/cash-counter/{eid}/delete', data={'csrf_token':token})
            self.assertEqual(response.status_code, 302)
            with app.app_context():
                self.assertFalse(db.session.get(CashCounterEntry, eid).is_deleted)
                db.session.remove(); db.engine.dispose()
