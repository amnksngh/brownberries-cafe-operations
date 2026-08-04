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


def menu_item_window_is_open(item, at_time: time | None = None) -> bool:
    return workstation_window_is_open(getattr(item, "prep_station", None), at_time)
