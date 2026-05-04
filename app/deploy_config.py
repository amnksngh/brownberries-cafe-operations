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
    }
    for key, env_name in env_map.items():
        val = os.getenv(env_name, "").strip()
        if val:
            cfg[key] = val
    return cfg
