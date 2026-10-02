import tempfile
import unittest
from datetime import date
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine, event

from app.cafe import _all_workstations, _inventory_closing_snapshots
from app.extensions import db
from app.leave_logic import run_leave_maintenance
from app.models import InventoryDailyClosing, InventoryItem, LeaveBalance, LeaveTransaction, StaffProfile, User, Workstation
from app.reliability import configure_sqlite, DisconnectedPollingSession


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", SECRET_KEY="test")
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def test_closing_snapshots_preserve_dates_and_first_duplicate(self):
        a, b = InventoryItem(name="Milk", unit="litre", area="cafe"), InventoryItem(name="Coffee", unit="kg", area="cafe")
        db.session.add_all([a, b])
        db.session.flush()
        rows = [InventoryDailyClosing(item_id=a.id, closing_date=date(2026, 9, day), closing_stock=value)
                for day, value in [(1, 10), (4, 8), (4, 99), (5, 6), (5, 98), (6, 4)]]
        db.session.add_all(rows)
        db.session.commit()
        queries = []
        def count(*args): queries.append(1)
        event.listen(db.engine, "before_cursor_execute", count)
        try:
            current, previous = _inventory_closing_snapshots(date(2026, 9, 5))
            self.assertEqual(len(queries), 2)
        finally:
            event.remove(db.engine, "before_cursor_execute", count)
        self.assertEqual(current[a.id].closing_stock, 6)
        self.assertEqual(previous[a.id].closing_stock, 8)
        self.assertNotIn(b.id, current)
        self.assertNotIn(b.id, previous)

    def test_workstation_cache_is_request_local_and_get_only(self):
        with self.app.test_request_context("/"):
            first = _all_workstations(True)
            self.assertIs(first, _all_workstations(True))
        station = Workstation.query.filter_by(slug="barista").one()
        station.name = "Updated Counter"
        db.session.commit()
        with self.app.test_request_context("/"):
            self.assertIn("Updated Counter", [s.name for s in _all_workstations(True)])
        with self.app.test_request_context("/", method="POST"):
            self.assertIsNot(_all_workstations(True), _all_workstations(True))

    def test_leave_credits_remain_idempotent_and_accrue_next_period(self):
        user = User(full_name="Test", email="test@example.invalid", password_hash="unused")
        user.staff_profile = StaffProfile(joining_date=date(2026, 8, 1), probation_end_date=date(2026, 8, 1))
        db.session.add(user)
        db.session.commit()
        run_leave_maintenance(date(2026, 9, 15))
        balance = LeaveBalance.query.filter_by(user_id=user.id).one()
        self.assertEqual(balance.earned_balance, 3)
        self.assertEqual(balance.urgent_balance, 12)
        count = LeaveTransaction.query.count()
        run_leave_maintenance(date(2026, 9, 15))
        self.assertEqual(LeaveTransaction.query.count(), count)
        self.assertEqual(balance.earned_balance, 3)
        run_leave_maintenance(date(2026, 9, 30))
        self.assertEqual(balance.earned_balance, 4)
        self.assertEqual(LeaveTransaction.query.count(), count + 1)

    def test_sqlite_wal_allows_reader_during_write_and_sets_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "test.db"))
            try:
                configure_sqlite(engine)
                with engine.begin() as c:
                    c.exec_driver_sql("CREATE TABLE example (value INTEGER)")
                    c.exec_driver_sql("INSERT INTO example VALUES (1)")
                with engine.connect() as writer, engine.connect() as reader:
                    self.assertEqual(reader.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
                    self.assertEqual(reader.exec_driver_sql("PRAGMA busy_timeout").scalar(), 15000)
                    writer.exec_driver_sql("UPDATE example SET value=2")
                    self.assertEqual(reader.exec_driver_sql("SELECT value FROM example").scalar(), 1)
                    writer.commit()
                    self.assertEqual(reader.exec_driver_sql("SELECT value FROM example").scalar(), 2)
            finally:
                engine.dispose()

    def test_expired_poll_is_400_but_other_errors_are_not_hidden(self):
        def expired(*args): raise KeyError("Session is disconnected")
        middleware = DisconnectedPollingSession(expired)
        statuses = []
        body = middleware({"PATH_INFO": "/socket.io/"}, lambda status, headers: statuses.append(status))
        self.assertEqual(statuses, ["400 Bad Request"])
        self.assertIn(b"reconnect", body[0])
        with self.assertRaises(KeyError):
            middleware({"PATH_INFO": "/cafe/orders"}, lambda *args: None)
        def other(*args): raise KeyError("actual application bug")
        with self.assertRaises(KeyError):
            DisconnectedPollingSession(other)({"PATH_INFO": "/socket.io/"}, lambda *args: None)


if __name__ == "__main__":
    unittest.main()
