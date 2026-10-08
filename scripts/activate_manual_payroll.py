"""Explicit, backed-up cutover. Never run as a startup task.

Use audit_manual_payroll.py first. Stop application writers, take a current
verified backup, then pass --apply --admin-id with the administrator of record.
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.models import User, LeaveBalance, StaffProfile, StaffAttendance
from app.manual_payroll import activate, active


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--admin-id',required=True,type=int)
    parser.add_argument('--apply',action='store_true',required=True)
    parser.add_argument('--include-admins',action='store_true',help='Explicitly reset admin staff too, as authorized by the owner')
    args=parser.parse_args()
    if not (Path(args.instance)/'brownberries.db').is_file():
        raise SystemExit('Existing database required; refusing to initialize a new production database.')
    app=create_app(instance_path=str(Path(args.instance).resolve()),initialize_legacy_leaves=False)
    with app.app_context():
        if db.engine.dialect.name=='sqlite':
            db.session.execute(db.text('BEGIN IMMEDIATE'))
        actor=db.session.get(User,args.admin_id)
        if not actor or not actor.active or not actor.has_role('admin'):
            raise SystemExit('An active administrator is required.')
        admins={u.id for u in User.query.all() if u.has_role('admin')}
        before={r.user_id:(r.earned_balance,r.urgent_balance) for r in LeaveBalance.query.all() if r.user_id in admins}
        history_count=StaffAttendance.query.count()
        changed=activate(actor,include_admins=args.include_admins)
        after={r.user_id:(r.earned_balance,r.urgent_balance) for r in LeaveBalance.query.all() if r.user_id in admins}
        if args.include_admins:
            staff_ids={p.user_id for p in StaffProfile.query.all()}
            assert all(value==(0,0) for uid,value in after.items() if uid in staff_ids), 'Admin staff reset incomplete; aborting.'
        else:
            assert before==after, 'Admin balances changed; aborting.'
        assert history_count==StaffAttendance.query.count(), 'Attendance history changed; aborting.'
        db.session.commit()
        print(json.dumps({'active':active(),'effective_date':'2026-10-01','reset_staff_count':len(changed),
            'include_admins':args.include_admins,'reset_admin_count':len(set(changed)&admins),
            'excluded_admin_count':0 if args.include_admins else len(admins),'legacy_attendance_preserved':history_count}))


if __name__=='__main__':
    main()
