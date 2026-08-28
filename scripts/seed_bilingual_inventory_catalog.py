"""Seed and normalize the bilingual Brownberries inventory catalog.

The script updates matching records in place, preserving IDs, quantities,
movements, purchases, vendors, and history.  Missing records/categories are
created.  It is safe to preview repeatedly and idempotent when applied.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import db
from app.inventory_catalog import CATALOG_ITEMS, INVENTORY_CATEGORIES, ITEM_ALIASES
from app.models import InventoryCategory, InventoryItem


def english_key(value: str | None) -> str:
    value = (value or "").split("(", 1)[0].strip()
    value = unicodedata.normalize("NFKC", value).lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def canonical_key(value: str | None) -> str:
    key = english_key(value)
    canonical = ITEM_ALIASES.get(key, value or "")
    return english_key(canonical)


def next_item_code(existing_codes: set[str], next_number: int) -> tuple[str, int]:
    while True:
        code = f"INV-{next_number:05d}"
        next_number += 1
        if code not in existing_codes:
            existing_codes.add(code)
            return code, next_number


def run_seed(*, apply_changes: bool) -> dict:
    category_changes = []
    for name, (icon, color) in INVENTORY_CATEGORIES.items():
        category = InventoryCategory.query.filter(
            db.func.lower(InventoryCategory.name) == name.lower()
        ).first()
        if not category:
            category = InventoryCategory(name=name, icon=icon, color=color, active=True)
            db.session.add(category)
            category_changes.append(f"create category {name}")
            continue
        changed = False
        if not category.active:
            category.active = True
            changed = True
        # Keep any icon/color the manager already chose; only fill blanks.
        if not (category.icon or "").strip():
            category.icon = icon
            changed = True
        if not (category.color or "").strip():
            category.color = color
            changed = True
        if changed:
            category_changes.append(f"normalize category {name}")

    existing_items = InventoryItem.query.order_by(InventoryItem.id).all()
    by_key: dict[str, InventoryItem] = {}
    for row in existing_items:
        key = canonical_key(row.name)
        if key and key not in by_key:
            by_key[key] = row

    existing_codes = {row.item_code for row in existing_items if row.item_code}
    numeric_codes = [
        int(match.group(1))
        for code in existing_codes
        if (match := re.fullmatch(r"INV-(\d+)", code or ""))
    ]
    next_number = max(numeric_codes or [0]) + 1
    created = []
    updated = []

    shelf_life_defaults = {
        "Fresh Produce": 5,
        "Fruits": 5,
        "Dairy": 7,
        "Bakery & Confectionery": 4,
    }
    for entry in CATALOG_ITEMS:
        key = english_key(entry["english"])
        row = by_key.get(key)
        if not row:
            code, next_number = next_item_code(existing_codes, next_number)
            row = InventoryItem(
                item_code=code,
                area=entry["area"],
                name=entry["name"],
                category_name=entry["category"],
                subcategory_name=entry["subcategory"],
                item_type=entry["item_type"],
                unit=entry["unit"],
                current_amount=0,
                required_amount=0,
                reorder_level=0,
                average_daily_usage=0,
                purchase_price=0,
                shelf_life_days=shelf_life_defaults.get(entry["category"]),
                expiry_tracking=entry["item_type"] == "perishable",
                storage_location=entry["storage"],
                note="Bilingual starter catalog; verify preferred brand and reorder level.",
            )
            db.session.add(row)
            by_key[key] = row
            created.append(entry["name"])
            continue

        changes = []
        desired = {
            "name": entry["name"],
            "category_name": entry["category"],
            "subcategory_name": entry["subcategory"],
            "item_type": entry["item_type"],
            "area": entry["area"],
            "unit": entry["unit"],
            "storage_location": entry["storage"],
            "expiry_tracking": entry["item_type"] == "perishable",
        }
        for field, value in desired.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changes.append(field)
        if row.shelf_life_days is None and entry["category"] in shelf_life_defaults:
            row.shelf_life_days = shelf_life_defaults[entry["category"]]
            changes.append("shelf_life_days")
        if not row.item_code:
            row.item_code, next_number = next_item_code(existing_codes, next_number)
            changes.append("item_code")
        if changes:
            updated.append(f"{entry['name']}: {', '.join(changes)}")

    if apply_changes:
        db.session.commit()
    else:
        db.session.rollback()
    return {
        "categories": category_changes,
        "created": created,
        "updated": updated,
        "catalog_total": len(CATALOG_ITEMS),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the seed. Without this flag, only preview the proposed changes.",
    )
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        result = run_seed(apply_changes=args.apply)
    mode = "Applied" if args.apply else "Would apply"
    print(
        f"{mode}: {len(result['categories'])} category change(s), "
        f"{len(result['created'])} new item(s), {len(result['updated'])} normalized item(s); "
        f"{result['catalog_total']} catalog items total."
    )
    for group in ("categories", "updated", "created"):
        for change in result[group]:
            print(f"- {change}")


if __name__ == "__main__":
    main()
