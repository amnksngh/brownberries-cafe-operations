"""Configurable two-level menu navigation for public and staff ordering surfaces."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import re

from .extensions import db
from .models import CafeOrder, CafeOrderItem, MenuCategory, MenuItem, MenuNavGroup


MENU_GROUPS = (
    ("food", "Food", (("starters", "Starters"), ("breakfast", "Breakfast"),
        ("south-indian", "South Indian"), ("pizza", "Pizza"),
        ("burgers-sandwiches", "Burgers & Sandwiches"), ("pasta", "Pasta"),
        ("indo-chinese", "Indo-Chinese"), ("soups", "Soups"),
        ("combos", "Combos"), ("other-food", "Other Food"))),
    ("beverages", "Beverages", (("hot-coffee", "Hot Coffee"),
        ("cold-coffee", "Cold Coffee"), ("tea", "Tea"),
        ("milkshakes", "Milkshakes"), ("fresh-juices", "Fresh Juices"),
        ("traditional-drinks", "Traditional Drinks"), ("coolers", "Coolers"),
        ("soft-drinks", "Soft Drinks"), ("other-beverages", "Other Beverages"))),
    ("desserts", "Desserts", (("ice-creams", "Ice Creams"),
        ("sundaes", "Sundaes"), ("coffee-desserts", "Coffee Desserts"),
        ("sweet-treats", "Sweet Treats"))),
    ("explore", "Explore", (("most-popular", "Most Popular"),
        ("brownberries-specials", "Brownberries Specials"),
        ("new-additions", "New Additions"), ("all-items", "All Items"))),
)

COLLECTION_KINDS = {
    "catalog": "Catalog subcategory",
    "most_popular": "Most Popular (automatic)",
    "specials": "Brownberries Specials (automatic)",
    "new_additions": "New Additions (automatic)",
    "all_items": "All Items (automatic)",
}
DEFAULT_SECTION_BY_GROUP = {
    "food": "starters", "beverages": "hot-coffee",
    "desserts": "ice-creams", "explore": "all-items",
}
EXPLORE_COLLECTION_KINDS = {
    "most-popular": "most_popular", "brownberries-specials": "specials",
    "new-additions": "new_additions", "all-items": "all_items",
}


def recent_paid_item_frequency(days: int = 90) -> dict[int, int]:
    """Return recent paid-order quantities used by the Most Popular collection."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    rows = (
        db.session.query(
            CafeOrderItem.menu_item_id,
            db.func.coalesce(db.func.sum(CafeOrderItem.quantity), 0).label("order_qty"),
        )
        .join(CafeOrder, CafeOrder.id == CafeOrderItem.order_id)
        .filter(CafeOrder.status == "paid", CafeOrder.created_at >= cutoff)
        .group_by(CafeOrderItem.menu_item_id)
        .all()
    )
    return {int(row.menu_item_id): int(row.order_qty or 0) for row in rows}


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    return any(term in value for term in terms)


def _contains_word(value: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", value) for term in terms)


def _permanent_destination(item, category_names: list[str]) -> tuple[str, str]:
    name = (item.name or "").strip().lower()
    item_type = (item.item_type or "").strip().lower()
    categories = {str(category).strip().lower() for category in category_names}
    known_food_categories = {
        "breakfast", "burgers & sandwiches", "chinese", "indo-chinese",
        "pizza & pasta", "south indian", "starters",
    }
    beverage_name = _contains_any(
        name, ("coffee", "frappe", "juice", "lassi", "lemonade", "milkshake",
               "mojito", "smoothie", "tea"),
    )
    is_food = bool(categories & known_food_categories) or "soup" in name
    is_dessert = "desserts" in categories or item_type == "dessert"
    is_beverage = (
        "hot & cold coffee" in categories
        or ("soups & beverages" in categories and "soup" not in name)
        or item_type == "beverage" or beverage_name
    )
    if is_food:
        if "starters" in categories: return "food", "starters"
        if "breakfast" in categories: return "food", "breakfast"
        if "south indian" in categories: return "food", "south-indian"
        if "burgers & sandwiches" in categories or _contains_any(name, ("burger", "sandwich")):
            return "food", "burgers-sandwiches"
        if "pizza & pasta" in categories:
            return ("food", "pasta") if _contains_any(name, ("pasta", "spaghetti", "macaroni")) else ("food", "pizza")
        if categories & {"chinese", "indo-chinese"}: return "food", "indo-chinese"
        if "soup" in name: return "food", "soups"
        if _contains_any(name, ("combo", "thali")): return "food", "combos"
        return "food", "other-food"
    if is_dessert and "milkshake" not in name:
        if "ice cream" in name: return "desserts", "ice-creams"
        if "sundae" in name: return "desserts", "sundaes"
        if _contains_any(name, ("affogato", "tiramisu", "coffee dessert")):
            return "desserts", "coffee-desserts"
        return "desserts", "sweet-treats"
    if is_beverage:
        if _contains_any(name, ("milkshake", "smoothie")): return "beverages", "milkshakes"
        if "juice" in name: return "beverages", "fresh-juices"
        if "tea" in name: return "beverages", "tea"
        if _contains_word(name, ("lassi", "chaas", "buttermilk", "jaljeera")):
            return "beverages", "traditional-drinks"
        if _contains_any(name, ("cooler", "mojito", "lemonade")): return "beverages", "coolers"
        if _contains_any(name, ("coke", "fanta", "pepsi", "soda", "soft drink", "sprite")):
            return "beverages", "soft-drinks"
        if _contains_any(name, ("cold", "iced", "frappe")): return "beverages", "cold-coffee"
        if "coffee" in name or "hot & cold coffee" in categories: return "beverages", "hot-coffee"
        return "beverages", "other-beverages"
    return "food", "other-food"


def _fallback_configuration() -> dict:
    groups = []
    catalog_by_token = {}
    collection_tokens: dict[str, list[str]] = {kind: [] for kind in COLLECTION_KINDS}
    for group_order, (group_key, group_label, sections) in enumerate(MENU_GROUPS):
        section_rows = []
        for section_order, (section_key, section_label) in enumerate(sections):
            kind = EXPLORE_COLLECTION_KINDS.get(section_key, "catalog")
            row = {"id": None, "key": section_key, "label": section_label,
                   "kind": kind, "display_order": section_order}
            section_rows.append(row)
            token = f"{group_key}:{section_key}"
            if kind == "catalog":
                catalog_by_token[token] = row
            collection_tokens.setdefault(kind, []).append(token)
        groups.append({"id": None, "key": group_key, "label": group_label,
                       "display_order": group_order, "sections": section_rows,
                       "default_section": DEFAULT_SECTION_BY_GROUP[group_key]})
    return {"groups": groups, "default_group": "explore", "section_by_id": {},
            "catalog_by_token": catalog_by_token, "collection_tokens": collection_tokens}


def load_menu_navigation_configuration(*, include_inactive: bool = False) -> dict:
    """Load the administrator-managed navigation structure."""
    groups = MenuNavGroup.query.order_by(MenuNavGroup.display_order, MenuNavGroup.id).all()
    if not groups:
        return _fallback_configuration()
    result_groups = []
    section_by_id = {}
    catalog_by_token = {}
    collection_tokens: dict[str, list[str]] = {kind: [] for kind in COLLECTION_KINDS}
    for group in groups:
        if not include_inactive and not group.active:
            continue
        section_models = sorted(group.sections, key=lambda row: (row.display_order, row.id))
        if not include_inactive:
            section_models = [row for row in section_models if row.active]
        if not section_models:
            continue
        default_model = next((row for row in section_models if row.is_default), section_models[0])
        section_rows = []
        for section in section_models:
            row = {"id": section.id, "key": section.slug, "label": section.label,
                   "kind": section.collection_kind, "display_order": section.display_order,
                   "active": section.active, "is_default": section.is_default}
            section_rows.append(row)
            section_by_id[section.id] = {**row, "group_key": group.slug}
            token = f"{group.slug}:{section.slug}"
            if section.collection_kind == "catalog":
                catalog_by_token[token] = row
            collection_tokens.setdefault(section.collection_kind, []).append(token)
        result_groups.append({"id": group.id, "key": group.slug, "label": group.label,
                              "display_order": group.display_order, "sections": section_rows,
                              "default_section": default_model.slug,
                              "active": group.active, "is_global_default": group.is_global_default})
    if not result_groups:
        return _fallback_configuration()
    default_group = next((row["key"] for row in result_groups if row.get("is_global_default")), result_groups[0]["key"])
    return {"groups": result_groups, "default_group": default_group,
            "section_by_id": section_by_id, "catalog_by_token": catalog_by_token,
            "collection_tokens": collection_tokens}


def ensure_menu_navigation_seeded() -> None:
    """Seed the initial structure and classify existing items once."""
    changed = False
    if MenuNavGroup.query.count() == 0:
        from .models import MenuNavSection
        for group_order, (group_key, group_label, sections) in enumerate(MENU_GROUPS):
            group = MenuNavGroup(slug=group_key, label=group_label,
                                 display_order=(group_order + 1) * 10,
                                 active=True, is_global_default=group_key == "explore")
            db.session.add(group)
            db.session.flush()
            for section_order, (section_key, section_label) in enumerate(sections):
                db.session.add(MenuNavSection(
                    group_id=group.id, slug=section_key, label=section_label,
                    display_order=(section_order + 1) * 10, active=True,
                    is_default=section_key == DEFAULT_SECTION_BY_GROUP[group_key],
                    collection_kind=EXPLORE_COLLECTION_KINDS.get(section_key, "catalog"),
                ))
        db.session.flush()
        changed = True
    configuration = load_menu_navigation_configuration(include_inactive=True)
    category_names = {row.id: row.name for row in MenuCategory.query.all()}
    for item in MenuItem.query.filter(MenuItem.navigation_section_id.is_(None)).all():
        category_ids = [item.category_id]
        if item.category_ids_json:
            try:
                category_ids.extend(int(value) for value in json.loads(item.category_ids_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        destination = _permanent_destination(
            item, [category_names[value] for value in dict.fromkeys(category_ids) if value in category_names]
        )
        section = configuration["catalog_by_token"].get(f"{destination[0]}:{destination[1]}")
        if section and section.get("id"):
            item.navigation_section_id = section["id"]
            changed = True
    if changed:
        db.session.commit()


def build_menu_navigation(
    menu_items, item_category_names_map: dict[int, list[str]],
    item_frequency: dict[int, int] | None = None, *, now: datetime | None = None,
    configuration: dict | None = None,
) -> dict:
    """Build navigation tabs and per-item filter tokens from managed configuration."""
    items = list(menu_items)
    config = configuration or _fallback_configuration()
    frequency = item_frequency or {}
    new_cutoff = (now or datetime.utcnow()) - timedelta(days=45)
    popular_ids = {
        item.id for item in sorted(items, key=lambda row: (-int(frequency.get(row.id, 0)), (row.name or "").lower()))[:10]
        if int(frequency.get(item.id, 0)) > 0
    }
    item_tokens, item_badges, counts = {}, {}, {}
    for item in items:
        assigned = config["section_by_id"].get(getattr(item, "navigation_section_id", None))
        if assigned and assigned["kind"] == "catalog":
            permanent_token = f"{assigned['group_key']}:{assigned['key']}"
        else:
            destination = _permanent_destination(item, item_category_names_map.get(item.id, []))
            candidate = f"{destination[0]}:{destination[1]}"
            permanent_token = candidate if candidate in config["catalog_by_token"] else None
        tokens = [permanent_token] if permanent_token else []
        tokens.extend(config["collection_tokens"].get("all_items", []))
        badges = []
        if item.id in popular_ids:
            tokens.extend(config["collection_tokens"].get("most_popular", [])); badges.append("Most Popular")
        if bool(getattr(item, "is_brownberries_special", False)):
            tokens.extend(config["collection_tokens"].get("specials", [])); badges.append("Brownberries Special")
        created_at = getattr(item, "created_at", None)
        if created_at:
            comparable = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
            if comparable >= new_cutoff:
                tokens.extend(config["collection_tokens"].get("new_additions", [])); badges.append("New")
        item_tokens[item.id] = list(dict.fromkeys(token for token in tokens if token))
        item_badges[item.id] = badges
        for token in item_tokens[item.id]: counts[token] = counts.get(token, 0) + 1

    groups = []
    for group in config["groups"]:
        sections = [{"id": row.get("id"), "key": row["key"], "label": row["label"],
                     "count": counts.get(f"{group['key']}:{row['key']}", 0)} for row in group["sections"]]
        populated = [row for row in sections if row["count"] > 0]
        if not populated: continue
        configured_default = group["default_section"]
        default_section = configured_default if any(row["key"] == configured_default for row in populated) else populated[0]["key"]
        groups.append({"id": group.get("id"), "key": group["key"], "label": group["label"],
                       "sections": populated, "default_section": default_section})
    if not groups:
        return {"groups": [], "default_group": "", "default_section": "",
                "item_tokens": item_tokens, "item_badges": item_badges}
    selected_group = next((row for row in groups if row["key"] == config["default_group"]), groups[0])
    return {"groups": groups, "default_group": selected_group["key"],
            "default_section": selected_group["default_section"],
            "item_tokens": item_tokens, "item_badges": item_badges}
