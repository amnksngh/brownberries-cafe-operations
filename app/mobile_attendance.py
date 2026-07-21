import hashlib
import secrets
from datetime import date, datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, g, jsonify, request
from werkzeug.security import check_password_hash

from .attendance_logic import attendance_status_label, refresh_attendance_row, shift_window_for_profile
from .deploy_config import load_deployment_config
from .extensions import db
from .models import StaffAttendance, StaffMobileSession, User

bp = Blueprint("mobile_attendance", __name__, url_prefix="/api/mobile/attendance")

IST_TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_ATTENDANCE_CAFE_LAT = 25.207989477704068
DEFAULT_ATTENDANCE_CAFE_LNG = 80.87374457551877
DEFAULT_ATTENDANCE_RADIUS_METERS = 120.0
OFFLINE_GRACE_MINUTES = 60
LOCATION_FAILURE_GRACE_MINUTES = 5
HEARTBEAT_INTERVAL_SECONDS = 60


def _attendance_settings():
    cfg = load_deployment_config(current_app.instance_path)
    try:
        cafe_lat = float(cfg.get("ATTENDANCE_CAFE_LAT") or DEFAULT_ATTENDANCE_CAFE_LAT)
    except (TypeError, ValueError):
        cafe_lat = DEFAULT_ATTENDANCE_CAFE_LAT
    try:
        cafe_lng = float(cfg.get("ATTENDANCE_CAFE_LNG") or DEFAULT_ATTENDANCE_CAFE_LNG)
    except (TypeError, ValueError):
        cafe_lng = DEFAULT_ATTENDANCE_CAFE_LNG
    try:
        radius_m = float(cfg.get("ATTENDANCE_RADIUS_METERS") or DEFAULT_ATTENDANCE_RADIUS_METERS)
    except (TypeError, ValueError):
        radius_m = DEFAULT_ATTENDANCE_RADIUS_METERS
    return {
        "cafe_lat": cafe_lat,
        "cafe_lng": cafe_lng,
        "radius_m": max(20.0, radius_m),
    }


def _attendance_haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    radius_m = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_m * c


def _attendance_distance_from_cafe(lat: float | None, lng: float | None) -> float | None:
    if lat is None or lng is None:
        return None
    settings = _attendance_settings()
    return _attendance_haversine_m(
        settings["cafe_lat"],
        settings["cafe_lng"],
        float(lat),
        float(lng),
    )


def _active_attendance_session_for_user(user_id: int):
    return (
        StaffAttendance.query.filter(
            StaffAttendance.user_id == user_id,
            StaffAttendance.check_in_at.is_not(None),
            StaffAttendance.check_out_at.is_(None),
        )
        .order_by(StaffAttendance.attendance_date.desc(), StaffAttendance.check_in_at.desc())
        .first()
    )


def _is_staff_attendance_user(user: User | None) -> bool:
    if not user or not user.active:
        return False
    return user.email not in {"qr.guest@brownberries.local", "delivery.guest@brownberries.local"}


def _json_payload():
    payload = request.get_json(silent=True) or {}
    return payload if isinstance(payload, dict) else {}


def _api_error(message: str, status: int = 400):
    return jsonify({"ok": False, "message": message}), status


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _extract_bearer_token() -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _parse_client_datetime(value: str | None) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return datetime.now(IST_TZ).replace(tzinfo=None)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(IST_TZ).replace(tzinfo=None)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(IST_TZ).replace(tzinfo=None)


def _mobile_policy_payload() -> dict:
    return {
        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
        "offline_checkout_grace_minutes": OFFLINE_GRACE_MINUTES,
        "location_failure_grace_minutes": LOCATION_FAILURE_GRACE_MINUTES,
    }


def _user_shift_payload(user: User | None) -> dict:
    profile = user.staff_profile if user else None
    shift_start, shift_end = shift_window_for_profile(profile)
    return {
        "shift_start": shift_start.strftime("%H:%M"),
        "shift_end": shift_end.strftime("%H:%M"),
    }


def _attendance_row_payload(row: StaffAttendance | None) -> dict | None:
    if not row:
        return None
    return {
        "attendance_id": row.id,
        "attendance_date": row.attendance_date.isoformat() if row.attendance_date else "",
        "status": row.status or "",
        "status_label": attendance_status_label(row.status),
        "check_in_at": row.check_in_at.isoformat() if row.check_in_at else None,
        "check_out_at": row.check_out_at.isoformat() if row.check_out_at else None,
        "check_in_method": row.check_in_method or "",
        "check_out_method": row.check_out_method or "",
        "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
        "check_in_distance_m": float(row.check_in_distance_m or 0) if row.check_in_distance_m is not None else None,
        "last_heartbeat_distance_m": float(row.last_heartbeat_distance_m or 0) if row.last_heartbeat_distance_m is not None else None,
        "mobile_device_id": row.mobile_device_id or "",
        "auto_checkout_reason": row.auto_checkout_reason or "",
    }


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "roles": user.assigned_roles() if hasattr(user, "assigned_roles") else [user.role],
    }


def _bootstrap_payload(user: User, session_row: StaffMobileSession | None = None) -> dict:
    settings = _attendance_settings()
    active_row = _active_attendance_session_for_user(user.id)
    return {
        "user": _user_payload(user),
        "geofence": settings,
        "policy": _mobile_policy_payload(),
        "shift": _user_shift_payload(user),
        "active_session": _attendance_row_payload(active_row),
        "mobile_session": {
            "device_id": session_row.device_id if session_row else "",
            "device_name": session_row.device_name if session_row else "",
            "platform": session_row.platform if session_row else "android",
            "app_version": session_row.app_version if session_row else "",
            "last_seen_at": session_row.last_seen_at.isoformat() if session_row and session_row.last_seen_at else None,
        } if session_row else None,
        "server_time_ist": datetime.now(IST_TZ).isoformat(),
    }


def mobile_token_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            return _api_error("Missing mobile attendance token.", 401)
        session_row = StaffMobileSession.query.filter_by(token_hash=_token_hash(token), active=True).first()
        if not session_row or not session_row.user or not _is_staff_attendance_user(session_row.user):
            return _api_error("Invalid or expired mobile attendance token.", 401)
        g.mobile_user = session_row.user
        g.mobile_session = session_row
        return view(*args, **kwargs)

    return wrapped


def _update_mobile_session_status(
    session_row: StaffMobileSession,
    *,
    lat: float | None = None,
    lng: float | None = None,
    distance_m: float | None = None,
    status: str = "",
    message: str = "",
):
    session_row.last_seen_at = datetime.now()
    if lat is not None:
        session_row.last_lat = lat
    if lng is not None:
        session_row.last_lng = lng
    if distance_m is not None:
        session_row.last_distance_m = round(distance_m, 2)
    session_row.last_sync_status = status or None
    session_row.last_sync_message = (message or "").strip()[:255] or None


@bp.route("/login", methods=["POST"])
def mobile_login():
    payload = _json_payload()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    device_id = (payload.get("device_id") or "").strip()
    if not email or not password or not device_id:
        return _api_error("Email, password, and device ID are required.")
    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return _api_error("Invalid login credentials.", 401)
    if not _is_staff_attendance_user(user):
        return _api_error("This account cannot use the attendance app.", 403)
    StaffMobileSession.query.filter_by(user_id=user.id, device_id=device_id, active=True).update(
        {"active": False},
        synchronize_session=False,
    )
    raw_token = secrets.token_urlsafe(48)
    session_row = StaffMobileSession(
        user_id=user.id,
        device_id=device_id,
        device_name=(payload.get("device_name") or "").strip() or None,
        platform=(payload.get("platform") or "android").strip() or "android",
        app_version=(payload.get("app_version") or "").strip() or None,
        token_hash=_token_hash(raw_token),
        active=True,
        last_seen_at=datetime.now(),
    )
    db.session.add(session_row)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "token": raw_token,
            **_bootstrap_payload(user, session_row),
        }
    )


@bp.route("/logout", methods=["POST"])
@mobile_token_required
def mobile_logout():
    g.mobile_session.active = False
    _update_mobile_session_status(g.mobile_session, status="logged_out", message="Logged out from device")
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/bootstrap", methods=["GET"])
@mobile_token_required
def mobile_bootstrap():
    _update_mobile_session_status(g.mobile_session, status="bootstrap", message="Bootstrap refreshed")
    db.session.commit()
    return jsonify({"ok": True, **_bootstrap_payload(g.mobile_user, g.mobile_session)})


@bp.route("/check-in", methods=["POST"])
@mobile_token_required
def mobile_check_in():
    payload = _json_payload()
    try:
        lat = float(payload.get("lat"))
        lng = float(payload.get("lng"))
    except (TypeError, ValueError):
        return _api_error("Valid latitude and longitude are required.")
    active_row = _active_attendance_session_for_user(g.mobile_user.id)
    if active_row and active_row.check_in_at and not active_row.check_out_at:
        _update_mobile_session_status(g.mobile_session, lat=lat, lng=lng, status="already_checked_in", message="Existing active session")
        db.session.commit()
        return jsonify({"ok": True, "message": "Active session already exists.", "active_session": _attendance_row_payload(active_row)})
    settings = _attendance_settings()
    distance_m = _attendance_distance_from_cafe(lat, lng)
    if distance_m is None or distance_m > settings["radius_m"]:
        _update_mobile_session_status(
            g.mobile_session,
            lat=lat,
            lng=lng,
            distance_m=distance_m,
            status="outside_geofence",
            message="Check-in attempted outside geofence",
        )
        db.session.commit()
        return _api_error(
            f"Outside cafe geofence. Current distance: {distance_m:.0f} m." if distance_m is not None else "Outside cafe geofence.",
            403,
        )
    today = date.today()
    completed_today = StaffAttendance.query.filter_by(user_id=g.mobile_user.id, attendance_date=today).first()
    if completed_today and completed_today.check_in_at and completed_today.check_out_at:
        return _api_error("Today's attendance is already completed. Ask admin if a correction is needed.", 409)
    row = completed_today or StaffAttendance(user_id=g.mobile_user.id, attendance_date=today)
    if row.id is None:
        db.session.add(row)
    event_time = _parse_client_datetime(payload.get("captured_at"))
    row.check_in_at = event_time
    row.check_out_at = None
    row.manager_override = False
    row.check_in_lat = lat
    row.check_in_lng = lng
    row.check_in_distance_m = round(distance_m, 2)
    row.check_in_method = "android_geofence_app"
    row.check_out_method = None
    row.check_out_lat = None
    row.check_out_lng = None
    row.check_out_distance_m = None
    row.last_heartbeat_at = event_time
    row.last_heartbeat_lat = lat
    row.last_heartbeat_lng = lng
    row.last_heartbeat_distance_m = round(distance_m, 2)
    row.mobile_device_id = g.mobile_session.device_id
    row.auto_checkout_reason = None
    refresh_attendance_row(row)
    _update_mobile_session_status(
        g.mobile_session,
        lat=lat,
        lng=lng,
        distance_m=distance_m,
        status="checked_in",
        message="Check-in recorded from Android app",
    )
    db.session.commit()
    return jsonify({"ok": True, "message": "Check-in recorded.", "active_session": _attendance_row_payload(row)})


@bp.route("/heartbeat", methods=["POST"])
@mobile_token_required
def mobile_heartbeat():
    payload = _json_payload()
    active_row = _active_attendance_session_for_user(g.mobile_user.id)
    if not active_row:
        _update_mobile_session_status(g.mobile_session, status="idle", message="No active attendance session")
        db.session.commit()
        return jsonify({"ok": True, "message": "No active session.", "active_session": None})
    try:
        lat = float(payload.get("lat"))
        lng = float(payload.get("lng"))
    except (TypeError, ValueError):
        return _api_error("Valid latitude and longitude are required.")
    distance_m = _attendance_distance_from_cafe(lat, lng)
    event_time = _parse_client_datetime(payload.get("captured_at"))
    active_row.last_heartbeat_at = event_time
    active_row.last_heartbeat_lat = lat
    active_row.last_heartbeat_lng = lng
    active_row.last_heartbeat_distance_m = round(distance_m, 2) if distance_m is not None else None
    active_row.mobile_device_id = g.mobile_session.device_id
    _update_mobile_session_status(
        g.mobile_session,
        lat=lat,
        lng=lng,
        distance_m=distance_m,
        status="heartbeat",
        message="Heartbeat synced",
    )
    db.session.commit()
    settings = _attendance_settings()
    return jsonify(
        {
            "ok": True,
            "message": "Heartbeat synced.",
            "inside_geofence": bool(distance_m is not None and distance_m <= settings["radius_m"]),
            "distance_m": round(distance_m, 2) if distance_m is not None else None,
            "active_session": _attendance_row_payload(active_row),
            "policy": _mobile_policy_payload(),
        }
    )


@bp.route("/check-out", methods=["POST"])
@mobile_token_required
def mobile_check_out():
    payload = _json_payload()
    active_row = _active_attendance_session_for_user(g.mobile_user.id)
    if not active_row or not active_row.check_in_at:
        _update_mobile_session_status(g.mobile_session, status="no_active_session", message="Check-out requested without active session")
        db.session.commit()
        return jsonify({"ok": True, "message": "No active session to close.", "active_session": None})
    lat = payload.get("lat")
    lng = payload.get("lng")
    try:
        lat_value = float(lat) if lat is not None else None
        lng_value = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lat_value = None
        lng_value = None
    distance_m = _attendance_distance_from_cafe(lat_value, lng_value) if lat_value is not None and lng_value is not None else None
    checkout_time = _parse_client_datetime(payload.get("captured_at"))
    if checkout_time < active_row.check_in_at:
        checkout_time = active_row.check_in_at
    reason = (payload.get("reason") or "").strip().lower()
    active_row.check_out_at = checkout_time
    active_row.check_out_lat = lat_value
    active_row.check_out_lng = lng_value
    active_row.check_out_distance_m = round(distance_m, 2) if distance_m is not None else None
    active_row.check_out_method = "android_manual_app" if reason in {"manual", "user", "profile"} else "android_auto_app"
    active_row.auto_checkout_reason = reason or None
    active_row.last_heartbeat_at = checkout_time
    if lat_value is not None:
        active_row.last_heartbeat_lat = lat_value
    if lng_value is not None:
        active_row.last_heartbeat_lng = lng_value
    if distance_m is not None:
        active_row.last_heartbeat_distance_m = round(distance_m, 2)
    refresh_attendance_row(active_row)
    _update_mobile_session_status(
        g.mobile_session,
        lat=lat_value,
        lng=lng_value,
        distance_m=distance_m,
        status="checked_out",
        message=f"Check-out synced ({reason or 'manual'})",
    )
    db.session.commit()
    return jsonify({"ok": True, "message": "Check-out recorded.", "active_session": _attendance_row_payload(active_row)})
