import json
import os
import ssl
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .deploy_config import load_deployment_config


def _boolish(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _ssl_context_from_config(cfg: dict):
    """Build a verified TLS context, with an optional local CA bundle."""
    ca_bundle = (cfg.get("SMS_CA_BUNDLE") or "").strip()
    try:
        if ca_bundle and os.path.exists(ca_bundle):
            return ssl.create_default_context(cafile=ca_bundle)
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _response_message(body: str, fallback: str) -> str:
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return body.strip() or fallback

    if isinstance(payload, dict):
        for key in ("message", "error", "msg"):
            value = payload.get(key)
            if isinstance(value, list):
                return "; ".join(str(item) for item in value)
            if value:
                return str(value)
    return body.strip() or fallback


def _send_via_fast2sms(cfg: dict, to_number: str, message: str):
    api_key = (cfg.get("FAST2SMS_API_KEY") or "").strip()
    base_url = (cfg.get("FAST2SMS_BASE_URL") or "https://www.fast2sms.com/dev/bulkV2").strip()
    route = (cfg.get("FAST2SMS_ROUTE") or "q").strip().lower()
    if not api_key:
        return False, "Fast2SMS API key is missing."
    if route not in {"q", "dlt_manual"}:
        return False, "Fast2SMS route must be Quick SMS or DLT Manual."

    payload = {
        "route": route,
        "message": message,
        "numbers": to_number,
        "sms_details": "1",
    }
    if route == "dlt_manual":
        sender_id = (cfg.get("FAST2SMS_SENDER_ID") or "").strip()
        template_id = (cfg.get("FAST2SMS_TEMPLATE_ID") or "").strip()
        entity_id = (cfg.get("FAST2SMS_ENTITY_ID") or "").strip()
        if not sender_id:
            return False, "Fast2SMS Sender ID is required for DLT Manual."
        payload["sender_id"] = sender_id
        if template_id:
            payload["template_id"] = template_id
        if entity_id:
            payload["entity_id"] = entity_id

    req = Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BrownberriesCafeOps/1.0 (+https://brownberriescafe.com)",
        },
    )
    try:
        with urlopen(req, timeout=20, context=_ssl_context_from_config(cfg)) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if not 200 <= resp.status < 300:
                return False, f"Fast2SMS returned HTTP {resp.status}: {_response_message(body, 'request failed')}"
            try:
                result = json.loads(body or "{}")
            except json.JSONDecodeError:
                result = {}
            if isinstance(result, dict) and result.get("return") is False:
                return False, f"Fast2SMS rejected the request: {_response_message(body, 'request rejected')}"
            return True, _response_message(body, "SMS sent.")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        return False, f"Fast2SMS returned HTTP {exc.code}: {_response_message(body, 'request failed')}"
    except Exception as exc:
        return False, f"SMS sending failed: {exc}"


def send_sms_from_config(instance_path: str, country_code: str, mobile: str, message: str):
    cfg = load_deployment_config(instance_path)
    if not _boolish(cfg.get("SMS_ENABLED", "0")):
        return False, "SMS gateway is disabled."

    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    cc = "".join(ch for ch in str(country_code or "+91") if ch.isdigit())
    if cc == "91" and digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if cc != "91":
        return False, "Fast2SMS currently supports Indian +91 mobile numbers only."
    if len(digits) != 10:
        return False, "Enter a valid 10-digit Indian mobile number."

    return _send_via_fast2sms(cfg, digits, message)
