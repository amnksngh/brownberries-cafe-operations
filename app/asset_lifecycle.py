"""Reusable stock accounting. Call inside a transaction; never erase historical entries."""
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from .extensions import db
from .models import (ReusableInventoryAsset, ReusableInventoryPurchase, ReusableInventoryCount,
                     ReusableInventoryLossEvent, User)
from .asset_lifecycle_models import AssetStock, AssetBatch, AssetMovement
from .cash_desk import today


def manager(user):
    return bool(user and user.active and any(user.has_role(r) for r in ('admin','owner','manager','inventory_manager')))


def integer(value, minimum=0):
    try:
        result = int(str(value))
    except (ValueError, TypeError):
        raise ValueError('Use a whole-number quantity.')
    if not minimum <= result <= 10000000:
        raise ValueError(f'Quantity must be between {minimum} and 10,000,000.')
    return result


def state(asset):
    stock = asset.lifecycle
    if stock is None:
        return None
    pending = {'ordered':0, 'transit':0}
    for batch in asset.delivery_batches:
        if batch.status in pending:
            pending[batch.status] += batch.purchase.quantity
    return dict(total=asset.purchased_quantity, store=stock.store, in_use=stock.in_use,
                broken=stock.broken, unusable=stock.unusable, **pending)


def validate(asset):
    values = state(asset)
    if any(v < 0 for v in values.values()) or values['total'] != sum(v for k,v in values.items() if k != 'total'):
        raise ValueError('Stock does not balance. No movement was saved; review the opening quantities.')
    asset.current_quantity = values['store'] + values['in_use']


def recoverable(movement):
    recovered = db.session.query(db.func.coalesce(db.func.sum(AssetMovement.quantity),0)).filter_by(recovery_of_id=movement.id).scalar()
    return movement.quantity - int(recovered)


def execute(asset, form, actor, key):
    from .cafe import _record_reusable_loss_event, _is_staff_user
    action = form.get('action')
    if not actor.active or (not manager(actor) and action != 'reported'):
        raise ValueError('Only inventory managers, managers, owners and admins can manage stock.')
    if not asset.active:
        raise ValueError('This item is inactive.')
    note = (form.get('reason') or '').strip()
    if not note or len(note) > 180:
        raise ValueError('Enter a reason/reference of 1–180 characters.')
    before = state(asset)
    stock = asset.lifecycle
    quantity = integer(form.get('quantity', '0'))
    loss = None
    origin = None
    details = {'reason':note, 'before':before}
    if action == 'initialize':
        if stock:
            raise ValueError('This asset already has an opening split.')
        if not 0 <= asset.current_quantity <= asset.purchased_quantity:
            raise ValueError('Existing totals are inconsistent; review them before enabling lifecycle tracking.')
        store = integer(form.get('store'))
        if store > asset.current_quantity:
            raise ValueError('Store stock cannot exceed current usable stock.')
        stock = AssetStock(asset=asset, version=1, store=store, in_use=asset.current_quantity-store,
                           broken=asset.purchased_quantity-asset.current_quantity, unusable=0)
        db.session.add(stock)
        # The opening loss bucket is a historical snapshot, not a new staff charge.
        quantity = stock.broken
    elif not stock:
        raise ValueError('Set the opening store/in-use split first.')
    elif action == 'order':
        quantity = integer(form.get('quantity'), 1)
        try:
            price = Decimal(form.get('unit_price', ''))
        except InvalidOperation:
            raise ValueError('Enter a valid unit price.')
        if not price.is_finite() or price < 0 or price > 10000000 or price != price.quantize(Decimal('.01')):
            raise ValueError('Unit price must be non-negative with at most two decimal places.')
        purchase_day = date.fromisoformat(form.get('purchase_date') or str(today()))
        if purchase_day > today():
            raise ValueError('Purchase date cannot be in the future.')
        mode, funding = form.get('payment_mode'), form.get('funding_source')
        if mode not in ('cash','card','upi') or funding not in ('cafe_operations','owner_personal'):
            raise ValueError('Select payment mode and funding source.')
        total = asset.purchased_quantity + quantity
        asset.average_unit_price = round((asset.purchased_quantity * asset.average_unit_price + quantity * float(price))/total, 2)
        asset.purchased_quantity = total
        purchase = ReusableInventoryPurchase(asset=asset,purchase_date=purchase_day,quantity=quantity,
            unit_price=float(price),total_amount=float(price*quantity),area_scope_snapshot=asset.area_scope,
            payment_mode=mode,funding_source=funding,note=note,created_by_user_id=actor.id)
        batch = AssetBatch(asset=asset,purchase=purchase,status='ordered')
        db.session.add_all([purchase,batch]); db.session.flush()
        details['batch_id'] = batch.id
    elif action in ('load','receive'):
        batch = db.session.get(AssetBatch, integer(form.get('batch_id'),1))
        if not batch or batch.asset_id != asset.id:
            raise ValueError('Choose a batch belonging to this item.')
        details['batch_id'] = batch.id
        if action == 'load':
            if batch.status != 'ordered':
                raise ValueError('Only an awaiting-delivery batch can be loaded.')
            batch.status = 'transit'; quantity = batch.purchase.quantity
        else:
            if batch.status != 'transit':
                raise ValueError('Mark this batch loaded/in transit before receiving it.')
            if quantity > batch.purchase.quantity:
                raise ValueError('Received stock cannot exceed the purchased batch quantity.')
            if form.get('confirmed') != 'yes':
                raise ValueError('Confirm final receipt: the unreceived balance is a cafe-borne transit loss.')
            batch.status = 'received'; batch.received = quantity
            batch.transit_loss = batch.purchase.quantity-quantity
            stock.store += quantity; stock.broken += batch.transit_loss
            details['received'] = quantity
            quantity = batch.transit_loss  # Recovery cap for this transit-loss origin.
            details['transit_loss'] = quantity
    elif action in ('dispatch','return','reported','shortage','wear','recover','count'):
        if action == 'count':
            store, used = integer(form.get('store')), integer(form.get('in_use'))
            if (store,used) != (stock.store,stock.in_use):
                raise ValueError('Count differs: record dispatch/return, loss, wear or linked recovery first, then save the matching count. No automatic staff charges were made.')
            quantity = store+used
        else:
            quantity = integer(form.get('quantity'),1)
            source = 'store' if action == 'dispatch' else 'in_use'
            if action == 'wear':
                source = form.get('source')
                if source not in ('store','in_use'):
                    raise ValueError('Choose store or in-use stock for wear/expiry.')
            if action == 'recover':
                origin = db.session.get(AssetMovement,integer(form.get('origin_id'),1))
                if not origin or origin.asset_id != asset.id or origin.action not in ('initialize','reported','shortage','receive') or quantity > recoverable(origin):
                    raise ValueError('Choose an original loss with enough unrecovered units.')
                source = 'broken'
                details['charge_review'] = 'Review original staff charges in Reusable Assets; recovery does not automatically alter settled payroll.'
                details['original_loss_event_id'] = origin.loss_event_id
            if getattr(stock,source) < quantity:
                raise ValueError(f'Not enough {source.replace("_"," ")} stock for this movement.')
            setattr(stock,source,getattr(stock,source)-quantity)
            target = {'dispatch':'in_use','return':'store','reported':'broken','shortage':'broken','wear':'unusable','recover':'in_use'}[action]
            setattr(stock,target,getattr(stock,target)+quantity)
        details['source'] = source if action != 'count' else 'physical count'
    else:
        raise ValueError('Unknown asset movement.')
    validate(asset)
    # Keep the existing loss graphs and staff-liability workflow in sync.
    if action in ('reported','shortage','wear','receive','recover','count'):
        lost = quantity if action in ('reported','shortage','wear','receive') else 0
        price = batch.purchase.unit_price if action == 'receive' else asset.average_unit_price
        count = ReusableInventoryCount(asset=asset,count_date=today(),quantity_before=(before or {}).get('store',0)+(before or {}).get('in_use',0),
            current_quantity=asset.current_quantity,lost_quantity=lost,recovered_quantity=quantity if action=='recover' else 0,
            unit_price_snapshot=price,loss_value=round(lost*price,2),area_scope_snapshot=asset.area_scope,
            note=f'{action}: {note}'[:255],created_by_user_id=actor.id)
        db.session.add(count); db.session.flush()
        if lost:
            if action in ('reported','shortage'):
                responsible = actor
                if action == 'reported' and manager(actor):
                    responsible = db.session.get(User,integer(form.get('responsible_user_id') or actor.id,1))
                if not responsible or not responsible.active or not _is_staff_user(responsible):
                    raise ValueError('Choose an active responsible employee.')
                loss = _record_reusable_loss_event(asset=asset,count_row=count,
                    loss_type='reported_breakage' if action=='reported' else 'unreported_shortage',
                    responsible_user=responsible if action=='reported' else None,created_by_user=actor,note=note)
            else:
                loss = ReusableInventoryLossEvent(asset=asset,count=count,loss_date=today(),
                    loss_type='transit_loss' if action=='receive' else 'wear_expiry',quantity=lost,
                    unit_price_snapshot=price,total_loss_value=count.loss_value,staff_charge_total=0,
                    cafe_share_amount=count.loss_value,shared_staff_count=0,area_scope_snapshot=asset.area_scope,
                    created_by_user_id=actor.id,note=note)
                db.session.add(loss); db.session.flush()
    details['after'] = state(asset)
    movement = AssetMovement(asset_id=asset.id,actor_id=actor.id,action=action,quantity=quantity,
        request_key=key,details_json=json.dumps(details),loss_event_id=loss.id if loss else None,
        recovery_of_id=origin.id if origin else None)
    db.session.add(movement)
    # Also advance on batch-only operations, so optimistic locking covers all mutations.
    stock.version += 1
    db.session.flush()
    return movement
