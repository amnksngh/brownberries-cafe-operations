"""Rehearse activation and UI integration on a disposable snapshot only."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.models import User, LeaveBalance, StaffAttendance, StaffProfile
from app.manual_payroll import activate
from app.manual_payroll_models import ManualAttendance
from app.leave_logic import run_leave_maintenance


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--instance',required=True)
    parser.add_argument('--html-output')
    parser.add_argument('--include-admins',action='store_true')
    args=parser.parse_args()
    source=Path(args.instance)
    with TemporaryDirectory(prefix='manual-payroll-audit-') as folder:
        with closing(sqlite3.connect(f"file:{(source/'brownberries.db').as_posix()}?mode=ro",uri=True)) as original:
            with closing(sqlite3.connect(str(Path(folder)/'brownberries.db'))) as target:
                original.backup(target)
        if (source/'deployment_config.json').exists():
            shutil.copy2(source/'deployment_config.json',folder)
        app=create_app(instance_path=folder,initialize_legacy_leaves=False)
        app.config['TESTING']=True
        try:
            with app.app_context():
                admin=next(u for u in User.query.filter_by(active=True) if u.has_role('admin'))
                admin_id=admin.id
                staff=next(u for u in User.query.join(StaffProfile) if u.active and not u.has_role('admin'))
                staff_id=staff.id
                original_users={u.id:(u.password_hash,u.email,u.active,u.role) for u in User.query.all()}
                original_attendance=[tuple(r) for r in db.session.execute(db.text('SELECT * FROM staff_attendance ORDER BY id'))]
                admins={u.id for u in User.query.all() if u.has_role('admin')}
                balances={r.user_id:(r.earned_balance,r.urgent_balance) for r in LeaveBalance.query.all() if r.user_id in admins}
                changed=activate(admin,include_admins=args.include_admins);db.session.commit()
                run_leave_maintenance()
                after={r.user_id:(r.earned_balance,r.urgent_balance) for r in LeaveBalance.query.all() if r.user_id in admins}
                if args.include_admins:
                    staff_ids={p.user_id for p in StaffProfile.query.all()}
                    assert all(value==(0,0) for uid,value in after.items() if uid in staff_ids)
                else:
                    assert balances==after
                assert all(db.session.query(LeaveBalance).filter_by(user_id=uid).one().earned_balance==0 for uid in changed)
            client=app.test_client()
            with client.session_transaction() as session: session['user_id']=admin_id
            routes=['/cafe/manual-payroll?user_id='+str(staff_id),'/cafe/','/cafe/staff?section=active_staff',
                '/profile?section=salary','/cafe/staff?section=payroll_summary','/profile?section=attendance',
                '/cafe/manual-payroll?export=csv&user_id='+str(staff_id),'/staff/app']
            for url in routes:
                response=client.get(url,follow_redirects=True)
                assert response.status_code==200,(url,response.status_code)
            page=client.get('/cafe/manual-payroll?user_id='+str(staff_id)).get_data(as_text=True)
            assert 'Not reviewed' in page
            if args.html_output: Path(args.html_output).write_text(page,encoding='utf-8')
            for endpoint in ['/api/mobile/attendance/check-in','/api/mobile/attendance/heartbeat',
                             '/api/mobile/attendance/check-out','/api/mobile/staff/leaves']:
                response=client.post(endpoint,json={})
                assert response.status_code==409,(endpoint,response.status_code)
            assert client.post('/cafe/staff',data={'action':'attendance_for_user'}).status_code==409
            assert client.post('/cafe/manual-payroll',data={'action':'day'}).status_code==400
            with client.session_transaction() as session: session['user_id']=staff_id
            assert client.get('/cafe/manual-payroll').status_code==200
            assert client.post('/cafe/manual-payroll',data={'action':'day'}).status_code==403
            with app.app_context():
                assert original_users=={u.id:(u.password_hash,u.email,u.active,u.role) for u in User.query.all()}
                assert original_attendance==[tuple(r) for r in db.session.execute(db.text('SELECT * FROM staff_attendance ORDER BY id'))]
                assert ManualAttendance.query.count()==0
                print(json.dumps({'reset_staff_count':len(changed),'include_admins':args.include_admins,'reset_admin_count':len(set(changed)&admins),
                    'legacy_attendance_preserved':len(original_attendance),'read_routes_verified':len(routes),
                    'legacy_writers_blocked':True,'source_unchanged':True,'admin_id':admin_id}))
        finally:
            with app.app_context(): db.session.remove();db.engine.dispose()


if __name__=='__main__':main()
