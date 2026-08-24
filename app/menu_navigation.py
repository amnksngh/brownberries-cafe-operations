"""Shared two-level menu navigation for public and staff ordering surfaces."""

from __future__ import annotations

from datetime import datetime, timedelta
import re

from .extensions import db
from .models import CafeOrder, CafeOrderItem


MENU_GROUPS = (
    (
        "food",
        "Food",
        (
            ("starters", "Starters"),
            ("breakfast", "Breakfast"),
            ("south-indian", "South Indian"),
            ("pizza", "Pizza"),
            ("burgers-sandwiches", "Burgers & Sandwiches"),
            ("pasta", "Pasta"),
            ("indo-chinese", "Indo-Chinese"),
            ("soups", "Soups"),
            ("combos", "Combos"),
            ("other-food", "Other Food"),
        ),
    ),
    (
        "beverages",
        "Beverages",
        (
            ("hot-coffee", "Hot Coffee"),
            ("cold-coffee", "Cold Coffee"),
            ("tea", "Tea"),
            ("milkshakes", "Milkshakes"),
            ("fresh-juices", "Fresh Juices"),
            ("traditional-drinks", "Traditional Drinks"),
            ("coolers", "Coolers"),
            ("soft-drinks", "Soft Drinks"),
            ("other-beverages", "Other Beverages"),
        ),
    ),
    (
        "desserts",
        "Desserts",
        (
            ("ice-creams", "Ice Creams"),
            ("sundaes", "Sundaes"),
            ("coffee-desserts", "Coffee Desserts"),
            ("sweet-treats", "Sweet Treats"),
        ),
    ),
    (
        "explore",
        "Explore",
        (
            ("most-popular", "Most Popular"),
            ("brownberries-specials", "Brownberries Specials"),
            ("new-additions", "New Additions"),
            ("all-items", "All Items"),
        ),
    ),
)


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
        "breakfast",
        "burgers & sandwiches",
        "chinese",
        "indo-chinese",
        "pizza & pasta",
        "south indian",
        "starters",
    }
    beverage_name = _contains_any(
        name,
        (
            "coffee",
            "frappe",
            "juice",
            "lassi",
            "lemonade",
            "milkshake",
            "mojito",
            "smoothie",
            "tea",
        ),
    )
    is_food = bool(categories & known_food_categories) or "soup" in name
    is_dessert = "desserts" in categories or item_type == "dessert"
    is_beverage = (
        "hot & cold coffee" in categories
        or ("soups & beverages" in categories and "soup" not in name)
        or item_type == "beverage"
        or beverage_name
    )

    if is_food:
        if "starters" in categories:
            return "food", "starters"
        if "breakfast" in categories:
            return "food", "breakfast"
        if "south indian" in categories:
            return "food", "south-indian"
        if "burgers & sandwiches" in categories or _contains_any(name, ("burger", "sandwich")):
            return "food", "burgers-sandwiches"
        if "pizza & pasta" in categories:
            if _contains_any(name, ("pasta", "spaghetti", "macaroni")):
                return "food", "pasta"
            return "food", "pizza"
        if categories & {"chinese", "indo-chinese"}:
            return "food", "indo-chinese"
        if "soup" in name:
            return "food", "soups"
        if _contains_any(name, ("combo", "thali")):
            return "food", "combos"
        return "food", "other-food"

    if is_dessert and "milkshake" not in name:
        if "ice cream" in name:
            return "desserts", "ice-creams"
        if "sundae" in name:
            return "desserts", "sundaes"
        if _contains_any(name, ("affogato", "tiramisu", "coffee dessert")):
            return "desserts", "coffee-desserts"
        return "desserts", "sweet-treats"

    if is_beverage:
        if _contains_any(name, ("milkshake", "smoothie")):
            return "beverages", "milkshakes"
        if "juice" in name:
            return "beverages", "fresh-juices"
        if "tea" in name:
            return "beverages", "tea"
        if _contains_word(name, ("lassi", "chaas", "buttermilk", "jaljeera")):
            return "beverages", "traditional-drinks"
        if _contains_any(name, ("cooler", "mojito", "lemonade")):
            return "beverages", "coolers"
        if _contains_any(name, ("coke", "fanta", "pepsi", "soda", "soft drink", "sprite")):
            return "beverages", "soft-drinks"
        if _contains_any(name, ("cold", "iced", "frappe")):
            return "beverages", "cold-coffee"
        if "coffee" in name or "hot & cold coffee" in categories:
            return "beverages", "hot-coffee"
        return "beverages", "other-beverages"

    return "food", "other-food"


def build_menu_navigation(
    menu_items,
    item_category_names_map: dict[int, list[str]],
    item_frequency: dict[int, int] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Build navigation tabs and per-item filter tokens without changing catalog data."""
    items = list(menu_items)
    frequency = item_frequency or {}
    current_time = now or datetime.utcnow()
    new_cutoff = current_time - timedelta(days=45)
    popular_ids = {
        item.id
        for item in sorted(
            items,
            key=lambda row: (-int(frequency.get(row.id, 0)), (row.name or "").lower()),
        )[:10]
        if int(frequency.get(item.id, 0)) > 0
    }

    item_tokens: dict[int, list[str]] = {}
    item_badges: dict[int, list[str]] = {}
    counts: dict[str, int] = {}
    for item in items:
        group_key, section_key = _permanent_destination(
            item,
            item_category_names_map.get(item.id, []),
        )
        tokens = [f"{group_key}:{section_key}", "explore:all-items"]
        badges = []
        if item.id in popular_ids:
            tokens.append("explore:most-popular")
            badges.append("Most Popular")
        if bool(getattr(item, "is_brownberries_special", False)):
            tokens.append("explore:brownberries-specials")
            badges.append("Brownberries Special")
        created_at = getattr(item, "created_at", None)
        if created_at:
            comparable_created_at = created_at.replace(tzinfo=None) if created_at.tzinfo else created_at
            if comparable_created_at >= new_cutoff:
                tokens.append("explore:new-additions")
                badges.append("New")
        item_tokens[item.id] = tokens
        item_badges[item.id] = badges
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1

    groups = []
    for group_key, group_label, sections in MENU_GROUPS:
        section_rows = [
            {
                "key": section_key,
                "label": section_label,
                "count": counts.get(f"{group_key}:{section_key}", 0),
            }
            for section_key, section_label in sections
        ]
        populated = [row for row in section_rows if row["count"] > 0]
        default_section = (
            "starters"
            if group_key == "food" and counts.get("food:starters", 0) > 0
            else (populated[0]["key"] if populated else section_rows[0]["key"])
        )
        groups.append(
            {
                "key": group_key,
                "label": group_label,
                "sections": populated or section_rows[:1],
                "default_section": default_section,
            }
        )

    return {
        "groups": groups,
        "default_group": "food",
        "default_section": next(
            group["default_section"] for group in groups if group["key"] == "food"
        ),
        "item_tokens": item_tokens,
        "item_badges": item_badges,
    }
