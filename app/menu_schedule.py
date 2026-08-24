"""Shared menu visibility schedules for preparation workstations.

The schedule is intentionally kept in deployment_config.json rather than the
database. It is operational configuration that belongs on the live server,
not in source control.
"""

import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from flask import current_app, g

from .deploy_config import load_deployment_config


DEFAULT_WORKSTATION_START_TIME = "00:00"
DEFAULT_WORKSTATION_END_TIME = "23:59"
DEFAULT_REGULAR_START_TIME = "11:00"
DEFAULT_REGULAR_END_TIME = "23:00"
DEFAULT_BREAKFAST_START_TIME = "08:00"
DEFAULT_BREAKFAST_END_TIME = "12:00"
MENU_SERVING_PERIODS = (
    ("regular", "Regular Hours"),
    ("breakfast", "Breakfast Hours"),
)
IST_TZ = ZoneInfo("Asia/Kolkata")


def _valid_time(value: str | None, fallback: str) -> str:
    try:
        return datetime.strptime((value or "").strip(), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        return fallback


def workstation_schedule_map() -> dict[str, dict[str, str]]:
    """Return normalized workstation hours keyed by workstation slug."""
    cached = getattr(g, "workstation_schedule_map", None)
    if cached is not None:
        return cached

    cfg = load_deployment_config(current_app.instance_path)
    raw = cfg.get("WORKSTATION_HOURS", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    schedules: dict[str, dict[str, str]] = {}
    for slug, value in raw.items():
        if not isinstance(value, dict):
            value = {}
        normalized_slug = str(slug or "").strip().lower()
        if not normalized_slug:
            continue
        schedules[normalized_slug] = {
            "start_time": _valid_time(value.get("start_time"), DEFAULT_WORKSTATION_START_TIME),
            "end_time": _valid_time(value.get("end_time"), DEFAULT_WORKSTATION_END_TIME),
        }

    g.workstation_schedule_map = schedules
    return schedules


def menu_period_settings() -> dict[str, dict[str, str]]:
    """Return the two configurable item-serving windows in IST."""
    cached = getattr(g, "menu_period_settings", None)
    if cached is not None:
        return cached
    cfg = load_deployment_config(current_app.instance_path)
    settings = {
        "regular": {
            "label": "Regular Hours",
            "start_time": _valid_time(cfg.get("REGULAR_MENU_START_TIME"), DEFAULT_REGULAR_START_TIME),
            "end_time": _valid_time(cfg.get("REGULAR_MENU_END_TIME"), DEFAULT_REGULAR_END_TIME),
        },
        "breakfast": {
            "label": "Breakfast Hours",
            "start_time": _valid_time(
                cfg.get("BREAKFAST_MENU_START_TIME") or cfg.get("BREAKFAST_START_TIME"),
                DEFAULT_BREAKFAST_START_TIME,
            ),
            "end_time": _valid_time(
                cfg.get("BREAKFAST_MENU_END_TIME") or cfg.get("BREAKFAST_END_TIME"),
                DEFAULT_BREAKFAST_END_TIME,
            ),
        },
    }
    g.menu_period_settings = settings
    return settings


def time_window_is_open(start_value: str, end_value: str, at_time: time) -> bool:
    """Evaluate a same-day or overnight half-open time window."""
    start = datetime.strptime(start_value, "%H:%M").time()
    end = datetime.strptime(end_value, "%H:%M").time()
    if start == end:
        return False
    if start < end:
        return start <= at_time < end
    return at_time >= start or at_time < end


def menu_period_window_is_open(period: str | None, at_time: time | None = None) -> bool:
    normalized_period = (period or "regular").strip().lower()
    if normalized_period not in {key for key, _ in MENU_SERVING_PERIODS}:
        normalized_period = "regular"
    schedule = menu_period_settings()[normalized_period]
    now = at_time or datetime.now(IST_TZ).time()
    return time_window_is_open(schedule["start_time"], schedule["end_time"], now)


def breakfast_category_id() -> int | None:
    """Return the protected Breakfast category id, cached for this request."""
    cached = getattr(g, "breakfast_category_id", None)
    if cached is not None:
        return cached or None
    from .extensions import db
    from .models import MenuCategory

    row = MenuCategory.query.filter(db.func.lower(MenuCategory.name) == "breakfast").first()
    g.breakfast_category_id = row.id if row else 0
    return row.id if row else None


def menu_item_serving_periods(item, breakfast_id: int | None = None) -> tuple[str, ...]:
    """Derive item windows from its category membership.

    Breakfast alone means Breakfast Hours; Breakfast plus any other category
    means both windows; all other category combinations mean Regular Hours.
    """
    if breakfast_id is None:
        breakfast_id = breakfast_category_id()
    category_ids = []
    raw_ids = getattr(item, "category_ids_json", None)
    if raw_ids:
        try:
            parsed = json.loads(raw_ids)
            if isinstance(parsed, list):
                for value in parsed:
                    try:
                        category_ids.append(int(value))
                    except (TypeError, ValueError):
                        continue
        except (TypeError, json.JSONDecodeError):
            category_ids = []
    primary_id = getattr(item, "category_id", None)
    if primary_id and int(primary_id) not in category_ids:
        category_ids.append(int(primary_id))
    unique_ids = set(category_ids)
    if breakfast_id and breakfast_id in unique_ids:
        return ("breakfast", "regular") if unique_ids - {breakfast_id} else ("breakfast",)
    return ("regular",)


def workstation_window_is_open(slug: str | None, at_time: time | None = None) -> bool:
    """Return whether ordering for a workstation is currently open in IST.

    An unassigned menu item is always available. Missing workstation settings
    use an all-day window for backwards compatibility.
    """
    normalized_slug = (slug or "").strip().lower()
    if not normalized_slug:
        return True

    schedule = workstation_schedule_map().get(
        normalized_slug,
        {
            "start_time": DEFAULT_WORKSTATION_START_TIME,
            "end_time": DEFAULT_WORKSTATION_END_TIME,
        },
    )
    start = datetime.strptime(schedule["start_time"], "%H:%M").time()
    end = datetime.strptime(schedule["end_time"], "%H:%M").time()
    if start == end:
        return False

    now = at_time or datetime.now(IST_TZ).time()
    if start < end:
        return start <= now < end
    return now >= start or now < end


def menu_item_window_is_open(
    item,
    at_time: time | None = None,
    *,
    breakfast_id: int | None = None,
) -> bool:
    serving_open = any(
        menu_period_window_is_open(period, at_time)
        for period in menu_item_serving_periods(item, breakfast_id)
    )
    return serving_open and workstation_window_is_open(getattr(item, "prep_station", None), at_time)
