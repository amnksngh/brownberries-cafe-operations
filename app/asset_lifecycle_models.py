"""Additive stock buckets and append-only movement history for reusable assets."""
from datetime import datetime
from .extensions import db


class AssetStock(db.Model):
    asset_id = db.Column(db.Integer, db.ForeignKey('reusable_inventory_asset.id'), primary_key=True)
    store = db.Column(db.Integer, nullable=False, default=0)
    in_use = db.Column(db.Integer, nullable=False, default=0)
    broken = db.Column(db.Integer, nullable=False, default=0)
    unusable = db.Column(db.Integer, nullable=False, default=0)
    version = db.Column(db.Integer, nullable=False, default=1)
    __mapper_args__ = {'version_id_col': version}
    asset = db.relationship('ReusableInventoryAsset', backref=db.backref('lifecycle', uselist=False))


class AssetBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey('reusable_inventory_asset.id'), nullable=False, index=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey('reusable_inventory_purchase.id'), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default='ordered')
    received = db.Column(db.Integer, nullable=False, default=0)
    transit_loss = db.Column(db.Integer, nullable=False, default=0)
    purchase = db.relationship('ReusableInventoryPurchase')
    asset = db.relationship('ReusableInventoryAsset', backref='delivery_batches')


class AssetMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey('reusable_inventory_asset.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    action = db.Column(db.String(30), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    request_key = db.Column(db.String(32), nullable=False, unique=True)
    details_json = db.Column(db.Text, nullable=False)
    # Origin links prevent recovery of the same missing units twice.
    loss_event_id = db.Column(db.Integer, db.ForeignKey('reusable_inventory_loss_event.id'))
    recovery_of_id = db.Column(db.Integer, db.ForeignKey('asset_movement.id'))
    actor = db.relationship('User')
