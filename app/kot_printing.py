"""Outbound-only kitchen printing. Claim once; ambiguous jobs need human reprint."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, session, url_for, flash
from .extensions import db
from .models import TimestampMixin, CafeKitchenTicket
from .auth_helpers import login_required


class KotPrintStation(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    printer = db.Column(db.String(255), nullable=False, default='')
    printers_json = db.Column(db.Text, nullable=False, default='[]')
    last_seen = db.Column(db.DateTime)
    test_mode = db.Column(db.Boolean, nullable=False, default=True)


class KotPrintJob(TimestampMixin, db.Model):
    __table_args__ = (db.UniqueConstraint('ticket_id', 'attempt'),)
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('cafe_kitchen_ticket.id'), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey('kot_print_station.id'), nullable=False)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(30), nullable=False, default='queued')
    claim_key = db.Column(db.String(64))
    claimed_at = db.Column(db.DateTime)
    printer = db.Column(db.String(255))
    test_mode = db.Column(db.Boolean)
    detail = db.Column(db.String(500), nullable=False, default='')


class KotPrintAudit(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(1000), nullable=False)


bp = Blueprint('kot_printing', __name__)


def csrf():
    return session.setdefault('kot_print_csrf', secrets.token_urlsafe(32))


def check_csrf():
    value = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
    if not session.get('kot_print_csrf') or not secrets.compare_digest(value, session['kot_print_csrf']):
        abort(400)


def lock():
    db.session.rollback()
    if db.engine.dialect.name == 'sqlite':
        db.session.execute(db.text('BEGIN IMMEDIATE'))


def agent():
    token = request.headers.get('Authorization', '').removeprefix('Bearer ')
    station = db.session.get(KotPrintStation, 1)
    if not station or not token or not secrets.compare_digest(station.token_hash, hashlib.sha256(token.encode()).hexdigest()):
        abort(401)
    return station


@bp.app_context_processor
def context():
    return dict(kot_remote_enabled=lambda: bool((s := db.session.get(KotPrintStation, 1)) and s.enabled), kot_print_csrf=csrf)


@bp.route('/cafe/kot-print/jobs/<token>', methods=['POST'])
def enqueue(token):
    # Existing random KOT capability supports both authenticated and kiosk approval.
    check_csrf()
    lock()
    ticket = CafeKitchenTicket.query.filter_by(token=token).first_or_404()
    station = db.session.get(KotPrintStation, 1)
    if not station or not station.enabled or not station.printer:
        return jsonify(error='Kitchen station is not configured. Contact an admin.'), 409
    job = KotPrintJob.query.filter_by(ticket_id=ticket.id, attempt=1).first()
    if not job:
        job = KotPrintJob(ticket_id=ticket.id, station_id=station.id)
        db.session.add(job)
        db.session.flush()
    db.session.commit()
    online = station.last_seen and station.last_seen > datetime.utcnow()-timedelta(seconds=45)
    return jsonify(job_id=job.id, status=job.status, message=f'KOT #{job.id}: {job.status}. ' +
        ('Kitchen station connected.' if online else 'Kitchen station offline; job retained in queue.'))


@bp.route('/api/kot-station/poll', methods=['POST'])
def poll():
    lock()
    station = agent()
    data = request.get_json(silent=True) or {}
    printers = data.get('printers')
    if not isinstance(printers, list) or len(printers) > 100 or any(not isinstance(p, str) or len(p) > 255 for p in printers):
        abort(400)
    station.last_seen = datetime.utcnow()
    station.printers_json = json.dumps(printers)
    job = None
    if station.enabled and station.printer in printers:
        job = KotPrintJob.query.filter_by(station_id=station.id, status='queued').order_by(KotPrintJob.id).first()
    response = dict(job=None)
    if job:
        job.status, job.claimed_at, job.claim_key = 'claimed', datetime.utcnow(), secrets.token_urlsafe(32)
        job.printer, job.test_mode = station.printer, station.test_mode
        ticket = db.session.get(CafeKitchenTicket, job.ticket_id)
        response['job'] = dict(id=job.id, claim_key=job.claim_key, printer=job.printer,
            test_mode=job.test_mode, attempt=job.attempt, ticket=json.loads(ticket.contents_json))
    db.session.commit()
    return jsonify(response)


@bp.route('/api/kot-station/result/<int:job_id>', methods=['POST'])
def result(job_id):
    lock()
    station = agent()
    job = db.session.get(KotPrintJob, job_id)
    data = request.get_json(silent=True) or {}
    if not job or job.station_id != station.id or not secrets.compare_digest(str(data.get('claim_key', '')), job.claim_key or 'missing'):
        abort(403)
    status = data.get('status')
    if status not in {'spooled', 'test_complete', 'needs_review'} or (status == 'spooled' and job.test_mode) or (status == 'test_complete' and not job.test_mode):
        abort(400)
    if job.status == 'claimed':
        job.status, job.detail = status, str(data.get('detail', ''))[:500]
        db.session.add(KotPrintAudit(action='agent_result', detail=f'Job {job.id}: {status}: {job.detail}'))
        db.session.commit()
    elif job.status != status:
        abort(409)
    return jsonify(ok=True)


@bp.route('/cafe/kot-print', methods=['GET', 'POST'])
@login_required
def settings():
    if not g.current_user.has_any_role('admin', 'owner'):
        abort(403)
    if request.method == 'POST':
        check_csrf()
        lock()
        station = db.session.get(KotPrintStation, 1)
        action = request.form.get('action')
        try:
            if action == 'register':
                if station:
                    raise ValueError('Station already registered. Rotate its key if replacing the machine.')
                token = secrets.token_urlsafe(32)
                station = KotPrintStation(id=1, name='Kitchen KOT Station', token_hash=hashlib.sha256(token.encode()).hexdigest())
                db.session.add(station)
                session['kot_new_station_key'] = token
            elif action == 'rotate' and station:
                token = secrets.token_urlsafe(32)
                station.token_hash = hashlib.sha256(token.encode()).hexdigest()
                station.enabled, station.last_seen = False, None
                station.printer, station.printers_json = '', '[]'
                session['kot_new_station_key'] = token
            elif action == 'configure' and station:
                printer = request.form.get('printer', '')
                if printer not in json.loads(station.printers_json):
                    raise ValueError('Select a printer reported by the kitchen agent.')
                station.printer = printer
                station.enabled = request.form.get('enabled') == 'yes'
                station.test_mode = request.form.get('test_mode') != 'live'
            elif action == 'cancel' and station:
                job = db.session.get(KotPrintJob, int(request.form.get('job_id', '0')))
                reason = request.form.get('reason', '').strip()
                if not job or job.status != 'queued' or not reason or len(reason) > 200:
                    raise ValueError('Only queued jobs can be cancelled; provide a reason.')
                job.status, job.detail = 'cancelled', reason
                action = f'cancel job {job.id}'
            elif action == 'reprint' and station:
                if request.form.get('confirm') != 'yes':
                    raise ValueError('Check the physical printer before confirming a reprint.')
                job = db.session.get(KotPrintJob, int(request.form.get('job_id', '0')))
                reason = request.form.get('reason', '').strip()
                if not job or not reason or len(reason) > 200:
                    raise ValueError('Select a job and supply a reason (up to 200 characters).')
                latest = KotPrintJob.query.filter_by(ticket_id=job.ticket_id).order_by(KotPrintJob.attempt.desc()).first()
                if latest.id != job.id or job.status == 'queued' or (job.status == 'claimed' and job.claimed_at > datetime.utcnow()-timedelta(minutes=2)):
                    raise ValueError('A newer or pending attempt exists. Wait and check the printer first.')
                db.session.add(KotPrintJob(ticket_id=job.ticket_id, station_id=1, attempt=job.attempt+1))
                action = f'reprint job {job.id}'
            else:
                raise ValueError('Invalid operation.')
            db.session.add(KotPrintAudit(actor_id=g.current_user.id, action=action,
                detail=(request.form.get('reason', '') or 'Station configuration updated')[:1000]))
            db.session.commit()
            flash('KOT printing configuration saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        return redirect(url_for('.settings'))
    station = db.session.get(KotPrintStation, 1)
    response = render_template('cafe/kot_print_settings.html', station=station,
        printers=json.loads(station.printers_json) if station else [],
        online=bool(station and station.last_seen and station.last_seen > datetime.utcnow()-timedelta(seconds=45)),
        pairing_key=session.pop('kot_new_station_key', None), csrf=csrf(),
        jobs=KotPrintJob.query.order_by(KotPrintJob.id.desc()).limit(100).all())
    return response, 200, {'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'}
