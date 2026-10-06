"""Verify additive migration on a disposable copy, never the source database."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.models import MenuItem, User
from app.menu_navigation import build_menu_navigation, load_menu_navigation_configuration
from app.cafe import _get_item_category_names, _menu_item_category_ids
from app.menu_classification import backfill_menu_classification, legacy_customer_visibility
from app.menu_schedule import legacy_menu_item_serving_periods, menu_item_serving_periods


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    source_dir = Path(args.instance)
    with TemporaryDirectory(prefix="menu-classification-audit-") as folder:
        with closing(sqlite3.connect(f"file:{(source_dir / 'brownberries.db').as_posix()}?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            original = {row["id"]: dict(row) for row in source.execute("SELECT * FROM menu_item")}
            names = dict(source.execute("SELECT id,name FROM menu_category"))
            with closing(sqlite3.connect(str(Path(folder) / "brownberries.db"))) as target:
                source.backup(target)
        if (source_dir / "deployment_config.json").exists():
            shutil.copy2(source_dir / "deployment_config.json", folder)
        app = create_app(instance_path=folder)
        app.config.update(TESTING=True)
        try:
            with app.app_context():
                breakfast = next((key for key, name in names.items() if name.lower() == "breakfast"), 0)
                counts = {}
                items = MenuItem.query.all()
                for item in items:
                    old = SimpleNamespace(**original[item.id])
                    assert menu_item_serving_periods(item) == legacy_menu_item_serving_periods(old, breakfast)
                    assert item.customer_visible == legacy_customer_visibility(old, names)
                    for field in ("category_id", "category_ids_json", "subcategory_id", "navigation_section_id", "available", "is_deleted", "price", "prep_station"):
                        assert getattr(item, field) == getattr(old, field), (item.id, field)
                    counts[item.serving_hours] = counts.get(item.serving_hours, 0) + 1
                legacy_names = {item.id: [names[cid] for cid in _menu_item_category_ids(item) if cid in names] for item in items}
                current_names = {item.id: _get_item_category_names(item, names) for item in items}
                configuration = load_menu_navigation_configuration()
                assert build_menu_navigation(items, legacy_names, {}, configuration=configuration)["item_tokens"] == build_menu_navigation(items, current_names, {}, configuration=configuration)["item_tokens"]
                backfill_menu_classification()
                admin = next(user for user in User.query.filter_by(active=True) if user.has_role("admin"))
                admin_id = admin.id
            client = app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = admin_id
            for section in ("catalog", "navigation", "add_item", "items", "availability", "deleted_items", "display_sops"):
                response = client.get("/cafe/menu?section=" + section)
                assert response.status_code == 200, section
                assert 'name="category_ids"' not in response.get_data(as_text=True)
            print(json.dumps({"items_verified": len(original), "serving_hours": counts, "menu_sections_rendered": 7, "source_unchanged": True}))
        finally:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    main()
