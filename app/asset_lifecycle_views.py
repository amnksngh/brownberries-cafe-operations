import json
import secrets
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError
from .auth_helpers import login_required
from .extensions import db
from .models import ReusableInventoryAsset, User
from .asset_lifecycle_models import AssetMovement
from . import asset_lifecycle as flow

bp = Blueprint('asset_lifecycle', __name__)


def revision(asset):
    if asset.lifecycle:
        return str(asset.lifecycle.version)
    return f'opening:{asset.purchased_quantity}:{asset.current_quantity}'


@bp.route('/cafe/inventory/reusable-lifecycle', methods=['GET','POST'])
@login_required
def workspace():
    from .cafe import _is_staff_user
    if not g.current_user.active or not (_is_staff_user(g.current_user) or flow.manager(g.current_user)):
        abort(403)
    manage = flow.manager(g.current_user)
    csrf = session.setdefault('asset_lifecycle_csrf', secrets.token_urlsafe(32))
    asset_id = request.values.get('asset_id', type=int)
    if request.method == 'POST':
        if not secrets.compare_digest(csrf,request.form.get('csrf_token','')):
            abort(400,'Reload this page before submitting.')
        try:
            key = request.form.get('request_key','')
            if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):
                raise ValueError('Reload this form before submitting.')
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            if AssetMovement.query.filter_by(request_key=key).first():
                flash('Already recorded; no duplicate movement was made.','success')
            else:
                asset = ReusableInventoryAsset.query.filter_by(id=asset_id).with_for_update().first()
                if request.form.get('action') == 'create':
                    if not manage:
                        abort(403)
                    name = (request.form.get('name') or '').strip()
                    if not name or len(name)>120:
                        raise ValueError('Enter an item name (maximum 120 characters).')
                    if ReusableInventoryAsset.query.filter(db.func.lower(ReusableInventoryAsset.name)==name.lower()).first():
                        raise ValueError('This item already exists. Select it to add a purchase batch.')
                    from .cafe import _normalize_reusable_area
                    asset = ReusableInventoryAsset(name=name,area_scope=_normalize_reusable_area(request.form.get('area_scope')),
                        unit='pcs',purchased_quantity=0,current_quantity=0,average_unit_price=0,active=True)
                    db.session.add(asset); db.session.flush(); asset_id=asset.id
                    flow.execute(asset,dict(action='initialize',store='0',reason='New lifecycle item'),g.current_user,key)
                else:
                    if not asset:
                        raise ValueError('Choose an existing reusable item.')
                    if request.form.get('revision') != revision(asset):
                        raise ValueError('Stock changed while this form was open. Reload and review the latest quantities.')
                    flow.execute(asset,request.form,g.current_user,key)
                db.session.commit()
                from .cafe import _STATS_CACHE
                _STATS_CACHE.clear()
                flash('Asset movement saved with an audit trail.','success')
        except (ValueError,TypeError) as exc:
            db.session.rollback(); flash(str(exc),'error')
        except (IntegrityError,OperationalError,StaleDataError):
            db.session.rollback(); flash('Stock changed or another operation is in progress. Reload before trying again.','error')
        finally:
            db.session.rollback()
        return redirect(url_for('.workspace',asset_id=asset_id))
    assets = ReusableInventoryAsset.query.filter_by(active=True).order_by(ReusableInventoryAsset.name).all()
    selected = next((a for a in assets if a.id==asset_id), None)
    movements = AssetMovement.query.filter_by(asset_id=selected.id).order_by(AssetMovement.id.desc()).all() if selected else []
    origins = [(m,flow.recoverable(m)) for m in movements if m.action in ('initialize','reported','shortage','receive') and m.quantity>0] if manage else []
    from .cafe import _reusable_area_options
    return render_template('cafe/asset_lifecycle.html',assets=assets,selected=selected,
        stock=flow.state(selected) if selected else None,manage=manage,csrf=csrf,
        revision=revision(selected) if selected else '',new_key=lambda:secrets.token_hex(16),
        today=flow.today(),origins=[(m,n) for m,n in origins if n>0],
        movements=[(m,json.loads(m.details_json)) for m in movements[:100]] if manage else [],
        staff=[u for u in User.query.filter_by(active=True).order_by(User.full_name).all() if _is_staff_user(u)] if manage else [],
        areas=_reusable_area_options(),state=flow.state)
