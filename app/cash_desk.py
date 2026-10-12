"""Cash controls: counted cash is evidence, not an automatic ledger adjustment."""
import hashlib
import json
from datetime import datetime, timedelta, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .extensions import db
from .models import CashCounterEntry, CafeOrder, InventoryExpenseLog, InventoryCategory, InventoryPurchase
from .cash_desk_models import CashDeskRecord, CashDeskSession, CashPaymentCorrection
from .payment_summary import summarize_payments

IST = ZoneInfo('Asia/Kolkata')


def today():
    return datetime.now(IST).date()


def money(value):
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Enter a valid amount.')
    if not value.is_finite() or value < 0 or value > 10000000 or value != value.quantize(Decimal('.01')):
        raise ValueError('Amounts must be non-negative, at most 10,000,000, and use at most two decimal places.')
    return value


def counts(form, prefix):
    from .cafe import CASH_DENOMINATION_VALUES
    result = {}
    for key in CASH_DENOMINATION_VALUES:
        try:
            quantity = int(form.get(prefix + key, '0') or '0')
        except (ValueError, TypeError):
            raise ValueError('Denominations must be whole-number counts.')
        if not 0 <= quantity <= 100000:
            raise ValueError('Denomination counts must be between 0 and 100,000.')
        if quantity:
            result[key] = quantity
    return result


def value(denominations):
    from .cafe import CASH_DENOMINATION_VALUES
    return sum(Decimal(CASH_DENOMINATION_VALUES[key]) * quantity for key, quantity in denominations.items())


def snapshot():
    from .cafe import _cash_counter_snapshot
    return _cash_counter_snapshot()


def revision():
    # Detect concurrent cash writes, reversals/deletions, or another control action.
    ledger = db.session.query(CashCounterEntry.id, CashCounterEntry.is_deleted).order_by(CashCounterEntry.id).all()
    latest = db.session.query(db.func.max(CashDeskRecord.id)).scalar() or 0
    return hashlib.sha256(repr((ledger, latest)).encode()).hexdigest()


def record(kind, actor, key, details):
    row = CashDeskRecord(kind=kind, actor_id=actor.id, request_key=key,
                        details_json=json.dumps(details, default=str))
    db.session.add(row)
    db.session.flush()
    return row


def movement(actor, direction, denominations, reason, note, order=None, amount=None):
    row = CashCounterEntry(entry_type=direction,
        amount=float(value(denominations) if amount is None else amount),
        denominations_json=json.dumps(denominations), reason=reason, note=note[:500],
        created_by_user_id=actor.id, source_order_id=order.id if order else None,
        table_id=order.table_id if order else None)
    db.session.add(row)
    db.session.flush()
    return row.id


def require_available(denominations):
    state = snapshot()
    if any(quantity > state['counts'].get(key, 0) for key, quantity in denominations.items()):
        raise ValueError('The withdrawal exceeds recorded notes/coins. Reconcile the drawer first.')
    if value(denominations) > Decimal(str(state['total'])):
        raise ValueError('The withdrawal exceeds the ledger balance.')


def group_orders(order_id):
    order = db.session.get(CafeOrder, order_id)
    if not order or order.status != 'paid':
        raise ValueError('Choose a settled order.')
    if order.settlement_group_id:
        return CafeOrder.query.filter_by(settlement_group_id=order.settlement_group_id).order_by(CafeOrder.id).all()
    # Old split rows cannot safely identify a shared settlement.
    if (order.payment_type or '').lower() == 'split payment':
        raise ValueError('This legacy split lacks a settlement identifier; it needs a separate historical review.')
    return [order]


def payment_state(orders):
    return [dict(id=o.id, total=o.total_amount, status=o.status, method=o.payment_type,
                 reference=o.payment_reference, breakdown=o.payment_breakdown_json,
                 paid_at=str(o.paid_at)) for o in orders]


def day_bounds(day):
    start = datetime.combine(day, time.min, IST).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def day_summary(day):
    start, end = day_bounds(day)
    entries = CashCounterEntry.query.filter(CashCounterEntry.is_deleted.is_(False),
        CashCounterEntry.occurred_at >= start, CashCounterEntry.occurred_at < end).all()
    orders = CafeOrder.query.filter(CafeOrder.status == 'paid', CafeOrder.paid_at >= start, CafeOrder.paid_at < end).all()
    payments = summarize_payments([{'order': o} for o in orders])
    groups = {}
    for entry in entries:
        # Movement labels are separate from sales; payments above are the sales authority.
        label = entry.reason if not entry.source_order_id else ('Payment corrections' if entry.reason.startswith('Payment correction') else 'Customer tender / change')
        groups[label] = round(groups.get(label, 0) + (entry.amount if entry.entry_type == 'deposit' else -entry.amount), 2)
    tender = sum(e.amount for e in entries if e.source_order_id and e.entry_type == 'deposit' and not e.reason.startswith('Payment correction'))
    change = sum(e.amount for e in entries if e.source_order_id and e.entry_type == 'withdrawal' and not e.reason.startswith('Payment correction'))
    return dict(payments=payments, movements=groups, tender=round(tender, 2), change=round(change, 2),
                net_movement=round(sum(e.amount if e.entry_type == 'deposit' else -e.amount for e in entries), 2))


def execute(form, actor, key):
    action = form.get('action')
    reason = (form.get('reason') or '').strip()
    if not reason or len(reason) > 180:
        raise ValueError('A reason/reference of 1–180 characters is required.')
    if action in ('open', 'close', 'count'):
        session = CashDeskSession.query.filter_by(business_date=today()).first()
        if action == 'open' and (session or CashDeskSession.query.filter_by(closing_id=None).first()):
            raise ValueError('A session already exists or a prior day remains open. Close it first.')
        if action == 'close':
            session = CashDeskSession.query.filter_by(closing_id=None).first()
            if not session:
                raise ValueError('There is no open session to close.')
        counted = counts(form, 'count_')
        state = snapshot()
        details = dict(reason=reason, counted=counted, counted_total=float(value(counted)), ledger=state,
                       variance=round(float(value(counted))-state['total'], 2))
        if action == 'close':
            opening = json.loads(session.opening.details_json)
            details['opening_total'] = opening['counted_total']
            details['ledger_change'] = round(state['total'] - opening['ledger']['total'], 2)
            details['expected_from_opening'] = round(opening['counted_total'] + details['ledger_change'], 2)
            details['session_variance'] = round(details['counted_total']-details['expected_from_opening'], 2)
            details['business_date'] = str(session.business_date)
            details['summary'] = day_summary(session.business_date)
        row = record(action, actor, key, details)
        if action == 'open':
            db.session.add(CashDeskSession(business_date=today(), opening_id=row.id))
        elif action == 'close':
            session.closing_id = row.id
        return
    if action in ('refill', 'expense', 'handover', 'end_withdrawal', 'exchange'):
        incoming, outgoing = counts(form, 'in_'), counts(form, 'out_')
        if action == 'refill':
            outgoing = {}
        elif action != 'exchange':
            incoming = {}
        if action == 'exchange' and (value(incoming) != value(outgoing) or not value(incoming)):
            raise ValueError('Exchange must give and receive exactly the same non-zero value.')
        if not value(incoming) and not value(outgoing):
            raise ValueError('Enter the notes/coins moved.')
        if outgoing:
            require_available(outgoing)
        expense_id = None
        purchase_id = None
        if action == 'expense':
            expense_id = int(form.get('expense_id') or 0)
            purchase_id = int(form.get('purchase_id') or 0)
            if expense_id and purchase_id:
                raise ValueError('Link either an expense or a purchase, not both.')
            if purchase_id:
                purchase = db.session.get(InventoryPurchase, purchase_id)
                if (not purchase or purchase.payment_status != 'paid' or (purchase.payment_mode or '').lower() != 'cash'
                        or purchase.funding_source != 'cafe_operations' or money(purchase.total_amount) != value(outgoing)):
                    raise ValueError('Purchase must be paid in Cash from cafe operations for exactly this amount.')
                for old in CashDeskRecord.query.filter_by(kind='expense').all():
                    if json.loads(old.details_json).get('purchase_id') == purchase_id:
                        raise ValueError('This purchase already has a cash withdrawal.')
            elif expense_id:
                expense = db.session.get(InventoryExpenseLog, expense_id)
                if (not expense or (expense.transaction_mode or '').lower() != 'cash'
                        or expense.funding_source != 'cafe_operations'
                        or money(expense.amount) != value(outgoing)):
                    raise ValueError('Linked expense must be a cash expense funded by cafe operations for exactly this amount.')
                for old in CashDeskRecord.query.filter_by(kind='expense').all():
                    if json.loads(old.details_json).get('expense_id') == expense_id:
                        raise ValueError('This expense already has a cash withdrawal.')
            else:
                category = db.session.get(InventoryCategory, int(form.get('category_id') or 0))
                if not category:
                    raise ValueError('Select an expense category or link an existing expense ID.')
                expense = InventoryExpenseLog(entry_date=today(), category_id=category.id,
                    amount=float(value(outgoing)), transaction_mode='cash', funding_source='cafe_operations',
                    note=reason, created_by_user_id=actor.id)
                db.session.add(expense)
                db.session.flush()
                expense_id = expense.id
        label = {'refill':'Morning / operating float', 'expense':'Cafe Expenses',
                 'handover':'Handover to Owner', 'end_withdrawal':'End-of-day withdrawal',
                 'exchange':'Denomination exchange'}[action]
        ids = []
        if outgoing:
            ids.append(movement(actor, 'withdrawal', outgoing, label, reason))
        if incoming:
            ids.append(movement(actor, 'deposit', incoming, label, reason))
        record(action, actor, key, dict(reason=reason, incoming=incoming, outgoing=outgoing,
                                      entry_ids=ids, expense_id=expense_id, purchase_id=purchase_id))
        return
    if action == 'request_correction':
        orders = group_orders(int(form.get('order_id') or 0))
        if any(o.status != 'paid' for o in orders):
            raise ValueError('All orders in the settlement must still be paid.')
        total = sum(money(o.total_amount) for o in orders)
        if total <= 0:
            raise ValueError('There is no collected payment to correct for this settlement.')
        amounts = {method: money(form.get(method) or 0) for method in ('cash', 'card', 'upi')}
        if sum(amounts.values()) != total:
            raise ValueError(f'Corrected Cash + Card + UPI must equal the complete settlement total ₹{total}.')
        incoming, outgoing = counts(form, 'in_'), counts(form, 'out_')
        if value(incoming)-value(outgoing) != amounts['cash'] or (not amounts['cash'] and (incoming or outgoing)):
            raise ValueError('Enter the actual cash received and change given for this settlement; net must equal corrected Cash.')
        reference = (form.get('reference') or '').strip()
        if (amounts['card'] or amounts['upi']) and not reference:
            raise ValueError('A card/UPI transaction reference is required.')
        row = record(action, actor, key, dict(reason=reason, before=payment_state(orders),
            amounts={m:float(a) for m,a in amounts.items()}, incoming=incoming, outgoing=outgoing, reference=reference[:120]))
        db.session.add(CashPaymentCorrection(order_id=orders[0].id, request_id=row.id))
        return
    if action in ('approve', 'reject'):
        if not (actor.has_role('admin') or actor.has_role('owner')):
            raise ValueError('Only an admin or owner can approve or reject payment corrections.')
        correction = db.session.get(CashPaymentCorrection, int(form.get('correction_id') or 0))
        if not correction or correction.status != 'pending':
            raise ValueError('This request has already been decided or no longer exists.')
        if correction.request_record.actor_id == actor.id:
            raise ValueError('A different admin or owner must review your own request.')
        details = json.loads(correction.request_record.details_json)
        ids = []
        if action == 'approve':
            orders = group_orders(correction.order_id)
            if payment_state(orders) != details['before']:
                raise ValueError('The settlement changed since this request. Reject it and submit a fresh request.')
            existing = CashCounterEntry.query.filter(CashCounterEntry.source_order_id.in_([o.id for o in orders]),
                                                      CashCounterEntry.is_deleted.is_(False)).all()
            # Reverse the net settlement position, not every historic reversal again.
            # This bounds ledger growth across repeated corrections and retains all originals.
            net_counts = {}
            net_amount = Decimal(0)
            for entry in existing:
                sign = 1 if entry.entry_type == 'deposit' else -1
                net_amount += sign * Decimal(str(entry.amount))
                for denomination, quantity in json.loads(entry.denominations_json or '{}').items():
                    net_counts[denomination] = net_counts.get(denomination, 0) + sign * int(quantity)
            reverse_out = {k:q for k,q in net_counts.items() if q > 0}
            reverse_in = {k:-q for k,q in net_counts.items() if q < 0}
            if reverse_out:
                ids.append(movement(actor, 'withdrawal', reverse_out, 'Payment correction reversal', reason, orders[0]))
            if reverse_in:
                ids.append(movement(actor, 'deposit', reverse_in, 'Payment correction reversal', reason, orders[0]))
            unallocated = net_amount - (value(reverse_out) - value(reverse_in))
            if unallocated:
                ids.append(movement(actor, 'withdrawal' if unallocated > 0 else 'deposit', {},
                    'Payment correction reversal', reason, orders[0], abs(unallocated)))
            incoming, outgoing = details['incoming'], details['outgoing']
            if incoming:
                ids.append(movement(actor, 'deposit', incoming, 'Payment correction tender', reason, orders[0]))
            if outgoing:
                ids.append(movement(actor, 'withdrawal', outgoing, 'Payment correction change', reason, orders[0]))
            rows = [{'method': m.title() if m != 'upi' else 'UPI', 'amount': a, 'reference': details['reference']}
                    for m,a in details['amounts'].items() if a]
            for order in orders:
                order.payment_type = rows[0]['method'] if len(rows) == 1 else 'Split Payment'
                order.payment_breakdown_json = json.dumps(rows)
                order.payment_reference = details['reference']
        decision = record(action, actor, key, dict(reason=reason, correction_id=correction.id, entry_ids=ids))
        correction.status = 'approved' if action == 'approve' else 'rejected'
        correction.decision_id = decision.id
        return
    raise ValueError('Unknown cash operation.')
