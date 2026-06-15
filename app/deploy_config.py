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
        "SMS_PROVIDER": "SMS_PROVIDER",
        "SMS_ENABLED": "SMS_ENABLED",
        "SMS_FROM": "SMS_FROM",
        "TWILIO_FROM_NUMBER": "TWILIO_FROM_NUMBER",
        "TWILIO_ACCOUNT_SID": "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN": "TWILIO_AUTH_TOKEN",
        "SMS_CA_BUNDLE": "SMS_CA_BUNDLE",
        "SMS_ALLOW_INSECURE_SSL": "SMS_ALLOW_INSECURE_SSL",
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
