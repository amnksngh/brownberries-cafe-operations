"""Exercise major read routes against a disposable SQLite snapshot, never live data."""
import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import subprocess
from html.parser import HTMLParser
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import event
from app import create_app
from app.extensions import db
from app.models import User, CafeTable


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.active = not attrs.get("src") and attrs.get("type", "text/javascript") in ("text/javascript", "application/javascript")

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False
            self.parts.append("\n;\n")

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--compare-ref", help="Compare financial/stock results with a trusted local Git revision")
    parser.add_argument("--check-js", action="store_true", help="Compile rendered inline JavaScript with Node (no execution)")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="brownberries-audit-") as folder:
        source_dir = Path(args.instance)
        source = sqlite3.connect(f"file:{(source_dir / 'brownberries.db').as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(str(Path(folder) / "brownberries.db"))
        source.backup(target)
        source.close()
        target.close()
        config = source_dir / "deployment_config.json"
        if config.exists():
            shutil.copy2(config, folder)
        app = create_app(instance_path=folder)
        app.config.update(TESTING=True)
        with app.app_context():
            admin = next(u for u in User.query.filter_by(active=True) if u.has_role("admin"))
            admin_id = admin.id
            table = CafeTable.query.filter_by(active=True).first()
            table_slug = table.qr_slug if table else None
            engine = db.engine
        counter = [0]
        def counted(*args):
            counter[0] += 1
        event.listen(engine, "before_cursor_execute", counted)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = admin_id
        urls = ["/healthz", "/", "/dashboard", "/profile", "/cafe/",
                "/cafe/menu", "/cafe/orders", "/cafe/cashier", "/cafe/kitchen",
                "/cafe/display/kiosk/barista", "/cafe/to-purchase",
                "/cafe/items-availability", "/cafe/stats", "/cafe/stats/export",
                "/cafe/reviews", "/cafe/bookings", "/library/", "/library/books",
                "/library/members", "/cafe/reusable-assets/report-breakage", "/customer/menu"]
        urls += ["/table?preview=1", "/cafe/operations/", "/cafe/cash-counter",
                 "/library/loans", "/library/payments", "/library/authors", "/library/plans",
                 "/cafe/user-types", "/cafe/api/statistics/summary"]
        urls += ["/cafe/inventory?section=" + s for s in
                 ["dashboard", "items_stock", "reusable_assets", "purchases", "daily_closing", "wastage", "audit", "settings"]]
        urls += ["/cafe/staff?section=" + s for s in
                 ["active_staff", "attendance_calendar", "payroll_summary", "leave_requests"]]
        with app.app_context():
            from flask import url_for
            with app.test_request_context():
                for rule in app.url_map.iter_rules():
                    if "GET" in rule.methods and "browse" in rule.endpoint and not rule.arguments:
                        urls.append(str(rule))
                if table_slug:
                    for rule in app.url_map.iter_rules():
                        if rule.endpoint == "main.table_qr_page":
                            urls.append(url_for(rule.endpoint, slug=table_slug))
        for url in urls:
            counter[0] = 0
            start = time.perf_counter()
            try:
                response = client.get(url)
                row = dict(url=url, status=response.status_code, bytes=len(response.data))
                if args.check_js and response.mimetype == "text/html" and response.status_code == 200:
                    scripts = InlineScripts()
                    scripts.feed(response.get_data(as_text=True))
                    compiled = subprocess.run(["node", "-e", "new (require('vm').Script)(require('fs').readFileSync(0,'utf8'))"],
                                              input="".join(scripts.parts), text=True, capture_output=True, encoding="utf-8")
                    if compiled.returncode:
                        raise AssertionError("Inline JavaScript syntax error: " + compiled.stderr[:500])
            except Exception as exc:
                row = dict(url=url, status=500, error=f"{type(exc).__name__}: {exc}")
            row.update(seconds=round(time.perf_counter() - start, 3), queries=counter[0])
            results.append(row)
            print(json.dumps(row), flush=True)
        if args.compare_ref:
            # Execute only the requested, trusted repository revision, never a
            # downloaded document. Both implementations use this disposable DB.
            from flask import g
            from app import cafe
            original = {"__name__": "app.baseline_cafe", "__package__": "app"}
            source = subprocess.check_output(["git", "show", args.compare_ref + ":app/cafe.py"], text=True, encoding="utf-8")
            exec(compile(source, "baseline_cafe.py", "exec"), original)
            keys = ["expense_vs_earning", "workstation_financial_rows", "closing_summary",
                    "item_stock_band_map", "reusable_summary", "reusable_loss_summary"]
            for period in ("today", "month", "last_month"):
                snapshots = []
                for function, namespace in ((original["inventory"], original), (cafe.inventory, cafe.__dict__)):
                    with app.test_request_context("/cafe/inventory?period=" + period):
                        g.current_user = db.session.get(User, admin_id)
                        with patch.dict(namespace, {"render_template": lambda name, **context: context}):
                            context = function()
                        snapshots.append({k: context[k] for k in keys})
                        snapshots[-1]["openings"] = {r["item"].id: r["opening"] for r in context["daily_rows"]}
                if snapshots[0] != snapshots[1]:
                    raise AssertionError("Stock/financial baseline mismatch: " + period)
                print("Baseline stock and financial comparison passed: " + period, flush=True)
        # Render each common staff role on the snapshot; no real sessions or
        # transactions are changed on the running service.
        with app.app_context():
            role_users = {}
            for user in User.query.filter_by(active=True):
                for role in user.assigned_roles():
                    role_users.setdefault(role, user.id)
        for role in ("staff", "chef", "barista", "manager", "cashier", "inventory_manager", "owner"):
            if role not in role_users:
                continue
            with client.session_transaction() as session:
                session["user_id"] = role_users[role]
            for url in ("/profile", "/cafe/orders", "/cafe/to-purchase", "/cafe/reusable-assets/report-breakage"):
                response = client.get(url, follow_redirects=True)
                row = dict(role=role, url=url, status=response.status_code)
                results.append(row)
                print(json.dumps(row), flush=True)
        with app.app_context():
            db.session.remove()
            engine.dispose()
    Path(args.output).write_text(json.dumps(results, indent=2), encoding="utf-8")
    if any(row["status"] >= 500 for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
