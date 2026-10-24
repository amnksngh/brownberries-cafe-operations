import json
import secrets
from datetime import date, datetime, timezone
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload
from .auth_helpers import login_required
from .extensions import db
from .models import InventoryCategory, CafeOrder
from .cash_desk_models import CashDeskRecord, CashDeskSession, CashPaymentCorrection
from . import cash_desk as desk

bp = Blueprint('cash_desk', __name__)


@bp.app_context_processor
def cash_session_context():
    def notice():
        try:
            desk.require_open_session()
        except ValueError as exc:
            return str(exc)
        return ''
    return {'cash_session_notice': notice}


@bp.app_template_filter('cash_ist')
def cash_ist(value):
    if not value:
        return '—'
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(desk.IST).strftime('%d %b %Y, %I:%M:%S %p IST')


@bp.app_template_filter('cash_audit_ist')
def cash_audit_ist(value):
    # Transform presentation only; immutable evidence and comparison values remain UTC.
    if isinstance(value, list):
        return [cash_audit_ist(item) for item in value]
    if isinstance(value, dict):
        return {key: cash_ist(item) if key == 'paid_at' and item and item != 'None'
                else cash_audit_ist(item) for key, item in value.items()}
    return value


@bp.route('/cafe/cash-counter/control', methods=['GET', 'POST'])
@login_required
def workspace():
    from .cafe import _can_manage_cash_counter, CASH_DENOMINATION_VALUES, _STATS_CACHE
    if not (_can_manage_cash_counter(g.current_user) or
            (g.current_user.active and g.current_user.has_role('owner'))):
        abort(403)
    csrf = session.setdefault('cash_desk_csrf', secrets.token_urlsafe(32))
    if request.method == 'POST':
        if not secrets.compare_digest(csrf, request.form.get('csrf_token', '')):
            abort(400, 'Reload the page before submitting.')
        try:
            key = request.form.get('request_key', '')
            if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
                raise ValueError('Reload this form before submitting.')
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            if CashDeskRecord.query.filter_by(request_key=key).first():
                flash('This operation has already been recorded.', 'success')
            else:
                if request.form.get('revision') != desk.revision():
                    raise ValueError('Cash records changed while this page was open. Reload and verify the latest figures before saving.')
                if request.form.get('action') not in ('open', 'close'):
                    desk.require_open_session()
                desk.execute(request.form, g.current_user, key)
                db.session.commit()
                _STATS_CACHE.clear()
                flash('Recorded with an audit trail. Counts do not automatically write off discrepancies.', 'success')
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        except (IntegrityError, OperationalError):
            db.session.rollback()
            flash('Another operation is in progress or this record already exists. Reload and check the history.', 'error')
        finally:
            db.session.rollback()
        return redirect(url_for('.workspace'))
    try:
        day = date.fromisoformat(request.args.get('day') or str(desk.today()))
    except ValueError:
        abort(400, 'Choose a valid date.')
    daily = CashDeskSession.query.filter_by(business_date=day).first()
    open_session = CashDeskSession.query.filter_by(closing_id=None).first()
    start, end = desk.day_bounds(day)
    audit = CashDeskRecord.query.options(joinedload(CashDeskRecord.actor)).filter(CashDeskRecord.occurred_at >= start, CashDeskRecord.occurred_at < end).order_by(CashDeskRecord.id.desc()).all()
    corrections = CashPaymentCorrection.query.options(joinedload(CashPaymentCorrection.request_record).joinedload(CashDeskRecord.actor)).filter_by(status='pending').order_by(CashPaymentCorrection.id).all()
    settled = CafeOrder.query.filter_by(status='paid').order_by(CafeOrder.paid_at.desc(), CafeOrder.id.desc()).limit(200).all()
    group_ids = {order.settlement_group_id for order in settled if order.settlement_group_id}
    grouped = {}
    if group_ids:
        for order in CafeOrder.query.filter(CafeOrder.settlement_group_id.in_(group_ids)).order_by(CafeOrder.id).all():
            grouped.setdefault(order.settlement_group_id, []).append(order)
    settlement_options = []
    seen = set()
    for order in settled:
        group = order.settlement_group_id or f'order:{order.id}'
        if group in seen:
            continue
        seen.add(group)
        if not order.settlement_group_id and (order.payment_type or '').lower() == 'split payment':
            continue
        orders = grouped[order.settlement_group_id] if order.settlement_group_id else [order]
        settlement_options.append(dict(id=order.id, label=', '.join(o.display_code or f'Order {o.id}' for o in orders),
            total=sum(o.total_amount for o in orders), method=order.payment_type, paid_at=order.paid_at))
    return render_template('cafe/cash_desk.html', csrf=csrf, revision=desk.revision(),
        new_key=lambda:secrets.token_hex(16), denominations=CASH_DENOMINATION_VALUES,
        state=desk.snapshot(), summary=desk.day_summary(day), day=day, daily=daily, open_session=open_session,
        opening=json.loads(daily.opening.details_json) if daily else None,
        closing=json.loads(daily.closing.details_json) if daily and daily.closing else None,
        audit=[(r,json.loads(r.details_json)) for r in audit],
        corrections=[(r,json.loads(r.request_record.details_json)) for r in corrections],
        categories=InventoryCategory.query.order_by(InventoryCategory.name).all(),
        settlement_options=settlement_options,
        approver=g.current_user.has_role('admin') or g.current_user.has_role('owner'))
