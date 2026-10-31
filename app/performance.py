"""Versioned scorecards and audited reviews. Never writes salary or attendance."""
import json
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError

from .auth_helpers import login_required
from .extensions import db
from .models import TimestampMixin, User, StaffProfile
from .organisation import Organisation


class PerformanceScorecard(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    position_id = db.Column(db.String(40), nullable=False)
    grade = db.Column(db.String(40), nullable=False)
    snapshot = db.Column(db.Text, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class PerformanceReview(TimestampMixin, db.Model):
    __table_args__ = (db.UniqueConstraint('employee_id', 'position_id', 'start', 'end', name='uq_performance_period'),)
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    position_id = db.Column(db.String(40), nullable=False)
    start = db.Column(db.Date, nullable=False)
    end = db.Column(db.Date, nullable=False)
    snapshot = db.Column(db.Text, nullable=False)
    answers = db.Column(db.Text, nullable=False, default='[]')
    status = db.Column(db.String(20), nullable=False, default='draft')
    version = db.Column(db.Integer, nullable=False, default=1)
    recommendation = db.Column(db.String(2000), nullable=False, default='')
    score = db.Column(db.Numeric(6, 2))
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))


class PerformanceAudit(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(db.Integer, db.ForeignKey('performance_review.id'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(30), nullable=False)
    detail = db.Column(db.Text, nullable=False)


def number(value, maximum=100):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Enter a valid numeric score or weight.') from exc
    if not result.is_finite() or result < 0 or result > maximum:
        raise ValueError(f'Value must be between 0 and {maximum}.')
    return result


def criteria_from_form(form):
    criteria = []
    for i in range(8):
        label = form.get(f'label_{i}', '').strip()
        target = form.get(f'target_{i}', '').strip()
        raw = form.get(f'weight_{i}', '').strip()
        if not (label or target or raw):
            continue
        if not label or not target or len(label) > 120 or len(target) > 1000:
            raise ValueError('Each metric needs a name and measurable target/scoring rubric.')
        weight = number(raw)
        if weight <= 0:
            raise ValueError('Each metric weight must be positive.')
        criteria.append(dict(label=label, target=target, weight=str(weight)))
    if not criteria or sum(Decimal(c['weight']) for c in criteria) != 100:
        raise ValueError('Scorecard weights must total exactly 100%.')
    if len({c['label'].casefold() for c in criteria}) != len(criteria):
        raise ValueError('Metric names must be distinct.')
    return criteria


def evaluate(criteria, form, complete):
    answers, total = [], Decimal(0)
    for i, criterion in enumerate(criteria):
        raw = form.get(f'score_{i}', '').strip()
        evidence = form.get(f'evidence_{i}', '').strip()
        if len(evidence) > 2000 or (complete and (not raw or not evidence)):
            raise ValueError('Submission requires a score and evidence for every metric (up to 2000 characters).')
        score = number(raw) if raw else None
        answers.append(dict(score=str(score) if score is not None else '', evidence=evidence))
        if score is not None:
            total += score * Decimal(criterion['weight']) / 100
    return answers, total.quantize(Decimal('.01'))


bp = Blueprint('performance', __name__)


@bp.route('/cafe/staff/performance', methods=['GET', 'POST'])
@login_required
def workspace():
    actor = g.current_user
    admin = actor.has_any_role('admin', 'owner')
    if not actor.active or not (admin or actor.has_role('manager')):
        abort(403)
    token = session.setdefault('performance_csrf', secrets.token_urlsafe(32))
    if request.method == 'POST':
        if not secrets.compare_digest(token, request.form.get('csrf_token', '')):
            abort(400)
        try:
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            state = db.session.get(Organisation, 1)
            positions = json.loads(state.document) if state else []
            action = request.form.get('action')
            if action in {'scorecard', 'create'}:
                if not admin:
                    abort(403)
                if action == 'scorecard':
                    position = next((p for p in positions if p['id'] == request.form.get('position_id') and p['enabled']), None)
                    grade = request.form.get('grade', '')
                    if not position or grade not in {r['name'] for r in position['grades']}:
                        raise ValueError('Choose an active position and one of its configured grades.')
                    threshold = number(request.form.get('threshold', ''))
                    snapshot = dict(position=position['title'], grade=grade,
                        progression=next((p['title'] for p in positions if p['id'] == position['progression']), None),
                        threshold=str(threshold), criteria=criteria_from_form(request.form))
                    db.session.add(PerformanceScorecard(position_id=position['id'], grade=grade,
                        snapshot=json.dumps(snapshot), actor_id=actor.id))
                else:
                    card = db.session.get(PerformanceScorecard, int(request.form.get('card_id', 0)))
                    employee = db.session.get(User, int(request.form.get('employee_id', 0)))
                    reviewer = db.session.get(User, int(request.form.get('reviewer_id', 0)))
                    position = next((p for p in positions if card and p['id'] == card.position_id and p['enabled']), None)
                    if not employee or not employee.active or not employee.staff_profile or employee.staff_profile.archived:
                        raise ValueError('Choose active staff.')
                    if not position or employee.id not in position['employees'] or card.grade not in {r['name'] for r in position['grades']}:
                        raise ValueError('Assign this employee to the active position and choose a current grade first.')
                    if not reviewer or not reviewer.active or not reviewer.has_any_role('admin', 'owner', 'manager') or reviewer.id == employee.id:
                        raise ValueError('Choose a different active admin, owner or manager as reviewer.')
                    start, end = date.fromisoformat(request.form['start']), date.fromisoformat(request.form['end'])
                    if end < start:
                        raise ValueError('Review end must not precede start.')
                    review = PerformanceReview(employee_id=employee.id, reviewer_id=reviewer.id,
                        position_id=position['id'], start=start, end=end, snapshot=card.snapshot)
                    db.session.add(review)
                    db.session.flush()
                    db.session.add(PerformanceAudit(review_id=review.id, actor_id=actor.id, action='created',
                        detail=json.dumps(dict(card_id=card.id, reviewer_id=reviewer.id, snapshot=json.loads(card.snapshot)))))
            else:
                review = db.session.get(PerformanceReview, int(request.form.get('review_id', 0)))
                if not review or not (admin or review.reviewer_id == actor.id):
                    abort(403)
                if int(request.form.get('version', 0)) != review.version:
                    raise ValueError('Review changed elsewhere. Reload before saving.')
                snapshot = json.loads(review.snapshot)
                if action in {'save', 'submit'}:
                    if actor.id != review.reviewer_id or actor.id == review.employee_id:
                        abort(403)
                    if review.status != 'draft':
                        raise ValueError('Only draft reviews can be edited.')
                    if action == 'submit' and review.end >= datetime.now(ZoneInfo('Asia/Kolkata')).date():
                        raise ValueError('Submit only once the review period has ended.')
                    answers, score = evaluate(snapshot['criteria'], request.form, action == 'submit')
                    recommendation = request.form.get('recommendation', '').strip()
                    if len(recommendation) > 2000:
                        raise ValueError('Recommendation must be at most 2000 characters.')
                    review.answers, review.recommendation = json.dumps(answers), recommendation
                    review.score = score if action == 'submit' else None
                    if action == 'submit':
                        review.status = 'submitted'
                    detail = dict(answers=answers, recommendation=recommendation, score=str(review.score))
                elif action in {'approve', 'return'}:
                    if not admin or actor.id in {review.employee_id, review.reviewer_id}:
                        abort(403)
                    if review.status != 'submitted':
                        raise ValueError('Only submitted reviews can be approved or returned.')
                    reason = request.form.get('reason', '').strip()
                    if not reason or len(reason) > 2000:
                        raise ValueError('Provide a decision reason (up to 2000 characters).')
                    review.status = 'approved' if action == 'approve' else 'draft'
                    review.approved_by = actor.id if action == 'approve' else None
                    if action == 'return':
                        review.score = None
                    detail = dict(reason=reason)
                else:
                    raise ValueError('Unknown operation.')
                review.version += 1
                db.session.add(PerformanceAudit(review_id=review.id, actor_id=actor.id, action=action,
                    detail=json.dumps(detail)))
            db.session.commit()
            flash('Performance workflow saved. No payroll or position changes were made.', 'success')
        except (ValueError, KeyError, IntegrityError, OperationalError) as exc:
            db.session.rollback()
            message = str(exc) if isinstance(exc, ValueError) else 'Duplicate review or conflicting update. Check the period and reload.'
            flash(message, 'error')
        return redirect(url_for('.workspace'))
    state = db.session.get(Organisation, 1)
    positions = json.loads(state.document) if state else []
    query = PerformanceReview.query
    if not admin:
        query = query.filter_by(reviewer_id=actor.id)
    reviews = query.order_by(PerformanceReview.id.desc()).limit(100).all()
    ids = [r.id for r in reviews]
    audits = PerformanceAudit.query.filter(PerformanceAudit.review_id.in_(ids)).order_by(PerformanceAudit.id.desc()).all()
    users = User.query.all()
    return render_template('cafe/performance.html', admin=admin, token=token, positions=positions,
        cards=[(c, json.loads(c.snapshot)) for c in PerformanceScorecard.query.order_by(PerformanceScorecard.id.desc()).all()] if admin else [],
        reviews=[(r, json.loads(r.snapshot), json.loads(r.answers)) for r in reviews], audits=audits,
        staff=[u for u in users if u.active and u.staff_profile and not u.staff_profile.archived] if admin else [],
        reviewers=[u for u in users if u.active and u.has_any_role('admin', 'owner', 'manager')] if admin else [],
        names={u.id: u.full_name for u in users}, offset=timedelta(hours=5, minutes=30))
