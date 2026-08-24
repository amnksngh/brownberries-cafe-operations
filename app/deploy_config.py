import json
import os
from pathlib import Path


def load_deployment_config(instance_path: str) -> dict:
    cfg = {}
    cfg_path = Path(instance_path) / "deployment_config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except (json.JSONDecodeError, OSError):
            cfg = {}

    # Environment variables override file values.
    env_map = {
        "PUBLIC_BASE_URL": "PUBLIC_BASE_URL",
        "HOST": "HOST",
        "PORT": "PORT",
        "ENABLE_HTTPS": "ENABLE_HTTPS",
        "SSL_CERT_FILE": "SSL_CERT_FILE",
        "SSL_KEY_FILE": "SSL_KEY_FILE",
        "LAN_IP_OVERRIDE": "LAN_IP_OVERRIDE",
        "DEBUG": "DEBUG",
        "SMS_ENABLED": "SMS_ENABLED",
        "SMS_CA_BUNDLE": "SMS_CA_BUNDLE",
        "FAST2SMS_API_KEY": "FAST2SMS_API_KEY",
        "FAST2SMS_BASE_URL": "FAST2SMS_BASE_URL",
        "FAST2SMS_ROUTE": "FAST2SMS_ROUTE",
        "FAST2SMS_SENDER_ID": "FAST2SMS_SENDER_ID",
        "FAST2SMS_TEMPLATE_ID": "FAST2SMS_TEMPLATE_ID",
        "FAST2SMS_ENTITY_ID": "FAST2SMS_ENTITY_ID",
        "QR_ORDER_CUTOFF_TIME": "QR_ORDER_CUTOFF_TIME",
        "STAFF_ORDER_CUTOFF_TIME": "STAFF_ORDER_CUTOFF_TIME",
        "BREAKFAST_START_TIME": "BREAKFAST_START_TIME",
        "BREAKFAST_END_TIME": "BREAKFAST_END_TIME",
        "REGULAR_MENU_START_TIME": "REGULAR_MENU_START_TIME",
        "REGULAR_MENU_END_TIME": "REGULAR_MENU_END_TIME",
        "BREAKFAST_MENU_START_TIME": "BREAKFAST_MENU_START_TIME",
        "BREAKFAST_MENU_END_TIME": "BREAKFAST_MENU_END_TIME",
        "WORKSTATION_HOURS": "WORKSTATION_HOURS",
        "SERVICE_CHARGE_RATE": "SERVICE_CHARGE_RATE",
        "KDS_KIOSK_TOKEN": "KDS_KIOSK_TOKEN",
        "RECEPTION_KIOSK_TOKEN": "RECEPTION_KIOSK_TOKEN",
        "ATTENDANCE_CAFE_LAT": "ATTENDANCE_CAFE_LAT",
        "ATTENDANCE_CAFE_LNG": "ATTENDANCE_CAFE_LNG",
        "ATTENDANCE_RADIUS_METERS": "ATTENDANCE_RADIUS_METERS",
        "ATTENDANCE_LENIENCY_MINUTES": "ATTENDANCE_LENIENCY_MINUTES",
        "ATTENDANCE_OUTSIDE_GRACE_MINUTES": "ATTENDANCE_OUTSIDE_GRACE_MINUTES",
        "ATTENDANCE_OFFLINE_GRACE_MINUTES": "ATTENDANCE_OFFLINE_GRACE_MINUTES",
        "ATTENDANCE_LOCATION_FAILURE_GRACE_MINUTES": "ATTENDANCE_LOCATION_FAILURE_GRACE_MINUTES",
    }
    for key, env_name in env_map.items():
        val = os.getenv(env_name, "").strip()
        if val:
            cfg[key] = val
    return cfg


def save_deployment_config(instance_path: str, updates: dict) -> dict:
    cfg_path = Path(instance_path) / "deployment_config.json"
    current = {}
    if cfg_path.exists():
        try:
            current = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                current = {}
        except (json.JSONDecodeError, OSError):
            current = {}
    merged = dict(current)
    merged.update(updates or {})
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged
