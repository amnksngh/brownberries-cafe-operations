"""Table-wide kitchen quantities used by the compact print protocol."""
from datetime import datetime
from .extensions import db
from .models import CafeOrder, CafeKitchenTicket

HINDI_REASONS = {
 'More Drinking Water Needed!': 'और पीने का पानी चाहिए',
 'Customer does not have enough time': 'ग्राहक के पास पर्याप्त समय नहीं है',
 'Customer wants to change the order (preparation not started)': 'ग्राहक ऑर्डर बदलना चाहता है (बनाना शुरू नहीं हुआ)',
 'There was some issue with the item': 'व्यंजन में समस्या थी',
 'Customer wants more!': 'ग्राहक को और चाहिए',
 'Customer wants less!': 'ग्राहक को कम चाहिए',
 'Customer is blacklisted due to prior behaviour': 'पिछले व्यवहार के कारण ग्राहक प्रतिबंधित है',
 'Staff entered the wrong item or quantity': 'कर्मचारी ने गलत व्यंजन या मात्रा दर्ज की',
 'Item is unavailable': 'व्यंजन उपलब्ध नहीं है',
 'Customer changed dine-in / parcel preference': 'ग्राहक ने यहाँ खाने या पैक कराने का विकल्प बदला',
}

def table_state(order):
    from .cafe import _include_in_kot
    scope = CafeOrder.table_id == order.table_id if order.table_id is not None else CafeOrder.id == order.id
    orders = CafeOrder.query.filter(scope,
        CafeOrder.status.notin_(['paid', 'cancelled'])).order_by(CafeOrder.id).all()
    # Include the current cancelled order for determining whether this table has
    # already received a KOT, but never include its cancelled quantities.
    last_paid = db.session.query(db.func.max(CafeOrder.paid_at)).filter(
        scope, CafeOrder.status == 'paid').scalar()
    cancelled = CafeOrder.query.filter(scope,
        CafeOrder.status == 'cancelled')
    if last_paid:
        cancelled = cancelled.filter(CafeOrder.created_at > last_paid)
    ids = [row.id for row in orders] + [row.id for row in cancelled.all()]
    if order.status != 'paid' and order.id not in ids:
        ids.append(order.id)
    has_ticket = bool(ids and CafeKitchenTicket.query.filter(CafeKitchenTicket.order_id.in_(ids)).first())
    totals = {}
    for row in orders:
        for line in row.order_items:
            item = line.menu_item
            if not item or line.approval_status != 'approved' or not _include_in_kot(item):
                continue
            key = (item.id, line.size_label or '', bool(line.is_parcel))
            entry = totals.setdefault(key, dict(name=item.name, hindi_name=item.hindi_name or '',
                size=line.size_label or '', parcel=bool(line.is_parcel), quantity=0))
            entry['quantity'] += int(line.quantity or 0)
    return dict(totals=totals, has_ticket=has_ticket)

def compact_payload(order, before, reason=''):
    from .cafe import _format_ist
    after = table_state(order)
    lines = []
    for key in dict.fromkeys([*before['totals'], *after['totals']]):
        old = before['totals'].get(key, {}).get('quantity', 0)
        new = after['totals'].get(key, {}).get('quantity', 0)
        if old == new:
            continue
        row = dict(after['totals'].get(key) or before['totals'][key])
        row.update(previous=old, current=new, delta=new-old, quantity=new,
            cancelled=new == 0)
        lines.append(row)
    label = 'NEW ORDER' if not before['has_ticket'] else 'RUNNING'
    if before['has_ticket']:
        label += ' (CANCELLED)' if lines and all(x['delta'] < 0 for x in lines) and all(x['current']==0 for x in lines) else ' (MODIFIED)'
    return dict(print_label=label, print_time=_format_ist(datetime.utcnow(), '%I:%M:%S %p'),
        table_items=lines, reason_hindi=HINDI_REASONS.get(reason, 'ऑर्डर में बदलाव' if reason else ''), protocol=3)
