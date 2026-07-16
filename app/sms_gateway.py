import json
import os
import ssl
from urllib.request import Request, urlopen

from .deploy_config import load_deployment_config


def _boolish(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _ssl_context_from_config(cfg: dict):
    ca_bundle = (cfg.get("SMS_CA_BUNDLE") or "").strip()
    allow_insecure_ssl = _boolish(cfg.get("SMS_ALLOW_INSECURE_SSL", "0"))
    try:
        if allow_insecure_ssl:
            return ssl._create_unverified_context()
        if ca_bundle and os.path.exists(ca_bundle):
            return ssl.create_default_context(cafile=ca_bundle)
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _send_via_textbee(cfg: dict, to_number: str, message: str):
    api_key = (cfg.get("TEXTBEE_API_KEY") or "").strip()
    device_id = (cfg.get("TEXTBEE_DEVICE_ID") or "").strip()
    sim_subscription_raw = (cfg.get("TEXTBEE_SIM_SUBSCRIPTION_ID") or "").strip()
    base_url = (cfg.get("TEXTBEE_BASE_URL") or "https://api.textbee.dev/api/v1").strip().rstrip("/")
    if not api_key or not device_id:
        return False, "TextBee credentials are incomplete."

    payload = {
        "recipients": [to_number],
        "message": message,
    }
    if sim_subscription_raw:
        try:
            payload["simSubscriptionId"] = int(sim_subscription_raw)
        except ValueError:
            return False, "TextBee SIM subscription ID must be numeric."

    req = Request(
        f"{base_url}/gateway/devices/{device_id}/send-sms",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "Accept": "application/json",
            "User-Agent": "BrownberriesCafeOps/1.0 (+https://brownberriescafe.com)",
        },
    )
    try:
        with urlopen(req, timeout=20, context=_ssl_context_from_config(cfg)) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if 200 <= resp.status < 300:
                return True, body or "SMS sent."
            return False, f"TextBee returned status {resp.status}."
    except Exception as exc:
        return False, f"SMS sending failed: {exc}"


def send_sms_from_config(instance_path: str, country_code: str, mobile: str, message: str):
    cfg = load_deployment_config(instance_path)
    enabled = _boolish(cfg.get("SMS_ENABLED", "0"))
    if not enabled:
        return False, "SMS gateway is disabled."

    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if not digits:
        return False, "Valid mobile number is required."
    cc = str(country_code or "+91").strip()
    if not cc.startswith("+"):
        cc = f"+{cc.lstrip('+')}"
    to_number = f"{cc}{digits}"

    return _send_via_textbee(cfg, to_number, message)
