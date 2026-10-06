"""Explicit menu visibility and hours; legacy classification is migration-only."""

import json

from .extensions import db
from .models import MenuCategory, MenuItem, MenuNavSection
from .menu_schedule import legacy_menu_item_serving_periods

SERVING_HOURS_OPTIONS = (
    ("regular", "Regular Hours"),
    ("breakfast", "Breakfast Hours"),
    ("both", "Breakfast and Regular Hours"),
)


def legacy_customer_visibility(item, category_names):
    # Preserve the old public boundary exactly: JSON membership, primary fallback.
    ids = []
    try:
        raw = json.loads(item.category_ids_json or "[]")
        if isinstance(raw, list):
            for value in raw:
                try:
                    ids.append(int(value))
                except (TypeError, ValueError):
                    continue
    except (TypeError, ValueError):
        pass
    if not ids and item.category_id:
        ids = [item.category_id]
    return any(
        (category_names.get(value) or "").strip().lower() not in {"", "other", "utility"}
        for value in ids
    )


def is_customer_visible(item, category_names=None):
    explicit = getattr(item, "customer_visible", None)
    if explicit is not None:
        return bool(explicit)
    if category_names is None:
        category_names = {row.id: row.name for row in MenuCategory.query.all()}
    return legacy_customer_visibility(item, category_names)


def backfill_menu_classification():
    categories = {row.id: row.name for row in MenuCategory.query.all()}
    breakfast_id = next((key for key, name in categories.items() if name.lower() == "breakfast"), 0)
    rows = MenuItem.query.filter(db.or_(MenuItem.serving_hours.is_(None), MenuItem.customer_visible.is_(None))).all()
    for item in rows:
        if item.serving_hours is None:
            periods = legacy_menu_item_serving_periods(item, breakfast_id)
            item.serving_hours = "both" if len(periods) == 2 else periods[0]
        if item.customer_visible is None:
            item.customer_visible = legacy_customer_visibility(item, categories)
    if rows:
        db.session.commit()


def navigation_label(item):
    section = item.navigation_section
    return f"{section.group.label} / {section.label}" if section and section.group else "Unassigned"


def navigation_filter_options():
    return [
        {"id": section.id, "name": f"{section.group.label} / {section.label}"}
        for section in MenuNavSection.query.filter_by(collection_kind="catalog").all()
    ]


def apply_navigation_filter(query, section_id):
    return query.filter(MenuItem.navigation_section_id == section_id) if section_id else query
