"""Additive cash reconciliation records; existing sales and cash history are retained."""
from datetime import datetime
from .extensions import db


class CashDeskRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    request_key = db.Column(db.String(80), nullable=False, unique=True)
    details_json = db.Column(db.Text, nullable=False)
    actor = db.relationship('User')


class CashDeskSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    business_date = db.Column(db.Date, nullable=False, unique=True)
    opening_id = db.Column(db.Integer, db.ForeignKey('cash_desk_record.id'), nullable=False)
    closing_id = db.Column(db.Integer, db.ForeignKey('cash_desk_record.id'))
    opening = db.relationship('CashDeskRecord', foreign_keys=[opening_id])
    closing = db.relationship('CashDeskRecord', foreign_keys=[closing_id])


class CashPaymentCorrection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('cafe_order.id'), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey('cash_desk_record.id'), nullable=False)
    decision_id = db.Column(db.Integer, db.ForeignKey('cash_desk_record.id'))
    status = db.Column(db.String(20), nullable=False, default='pending')
    request_record = db.relationship('CashDeskRecord', foreign_keys=[request_id])
    decision = db.relationship('CashDeskRecord', foreign_keys=[decision_id])
