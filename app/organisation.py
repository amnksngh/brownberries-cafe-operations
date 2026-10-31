"""Organisation design is deliberately separate from login roles and payroll."""
import json
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import OperationalError

from .auth_helpers import login_required
from .extensions import db
from .models import TimestampMixin, User, StaffProfile, Workstation, WorkstationGroup


class Organisation(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    revision = db.Column(db.Integer, nullable=False, default=0)
    document = db.Column(db.Text, nullable=False, default='[]')


class OrganisationAudit(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    revision = db.Column(db.Integer, nullable=False, unique=True)
    reason = db.Column(db.String(500), nullable=False)
    document = db.Column(db.Text, nullable=False)


def new_position(identifier, title, department, parent=None, enabled=True):
    return dict(id=identifier, title=title, department=department, parent=parent,
        enabled=enabled, progression=None, scope='', responsibilities='', employees=[],
        grades=[dict(name=f'G{i}', minimum='', maximum='', criteria='') for i in range(1, 4)])


def starter_positions():
    rows = []
    def add(title, department, parent=None, enabled=True):
        row = new_position(str(len(rows)+1), title, department, parent, enabled)
        rows.append(row)
        return row
    owner = add('Owner', 'Leadership')
    executive = add('Executive Chef', 'Kitchen', owner['id'], False)
    sous = add('Sous Chef / Kitchen Manager', 'Kitchen', owner['id'])
    sous['progression'] = executive['id']
    for station in ('South Indian', 'Continental, Pizza & Pasta', 'Indo-Chinese & Fast Food'):
        cdp = add(f'CDP — {station}', 'Kitchen', sous['id'])
        cdp['progression'] = sous['id']
        dcdp = add(f'DCDP — {station}', 'Kitchen', cdp['id'], False)
        dcdp['progression'] = cdp['id']
        commis1 = add(f'Commis I — {station}', 'Kitchen', cdp['id'])
        commis1['progression'] = dcdp['id']
        commis2 = add(f'Commis II — {station}', 'Kitchen', cdp['id'], False)
        commis2['progression'] = commis1['id']
        commis3 = add(f'Commis III — {station}', 'Kitchen', cdp['id'], False)
        commis3['progression'] = commis2['id']
        trainee = add(f'Kitchen Trainee — {station}', 'Kitchen', cdp['id'])
        trainee['progression'] = commis3['id']
    add('Kitchen Steward Team', 'Kitchen', sous['id'])
    ladders = {
        'Barista': ['Head Barista', 'Senior Barista', 'Barista', 'Junior Barista', 'Barista Trainee'],
        'Service': ['Floor Supervisor', 'Senior Captain', 'Captain', 'Server', 'Service Trainee'],
        'Management': ['General Manager', 'Operations Manager', 'Operations Executive',
                       'Front Office Supervisor', 'Cashier', 'Reception Executive'],
    }
    for department, titles in ladders.items():
        parent = owner['id']
        for title in titles:
            row = add(title, department, parent)
            row['progression'] = parent if parent != owner['id'] else None
            parent = row['id']
    return rows


def validate_structure(rows):
    ids = {r['id'] for r in rows}
    if len(ids) != len(rows):
        raise ValueError('Duplicate position identifiers.')
    by_id = {r['id']: r for r in rows}
    for edge in ('parent', 'progression'):
        for row in rows:
            seen, cursor = set(), row['id']
            while cursor:
                if cursor not in ids:
                    raise ValueError('Choose an existing reporting/progression position.')
                if cursor in seen:
                    raise ValueError('Reporting and progression paths cannot contain a cycle.')
                seen.add(cursor)
                if len(seen) > 30:
                    raise ValueError('Use at most 30 levels in a reporting or career path.')
                cursor = by_id[cursor][edge]
    for row in rows:
        if not row['enabled'] and row['employees']:
            raise ValueError('Unassign employees before disabling their position.')
        parent = by_id.get(row['parent'])
        if row['enabled'] and parent and not parent['enabled']:
            raise ValueError('An active position cannot report to a disabled position. Reassign its reports first.')


def optional_money(value):
    if not value.strip():
        return ''
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Enter a valid compensation range.') from exc
    if not number.is_finite() or number < 0 or number > 99999999 or number.as_tuple().exponent < -2:
        raise ValueError('Use a non-negative amount with at most two decimal places.')
    return str(number)


bp = Blueprint('organisation', __name__)


@bp.route('/cafe/staff/organisation', methods=['GET', 'POST'])
@login_required
def workspace():
    if not g.current_user.active or not g.current_user.has_any_role('admin', 'owner'):
        abort(403)
    token = session.setdefault('organisation_csrf', secrets.token_urlsafe(32))
    employees = User.query.join(StaffProfile).filter(User.active.is_(True), StaffProfile.archived.is_(False)).order_by(User.full_name).all()
    stations = Workstation.query.filter_by(active=True).order_by(Workstation.name).all()
    groups = WorkstationGroup.query.filter_by(active=True).order_by(WorkstationGroup.name).all()
    scopes = {**{f's:{s.id}': s.name for s in stations}, **{f'g:{s.id}': f'Group: {s.name}' for s in groups}}
    if request.method == 'POST':
        if not secrets.compare_digest(request.form.get('csrf_token', ''), token):
            abort(400, 'Reload this page before saving.')
        try:
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            state = db.session.get(Organisation, 1)
            revision = state.revision if state else 0
            if int(request.form.get('revision', '-1')) != revision:
                raise ValueError('The structure changed elsewhere. Reload before saving.')
            reason = request.form.get('reason', '').strip()
            if not reason or len(reason) > 500:
                raise ValueError('Provide a change reason (up to 500 characters).')
            rows = json.loads(state.document) if state else []
            action = request.form.get('action')
            if action == 'seed':
                if rows:
                    raise ValueError('Templates are only installed into an empty structure.')
                rows = starter_positions()
            elif action == 'save':
                identifier = request.form.get('position_id')
                row = next((r for r in rows if r['id'] == identifier), None)
                if identifier and row is None:
                    raise ValueError('Position no longer exists.')
                if row is None:
                    row = new_position(str(max([int(r['id']) for r in rows], default=0)+1), '', '')
                    rows.append(row)
                for field, limit in [('title', 120), ('department', 80), ('responsibilities', 4000)]:
                    value = request.form.get(field, '').strip()
                    if len(value) > limit or (field != 'responsibilities' and not value):
                        raise ValueError(f'Enter a valid {field} (maximum {limit} characters).')
                    row[field] = value
                row['parent'] = request.form.get('parent') or None
                row['progression'] = request.form.get('progression') or None
                row['enabled'] = request.form.get('enabled') == 'yes'
                scope = request.form.get('scope', '')
                if scope and scope not in scopes and scope != row['scope']:
                    raise ValueError('Select an available station or group.')
                row['scope'] = scope
                selected = list(dict.fromkeys(int(i) for i in request.form.getlist('employees')))
                allowed = {u.id for u in employees} | set(row['employees'])
                if any(i not in allowed for i in selected):
                    raise ValueError('Select active staff for new assignments.')
                row['employees'] = selected
                grades = []
                for i in range(3):
                    name = request.form.get(f'grade_{i}', '').strip()
                    criteria = request.form.get(f'criteria_{i}', '').strip()
                    if not name or len(name) > 40 or len(criteria) > 4000:
                        raise ValueError('Each grade needs a name (up to 40 characters) and criteria up to 4000 characters.')
                    low = optional_money(request.form.get(f'min_{i}', ''))
                    high = optional_money(request.form.get(f'max_{i}', ''))
                    if low and high and Decimal(low) > Decimal(high):
                        raise ValueError('Minimum compensation cannot exceed maximum.')
                    grades.append(dict(name=name, minimum=low, maximum=high, criteria=criteria))
                if len({r['name'].casefold() for r in grades}) != 3:
                    raise ValueError('Grade names must be distinct within a position.')
                row['grades'] = grades
            else:
                raise ValueError('Unknown operation.')
            validate_structure(rows)
            document = json.dumps(rows, ensure_ascii=False)
            if state is None:
                state = Organisation(id=1)
                db.session.add(state)
            state.document, state.revision = document, revision+1
            db.session.add(OrganisationAudit(actor_id=g.current_user.id, revision=revision+1,
                reason=reason, document=document))
            db.session.commit()
            flash('Organisation saved. Login permissions and payroll are unchanged.', 'success')
        except (ValueError, OperationalError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else 'Another update is in progress. Reload and retry.', 'error')
        return redirect(url_for('.workspace'))
    state = db.session.get(Organisation, 1)
    rows = json.loads(state.document) if state else []
    by_id = {r['id']: r for r in rows}
    children = {}
    for row in rows:
        children.setdefault(row['parent'], []).append(row)
    return render_template('cafe/organisation.html', positions=rows, children=children,
        by_id=by_id, scopes=scopes, employees=employees,
        names={u.id: u.full_name for u in User.query.all()}, token=token,
        ist_offset=timedelta(hours=5, minutes=30),
        revision=state.revision if state else 0, blank=new_position('', '', ''),
        audits=OrganisationAudit.query.order_by(OrganisationAudit.revision.desc()).limit(20).all())
