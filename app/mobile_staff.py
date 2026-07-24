"""Mobile workspace APIs for staff profile, leave, documents and cafe work.

The Android app uses the same records as the web application.  This module is
deliberately small and task-oriented: it exposes only the actions a logged-in
staff member needs on a phone and keeps admin-only staff management in the
web workspace.
"""

import json
import os
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, redirect, request, send_file, session
from werkzeug.utils import secure_filename

from .auth_helpers import user_has_any_role, user_has_permission
from .cafe import _current_ist_day_bounds, _recalculate_order_totals, create_cafe_order
from .extensions import db
from .leave_logic import calculate_leave_duration, leave_policy, validate_leave_request
from .mobile_attendance import mobile_token_required
from .models import (
    AttendanceRuleBook,
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    LeaveBalance,
    MenuCategory,
    MenuItem,
    StaffAttendance,
    StaffDocument,
    StaffLeaveRequest,
    User,
    Workstation,
)

bp = Blueprint("mobile_staff", __name__, url_prefix="/api/mobile/staff")
IST = ZoneInfo("Asia/Kolkata")
PROTECTED_CATEGORY_NAMES = {"other", "utility"}


def _payload():
    value = request.get_json(silent=True) or {}
    return value if isinstance(value, dict) else {}


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "message": message}), status


def _is_staff(user: User | None) -> bool:
    return bool(user and user.active and not user.email.endswith(".guest@brownberries.local"))


def _date(value, fallback=None):
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _dt(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(IST).isoformat()


def _profile_payload(user):
    profile = user.staff_profile
    return {
        "joining_date": profile.joining_date.isoformat() if profile and profile.joining_date else "",
        "dob": profile.dob.isoformat() if profile and profile.dob else "",
        "gender": profile.gender or "" if profile else "",
        "marital_status": profile.marital_status or "" if profile else "",
        "phone": profile.phone or "" if profile else "",
        "alternate_contact": profile.alternate_contact or "" if profile else "",
        "emergency_contact": profile.emergency_contact or "" if profile else "",
        "address": profile.address or "" if profile else "",
        "shift_start": profile.shift_start_time.strftime("%H:%M") if profile and profile.shift_start_time else "",
        "shift_end": profile.shift_end_time.strftime("%H:%M") if profile and profile.shift_end_time else "",
        "govt_id_type": profile.govt_id_type or "" if profile else "",
        "govt_id_number": profile.govt_id_number or "" if profile else "",
    }


def _attendance_payload(row):
    if not row:
        return None
    return {
        "id": row.id,
        "date": row.attendance_date.isoformat() if row.attendance_date else "",
        "status": row.status or "",
        "check_in_at": _dt(row.check_in_at),
        "check_out_at": _dt(row.check_out_at),
        "check_in_method": row.check_in_method or "",
        "check_out_method": row.check_out_method or "",
        "auto_checkout_reason": row.auto_checkout_reason or "",
        "worked_hours": round(max(0.0, (row.check_out_at - row.check_in_at).total_seconds() / 3600), 2)
        if row.check_in_at and row.check_out_at
        else None,
    }


def _document_payload(doc):
    return {
        "id": doc.id,
        "doc_type": doc.doc_type,
        "doc_number": doc.doc_number or "",
        "status": doc.verification_status or "pending",
        "released": bool(doc.released_by_admin),
        "created_at": _dt(doc.created_at),
        "file_name": os.path.basename(doc.file_path or ""),
        "download_path": f"/api/mobile/staff/documents/{doc.id}/download",
    }


def _leave_payload(row):
    return {
        "id": row.id,
        "leave_type": row.leave_type,
        "start_date": row.start_date.isoformat(),
        "end_date": row.end_date.isoformat(),
        "from_shift": row.from_shift,
        "to_shift": row.to_shift,
        "duration_days": float(row.duration_days or 0),
        "reason": row.reason or "",
        "status": row.status,
        "admin_remarks": row.admin_remarks or "",
        "created_at": _dt(row.created_at),
    }


def _categories_for_item(item):
    ids = []
    try:
        raw = json.loads(item.category_ids_json or "[]")
        if isinstance(raw, list):
            ids = [int(value) for value in raw if str(value).isdigit()]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    names = []
    for category in MenuCategory.query.filter(MenuCategory.id.in_(ids or [item.category_id])).all():
        if category.name not in names:
            names.append(category.name)
    if item.category and item.category.name not in names:
        names.insert(0, item.category.name)
    return names


def _size_options(item):
    if not item.has_size_variants:
        return []
    try:
        raw = json.loads(item.size_pricing_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [
        {"size": str(row.get("size") or "").strip(), "price": float(row.get("price") or 0)}
        for row in raw
        if isinstance(row, dict) and str(row.get("size") or "").strip()
    ]


def _menu_payload(item, include_protected=False):
    category_names = _categories_for_item(item)
    protected = any(name.strip().lower() in PROTECTED_CATEGORY_NAMES for name in category_names)
    if protected and not include_protected:
        return None
    return {
        "id": item.id,
        "name": item.name,
        "price": float(item.price or 0),
        "short_description": item.short_description or "",
        "available": bool(item.available),
        "image_url": item.image_url or "",
        "prep_station": item.prep_station or "",
        "chef_name": item.chef.full_name if item.chef else "",
        "category_names": category_names,
        "sizes": _size_options(item),
    }


def _active_today_orders(table_id):
    start, end = _current_ist_day_bounds()
    return (
        CafeOrder.query.filter(
            CafeOrder.table_id == table_id,
            CafeOrder.created_at >= start,
            CafeOrder.created_at < end,
            CafeOrder.status.notin_(["paid", "cancelled"]),
        )
        .order_by(CafeOrder.created_at.asc())
        .all()
    )


def _table_payload(table):
    orders = _active_today_orders(table.id)
    return {
        "id": table.id,
        "name": table.name,
        "seating_capacity": table.seating_capacity,
        "active_orders": len(orders),
        "pending_amount": round(sum(float(order.total_amount or 0) for order in orders), 2),
        "orders": [
            {
                "id": order.id,
                "order_code": order.display_code or f"#{order.id}",
                "status": order.status,
                "total": round(float(order.total_amount or 0), 2),
                "created_at": _dt(order.created_at),
                "items": [
                    {
                        "name": item.menu_item.name if item.menu_item else "-",
                        "quantity": item.quantity,
                        "size_label": item.size_label or "Standard",
                        "approval_status": item.approval_status or "pending",
                        "prep_status": item.prep_status or "pending",
                    }
                    for item in order.order_items
                ],
            }
            for order in orders
        ],
    }


@bp.route("/workspace", methods=["GET"])
@mobile_token_required
def workspace():
    user = g.mobile_user
    today = datetime.now(IST).date()
    attendance = (
        StaffAttendance.query.filter_by(user_id=user.id)
        .order_by(StaffAttendance.attendance_date.desc())
        .limit(60)
        .all()
    )
    balance = LeaveBalance.query.filter_by(user_id=user.id).first()
    leave_requests = StaffLeaveRequest.query.filter_by(user_id=user.id).order_by(StaffLeaveRequest.created_at.desc()).limit(40).all()
    documents = StaffDocument.query.filter_by(user_id=user.id).order_by(StaffDocument.created_at.desc()).all()
    rulebook = AttendanceRuleBook.query.filter_by(active=True).order_by(AttendanceRuleBook.version.desc()).first()
    tables = CafeTable.query.filter_by(active=True).order_by(CafeTable.name.asc()).all()
    menu_items = MenuItem.query.filter_by(is_deleted=False).order_by(MenuItem.name.asc()).all()
    visible_menu = [payload for item in menu_items if (payload := _menu_payload(item))]
    all_menu = [payload for item in menu_items if (payload := _menu_payload(item, include_protected=True))]
    categories = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    return jsonify({
        "ok": True,
        "server_time_ist": datetime.now(IST).isoformat(),
        "user": {"id": user.id, "full_name": user.full_name, "email": user.email, "roles": user.assigned_roles()},
        "profile": _profile_payload(user),
        "attendance": {"today": _attendance_payload(next((row for row in attendance if row.attendance_date == today), None)), "history": [_attendance_payload(row) for row in attendance]},
        "leave": {
            "balance": {"earned": float(balance.earned_balance if balance else 0), "urgent": float(balance.urgent_balance if balance else 0)},
            "requests": [_leave_payload(row) for row in leave_requests],
            "policy": {"max_continuous_days": leave_policy().max_continuous_days, "max_monthly_urgent_leaves": float(leave_policy().max_monthly_urgent_leaves or 0)},
        },
        "documents": [_document_payload(doc) for doc in documents],
        "rulebook": {"version": rulebook.version, "title": rulebook.title, "content": rulebook.content_text or "", "file_name": rulebook.file_name or ""} if rulebook else None,
        "tables": [_table_payload(table) for table in tables],
        "categories": [{"id": category.id, "name": category.name} for category in categories if category.name.strip().lower() not in PROTECTED_CATEGORY_NAMES],
        "menu": visible_menu,
        "availability_menu": all_menu,
        "capabilities": {"can_manage_orders": True, "can_manage_availability": True},
    })


@bp.route("/web-session", methods=["GET"])
@mobile_token_required
def web_session():
    """Bridge the Android token into the normal Flask web session.

    The Android app uses the mobile token for background attendance.  The
    staff workspace itself is rendered by the existing responsive web app so
    staff get the same live ordering, profile, and availability workflows.
    """
    next_path = (request.args.get("next") or "/cafe/orders").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/cafe/orders"
    session.permanent = True
    session["user_id"] = g.mobile_user.id
    return redirect(next_path)


@bp.route("/profile", methods=["POST"])
@mobile_token_required
def update_profile():
    user = g.mobile_user
    profile = user.staff_profile
    if not profile:
        from .models import StaffProfile
        profile = StaffProfile(user_id=user.id)
        db.session.add(profile)
    payload = _payload()
    for field in ("phone", "alternate_contact", "emergency_contact", "address", "gender", "marital_status", "govt_id_type", "govt_id_number"):
        if field in payload:
            setattr(profile, field, str(payload.get(field) or "").strip() or None)
    if "dob" in payload:
        parsed = _date(payload.get("dob"))
        if payload.get("dob") and not parsed:
            return _error("DOB must use YYYY-MM-DD format.")
        profile.dob = parsed
    db.session.commit()
    return jsonify({"ok": True, "message": "Profile updated.", "profile": _profile_payload(user)})


@bp.route("/leaves", methods=["POST"])
@mobile_token_required
def create_leave():
    payload = _payload()
    start_date = _date(payload.get("start_date"))
    end_date = _date(payload.get("end_date"))
    if not start_date or not end_date:
        return _error("Start and end dates must use YYYY-MM-DD format.")
    leave_type = str(payload.get("leave_type") or "earned").strip().lower()
    from_shift = str(payload.get("from_shift") or "first_half").strip().lower()
    to_shift = str(payload.get("to_shift") or "second_half").strip().lower()
    errors, duration = validate_leave_request(g.mobile_user, leave_type, start_date, end_date, from_shift, to_shift)
    if errors:
        db.session.rollback()
        return _error(" ".join(errors), 422)
    row = StaffLeaveRequest(
        user_id=g.mobile_user.id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        from_shift=from_shift,
        to_shift=to_shift,
        duration_days=duration,
        reason=str(payload.get("reason") or "").strip() or None,
        status="pending",
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "message": "Leave request submitted.", "leave": _leave_payload(row)})


@bp.route("/leaves/<int:leave_id>/cancel", methods=["POST"])
@mobile_token_required
def cancel_leave(leave_id):
    row = StaffLeaveRequest.query.filter_by(id=leave_id, user_id=g.mobile_user.id).first()
    if not row or row.status not in {"pending", "requested_changes"}:
        return _error("Only pending leave requests can be cancelled.", 409)
    row.status = "cancelled"
    row.cancelled_at = datetime.utcnow()
    row.cancelled_by_user_id = g.mobile_user.id
    db.session.commit()
    return jsonify({"ok": True, "message": "Leave request cancelled."})


@bp.route("/documents", methods=["POST"])
@mobile_token_required
def upload_document():
    doc_file = request.files.get("document") or request.files.get("doc_file")
    if not doc_file or not doc_file.filename:
        return _error("Choose a document first.")
    safe_name = secure_filename(doc_file.filename)
    ext = os.path.splitext(safe_name)[1].lower() or ".bin"
    root = current_app.config["UPLOADS_ROOT"]
    folder = os.path.join(root, "staff_docs")
    os.makedirs(folder, exist_ok=True)
    filename = f"mobile-doc-{g.mobile_user.id}-{uuid4().hex[:10]}{ext}"
    full_path = os.path.join(folder, filename)
    doc_file.save(full_path)
    row = StaffDocument(
        user_id=g.mobile_user.id,
        uploaded_by_user_id=g.mobile_user.id,
        doc_type=(request.form.get("doc_type") or "Other").strip() or "Other",
        doc_number=(request.form.get("doc_number") or "").strip() or None,
        file_path=os.path.relpath(full_path, root),
        released_by_admin=False,
        verification_status="pending",
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "message": "Document uploaded for review.", "document": _document_payload(row)})


@bp.route("/documents/<int:doc_id>/download", methods=["GET"])
@mobile_token_required
def download_document(doc_id):
    row = StaffDocument.query.filter_by(id=doc_id, user_id=g.mobile_user.id).first()
    if not row or not row.released_by_admin:
        return _error("This document is not available for download yet.", 403)
    root = os.path.abspath(current_app.config["UPLOADS_ROOT"])
    full_path = os.path.abspath(os.path.join(root, row.file_path))
    if not full_path.startswith(root + os.sep) or not os.path.isfile(full_path):
        return _error("Document file is unavailable.", 404)
    return send_file(full_path, as_attachment=True, download_name=os.path.basename(full_path))


@bp.route("/items/<int:item_id>/availability", methods=["POST"])
@mobile_token_required
def update_availability(item_id):
    item = MenuItem.query.filter_by(id=item_id, is_deleted=False).first()
    if not item:
        return _error("Menu item not found.", 404)
    payload = _payload()
    if "available" not in payload:
        return _error("Availability is required.")
    item.available = bool(payload.get("available"))
    db.session.commit()
    return jsonify({"ok": True, "message": f"{item.name} availability updated.", "item": _menu_payload(item, include_protected=True)})


@bp.route("/orders", methods=["POST"])
@mobile_token_required
def create_staff_order():
    payload = _payload()
    try:
        table_id = int(payload.get("table_id"))
    except (TypeError, ValueError):
        return _error("Select a table.")
    table = CafeTable.query.filter_by(id=table_id, active=True).first()
    if not table:
        return _error("Table not found.", 404)
    requested = payload.get("items")
    if not isinstance(requested, list) or not requested:
        return _error("Add at least one menu item.")
    line_items = []
    for row in requested:
        if not isinstance(row, dict):
            continue
        try:
            item_id = int(row.get("menu_item_id"))
            quantity = max(1, int(row.get("quantity", 1)))
        except (TypeError, ValueError):
            continue
        item = MenuItem.query.filter_by(id=item_id, is_deleted=False).first()
        if not item or not item.available:
            continue
        category_names = _categories_for_item(item)
        if any(name.strip().lower() in PROTECTED_CATEGORY_NAMES for name in category_names):
            continue
        size_label = str(row.get("size_label") or "").strip() or None
        unit_price = float(item.price or 0)
        sizes = _size_options(item)
        if sizes:
            choice = next((option for option in sizes if option["size"].lower() == (size_label or "").lower()), sizes[0])
            size_label = choice["size"]
            unit_price = choice["price"]
        line_items.append((item, quantity, bool(row.get("is_parcel")), size_label, unit_price))
    if not line_items:
        return _error("No available menu items were selected.")
    order = create_cafe_order(
        table_id=table.id,
        ordered_by_user_id=g.mobile_user.id,
        line_items=line_items,
        status="pending_approval",
        payment_type=None,
        payment_reference=None,
    )
    return jsonify({"ok": True, "message": "Order sent for approval.", "order_id": order.id, "order_code": order.display_code, "total": order.total_amount})
