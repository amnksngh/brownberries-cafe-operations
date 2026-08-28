"""Repair the live sellable-menu classifications after the two-level migration.

Run without --apply to preview the exact changes.  The repair is idempotent:
re-running it does not remove any category membership or touch availability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import db
from app.models import MenuCategory, MenuItem, MenuNavGroup, MenuNavSection


def category_ids(item: MenuItem) -> list[int]:
    values: list[int] = []
    try:
        raw = json.loads(item.category_ids_json or "[]")
        if isinstance(raw, list):
            values.extend(int(value) for value in raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if item.category_id and item.category_id not in values:
        values.append(item.category_id)
    return list(dict.fromkeys(values))


def run_repair(*, apply_changes: bool) -> list[str]:
    breakfast = MenuCategory.query.filter(
        db.func.lower(MenuCategory.name) == "breakfast"
    ).first()
    beverage_category = MenuCategory.query.filter(
        db.func.lower(MenuCategory.name) == "soups & beverages"
    ).first()
    if not breakfast or not beverage_category:
        raise RuntimeError("Breakfast and Soups & Beverages categories must exist.")

    section_rows = (
        db.session.query(MenuNavSection, MenuNavGroup)
        .join(MenuNavGroup, MenuNavGroup.id == MenuNavSection.group_id)
        .all()
    )
    other_beverages = next(
        (
            section
            for section, group in section_rows
            if group.slug == "beverages" and section.slug == "other-beverages"
        ),
        None,
    )

    changes: list[str] = []
    water = MenuItem.query.filter(db.func.lower(MenuItem.name) == "water").first()
    if water:
        ids = category_ids(water)
        repaired_ids = [
            value
            for value in ids
            if value not in {
                row.id
                for row in MenuCategory.query.filter(
                    db.func.lower(MenuCategory.name).in_(["other", "utility"])
                ).all()
            }
        ]
        for value in (breakfast.id, beverage_category.id):
            if value not in repaired_ids:
                repaired_ids.append(value)
        if water.category_id != beverage_category.id:
            water.category_id = beverage_category.id
            changes.append("Water: move primary category from Utility to Soups & Beverages")
        if category_ids(water) != repaired_ids:
            water.category_ids_json = json.dumps(repaired_ids)
            changes.append("Water: make customer-visible during breakfast and regular hours")
        if other_beverages and water.navigation_section_id != other_beverages.id:
            water.navigation_section_id = other_beverages.id
            changes.append("Water: assign Beverages > Other Beverages")

    protected_category_ids = {
        row.id
        for row in MenuCategory.query.filter(
            db.func.lower(MenuCategory.name).in_(["other", "utility"])
        ).all()
    }
    for item in MenuItem.query.filter(MenuItem.is_deleted.is_(False)).order_by(MenuItem.id):
        ids = category_ids(item)
        # Temporary operating override requested by the cafe: every genuine
        # menu item gets Breakfast membership, while internal Utility/Other
        # helpers such as parcel charge rows remain private.
        if not (set(ids) - protected_category_ids):
            continue
        if breakfast.id not in ids:
            ids.append(breakfast.id)
            item.category_ids_json = json.dumps(ids)
            changes.append(f"{item.name}: add temporary Breakfast membership")

    item_type_repairs = {
        "chocolate milkshake": "Beverage",
        "manchurian gravy": "Meal",
        "manchurian dry": "Meal",
    }
    for normalized_name, item_type in item_type_repairs.items():
        item = MenuItem.query.filter(db.func.lower(MenuItem.name) == normalized_name).first()
        if item and item.item_type != item_type:
            changes.append(f"{item.name}: change type from {item.item_type} to {item_type}")
            item.item_type = item_type

    if apply_changes:
        db.session.commit()
    else:
        db.session.rollback()
    return changes


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the repair. Without this flag, only print the proposed changes.",
    )
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        changes = run_repair(apply_changes=args.apply)
    mode = "Applied" if args.apply else "Would apply"
    print(f"{mode} {len(changes)} change(s).")
    for change in changes:
        print(f"- {change}")


if __name__ == "__main__":
    main()
