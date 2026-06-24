import json
import calendar
import math
import os
import base64
import ssl
from datetime import date, datetime, timedelta, time
from io import BytesIO
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

import qrcode
from PIL import Image, ImageDraw, ImageFont
from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from openpyxl import Workbook
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .auth_helpers import login_required, roles_required
from .deploy_config import load_deployment_config, save_deployment_config
from .extensions import db, socketio
from .models import (
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    InventoryCategory,
    InventoryItem,
    InventoryDailyClosing,
    InventoryPurchase,
    InventoryPurchaseLine,
    InventoryRecipe,
    InventoryRecipeItem,
    InventoryVendor,
    InventoryWastage,
    MenuCategory,
    MenuItem,
    MenuType,
    StaffAttendance,
    StaffDocument,
    UserType,
    StaffLeaveRequest,
    StaffProfile,
    TableBooking,
    User,
)

bp = Blueprint("cafe", __name__, url_prefix="/cafe")

DEFAULT_ROLE_OPTIONS = (
    "owner",
    "admin",
    "manager",
    "accountant",
    "cashier",
    "staff",
    "server",
    "chef",
    "barista",
    "librarian",
    "inventory_manager",
    "cleaner",
    "delivery_partner",
)
STAFF_ROLES = DEFAULT_ROLE_OPTIONS
PREP_STATION_OPTIONS = ("kitchen", "barista")
IST_TZ = ZoneInfo("Asia/Kolkata")
UTC_TZ = ZoneInfo("UTC")
PROTECTED_MENU_CATEGORY_NAMES = {"other", "utility"}
STAFF_ATTENDANCE_STATUS_OPTIONS = [
    ("present_all_day", "Present All Day"),
    ("first_half", "First Half"),
    ("second_half", "Second Half"),
    ("on_leave", "On Leave"),
    ("sick_leave", "Sick Leave"),
    ("weekly_off", "Weekly Off"),
    ("late_entry", "Late Entry"),
    ("early_exit", "Early Exit"),
    ("missed_checkout", "Missed Checkout"),
    ("absent", "Absent"),
]


def _order_local_date(order: CafeOrder | None) -> date:
    local_dt = _ist_from_utc_naive(order.created_at if order else None)
    return local_dt.date() if local_dt else date.today()


def _current_ist_day_bounds():
    today_ist = datetime.now(IST_TZ).date()
    start_local = datetime.combine(today_ist, time.min).replace(tzinfo=IST_TZ)
    end_local = datetime.combine(today_ist, time.max).replace(tzinfo=IST_TZ)
    return (
        start_local.astimezone(UTC_TZ).replace(tzinfo=None),
        end_local.astimezone(UTC_TZ).replace(tzinfo=None),
    )


def _order_channel_code(order: CafeOrder | None) -> str:
    if order and order.is_delivery:
        return "O"
    return "D"


def _format_internal_order_code(order: CafeOrder | None, seq: int | None = None) -> str:
    if not order:
        return "-"
    local_day = _order_local_date(order)
    running_seq = int(seq if seq is not None else (order.daily_sequence or order.id or 0))
    return f"BB-{_order_channel_code(order)}-{local_day.strftime('%y%m%d')}-{running_seq:04d}"


def _format_pickup_number(order: CafeOrder | None, compact: bool = False) -> str:
    if not order:
        return "-"
    running_seq = int(order.daily_sequence or order.id or 0)
    if compact:
        return f"{running_seq}"
    return f"{_order_channel_code(order)}{running_seq:03d}"


def _normalize_single_order_code(order: CafeOrder, seq: int | None = None):
    if not order:
        return
    running_seq = int(seq if seq is not None else (order.daily_sequence or 0))
    if running_seq < 1:
        running_seq = int(order.id or 0)
    order.daily_sequence = running_seq
    order.display_code = _format_internal_order_code(order, running_seq)


def _backfill_order_codes():
    changed = False
    grouped: dict[date, list[CafeOrder]] = {}
    orders = CafeOrder.query.order_by(CafeOrder.created_at.asc(), CafeOrder.id.asc()).all()
    for order in orders:
        grouped.setdefault(_order_local_date(order), []).append(order)
    for _, day_orders in grouped.items():
        for idx, order in enumerate(day_orders, start=1):
            expected = _format_internal_order_code(order, idx)
            if order.daily_sequence != idx or (order.display_code or "") != expected:
                order.daily_sequence = idx
                order.display_code = expected
                changed = True
    if changed:
        db.session.commit()


def _backfill_paid_timestamps():
    changed = False
    paid_orders = CafeOrder.query.filter(
        CafeOrder.status == "paid",
        CafeOrder.paid_at.is_(None),
    ).all()
    for order in paid_orders:
        order.paid_at = order.created_at
        changed = True
    if changed:
        db.session.commit()


def _staff_attendance_pay_fraction(status: str | None) -> float:
    if status in ["present_all_day", "weekly_off", "late_entry", "early_exit", "on_leave"]:
        return 1.0
    if status in ["first_half", "second_half"]:
        return 0.5
    return 0.0


def _menu_form_state_from_request():
    form = request.form
    selected_category_ids = []
    for value in form.getlist("category_ids"):
        try:
            selected_category_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    selected_category_ids = list(dict.fromkeys(selected_category_ids))
    size_rows = []
    idx = 1
    while True:
        size_name = form.get(f"size_name_{idx}")
        size_price = form.get(f"size_price_{idx}")
        if size_name is None and size_price is None:
            break
        size_rows.append(
            {
                "size": (size_name or "").strip(),
                "price": (size_price or "").strip(),
            }
        )
        idx += 1
    if not size_rows:
        size_rows = [{"size": "", "price": ""}, {"size": "", "price": ""}]
    return {
        "selected_category_ids": selected_category_ids,
        "menu_type_id": form.get("menu_type_id", "").strip(),
        "name": form.get("name", "").strip(),
        "prep_station": (form.get("prep_station") or "kitchen").strip().lower(),
        "image_url": form.get("image_url", "").strip(),
        "short_description": form.get("short_description", "").strip(),
        "description": form.get("description", "").strip(),
        "calories": form.get("calories", "").strip(),
        "price": form.get("price", "").strip(),
        "has_size_variants": True if form.get("has_size_variants") else False,
        "size_rows": size_rows,
        "available": True if form.get("available") else False,
    }


def _default_menu_form_state():
    return {
        "selected_category_ids": [],
        "menu_type_id": "",
        "name": "",
        "prep_station": "kitchen",
        "image_url": "",
        "short_description": "",
        "description": "",
        "calories": "",
        "price": "",
        "has_size_variants": False,
        "size_rows": [{"size": "", "price": ""}, {"size": "", "price": ""}],
        "available": True,
    }


def _render_menu_page(active_menu_section: str = "catalog", add_form_state: dict | None = None):
    items = MenuItem.query.filter(MenuItem.is_deleted.is_(False)).order_by(MenuItem.name).all()
    deleted_items = MenuItem.query.filter(MenuItem.is_deleted.is_(True)).order_by(MenuItem.updated_at.desc(), MenuItem.name.asc()).all()
    all_categories = MenuCategory.query.order_by(MenuCategory.name).all()
    item_category_map = {}
    item_size_map = {}
    for item in items + deleted_items:
        parsed = []
        if item.category_ids_json:
            try:
                raw = json.loads(item.category_ids_json)
                if isinstance(raw, list):
                    parsed = [int(x) for x in raw if str(x).isdigit()]
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
        if not parsed and item.category_id:
            parsed = [item.category_id]
        item_category_map[item.id] = parsed
        sizes = []
        if item.size_pricing_json:
            try:
                raw_sizes = json.loads(item.size_pricing_json)
                if isinstance(raw_sizes, list):
                    for row in raw_sizes:
                        if isinstance(row, dict) and row.get("size") and row.get("price") is not None:
                            sizes.append({"size": str(row["size"]), "price": float(row["price"])})
            except (TypeError, ValueError, json.JSONDecodeError):
                sizes = []
        item_size_map[item.id] = sizes

    selected_category_filter = request.args.get("category_filter", type=int)
    availability_items = _apply_category_filter(
        MenuItem.query.filter(MenuItem.is_deleted.is_(False)).order_by(MenuItem.name.asc()),
        selected_category_filter,
    ).all()
    if active_menu_section not in ["catalog", "add_item", "items", "availability", "deleted_items"]:
        active_menu_section = "catalog"
    return render_template(
        "cafe/menu.html",
        items=items,
        deleted_items=deleted_items,
        categories=all_categories,
        protected_category_ids=[c.id for c in all_categories if _is_protected_menu_category(c)],
        menu_types=MenuType.query.order_by(MenuType.name).all(),
        prep_station_options=PREP_STATION_OPTIONS,
        item_category_map=item_category_map,
        item_size_map=item_size_map,
        active_menu_section=active_menu_section,
        availability_items=availability_items,
        selected_category_filter=selected_category_filter,
        add_form=add_form_state or _default_menu_form_state(),
    )


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _parse_cutoff_time(raw_value: str | None):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _format_cutoff_value(raw_value: str | None) -> str:
    parsed = _parse_cutoff_time(raw_value)
    return parsed.strftime("%H:%M") if parsed else ""


def _order_cutoff_message(channel: str):
    cfg = load_deployment_config(current_app.instance_path)
    key = "QR_ORDER_CUTOFF_TIME" if channel == "qr" else "STAFF_ORDER_CUTOFF_TIME"
    label = "table QR ordering" if channel == "qr" else "staff assisted table ordering"
    parsed = _parse_cutoff_time(cfg.get(key))
    if not parsed:
        return None
    now_ist = datetime.now(IST_TZ).time().replace(second=0, microsecond=0)
    if now_ist <= parsed:
        return None
    return f"{label.title()} is closed for today after {parsed.strftime('%I:%M %p')}."


def _split_multiline(value: str | None) -> list[str]:
    if not value:
        return []
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _load_menu_item_size_variants(menu_item: MenuItem | None) -> list[dict]:
    if not menu_item or not menu_item.has_size_variants:
        return []
    try:
        raw = json.loads(menu_item.size_pricing_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = []
    rows = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("size") or "").strip()
        if not label:
            continue
        rows.append(
            {
                "size": label,
                "price": round(_safe_float(entry.get("price"), 0), 2),
            }
        )
    return rows


def _parse_recipe_size_notes_from_form() -> list[dict]:
    labels = request.form.getlist("size_sop_label[]")
    notes = request.form.getlist("size_sop_note[]")
    rows = []
    for idx, raw_label in enumerate(labels):
        label = (raw_label or "").strip()
        note = (notes[idx] if idx < len(notes) else "").strip()
        if not label:
            continue
        rows.append({"size": label, "note": note})
    return rows


def _recipe_size_note_map(recipe: InventoryRecipe | None) -> dict[str, str]:
    if not recipe or not recipe.size_sop_json:
        return {}
    try:
        rows = json.loads(recipe.size_sop_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        rows = []
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("size") or "").strip()
        note = str(row.get("note") or "").strip()
        if label:
            result[label] = note
    return result


def _serialize_recipe_sop(recipe: InventoryRecipe | None, size_label: str | None = None) -> dict:
    recipe = recipe or None
    size_label = (size_label or "").strip()
    size_note_map = _recipe_size_note_map(recipe)
    ingredients = []
    if recipe:
        for ing in recipe.ingredients:
            if not ing.inventory_item:
                continue
            ingredients.append(
                {
                    "name": ing.inventory_item.name,
                    "qty": round(float(ing.qty_per_menu or 0), 3),
                    "unit": ing.unit or ing.inventory_item.unit or "",
                }
            )
    return {
        "prep_time_minutes": int(recipe.prep_time_minutes or 0) if recipe else 0,
        "yield_qty": round(float(recipe.yield_qty or 0), 3) if recipe else 0,
        "yield_unit": (recipe.yield_unit or "").strip() if recipe else "",
        "ingredients": ingredients,
        "ingredients_note": _split_multiline(recipe.ingredients_note if recipe else None),
        "preparation_steps": _split_multiline(recipe.preparation_steps if recipe else None),
        "plating_notes": _split_multiline(recipe.plating_notes if recipe else None),
        "quality_checks": _split_multiline(recipe.quality_checks if recipe else None),
        "allergy_alerts": _split_multiline(recipe.allergy_alerts if recipe else None),
        "training_notes": _split_multiline(recipe.training_notes if recipe else None),
        "photo_url": (recipe.sop_photo_url or "").strip() if recipe else "",
        "size_label": size_label,
        "size_note": size_note_map.get(size_label, "") if size_label else "",
        "size_notes": [{"size": key, "note": value} for key, value in size_note_map.items()],
    }


def _utc_naive_from_ist(dt_value: datetime) -> datetime:
    localized = dt_value.replace(tzinfo=IST_TZ) if dt_value.tzinfo is None else dt_value.astimezone(IST_TZ)
    return localized.astimezone(UTC_TZ).replace(tzinfo=None)


def _ist_from_utc_naive(dt_value: datetime | None):
    if not dt_value:
        return None
    aware_utc = dt_value.replace(tzinfo=UTC_TZ) if dt_value.tzinfo is None else dt_value.astimezone(UTC_TZ)
    return aware_utc.astimezone(IST_TZ)


def _format_ist(dt_value: datetime | None, fmt: str = "%Y-%m-%d %H:%M"):
    local = _ist_from_utc_naive(dt_value)
    return local.strftime(fmt) if local else "-"


def _emit_ops_notification(message: str, kind: str = "order", payload: dict | None = None):
    data = {"kind": kind, "message": message}
    if payload:
        data.update(payload)
    socketio.emit("ops_notification", data, namespace="/kitchen")
    socketio.emit("ops_notification", data, namespace="/table")


def _is_staff_user(user: User) -> bool:
    return user.role in _get_role_options() and user.email != "qr.guest@brownberries.local"


def _ensure_staff_profile(user: User) -> StaffProfile:
    profile = user.staff_profile
    if not profile:
        profile = StaffProfile(user_id=user.id, archived=not user.active)
        db.session.add(profile)
        db.session.flush()
    return profile


def _is_protected_admin(user: User) -> bool:
    return user.email == "admin@brownberries.local" or (
        user.role == "admin" and user.full_name.strip().lower() == "cafe admin"
    )


def _get_role_options():
    names = {r for r in DEFAULT_ROLE_OPTIONS}
    names.update({(ut.name or "").strip() for ut in UserType.query.all() if (ut.name or "").strip()})
    names.update({(u.role or "").strip() for u in User.query.all() if (u.role or "").strip()})
    return tuple(sorted(names))


def _ensure_role_templates_exist():
    existing = {ut.name.lower(): ut for ut in UserType.query.all() if ut.name}
    changed = False
    for role_name in _get_role_options():
        if role_name.lower() not in existing:
            ut = UserType(name=role_name)
            ut.can_access_cafe = True
            ut.can_manage_orders = True
            ut.can_manage_kitchen = True
            if role_name.lower() == "delivery_partner":
                ut.can_view_delivery_locations = True
            db.session.add(ut)
            changed = True
    if changed:
        db.session.commit()


def _assign_user_type_from_role(user: User):
    if not user.role:
        return
    ut = UserType.query.filter(db.func.lower(UserType.name) == user.role.lower()).first()
    user.user_type_id = ut.id if ut else None


def _save_uploaded_file(file_obj, subdir: str, prefix: str):
    if not file_obj or not file_obj.filename:
        return None
    root = current_app.config["UPLOADS_ROOT"]
    ext = os.path.splitext(secure_filename(file_obj.filename))[1].lower() or ".bin"
    filename = f"{prefix}-{uuid4().hex[:10]}{ext}"
    folder = os.path.join(root, subdir)
    os.makedirs(folder, exist_ok=True)
    full_path = os.path.join(folder, filename)
    file_obj.save(full_path)
    return os.path.relpath(full_path, root)


def _save_menu_image(file_obj):
    if not file_obj or not file_obj.filename:
        return None
    ext = os.path.splitext(secure_filename(file_obj.filename))[1].lower()
    filename = f"menu-{uuid4().hex[:12]}.webp"
    folder = os.path.join(current_app.static_folder, "uploads", "menu")
    os.makedirs(folder, exist_ok=True)
    full_path = os.path.join(folder, filename)
    try:
        img = Image.open(file_obj.stream).convert("RGB")
        img.thumbnail((900, 900))
        img.save(full_path, format="WEBP", quality=78, optimize=True, method=6)
    except Exception:
        return None
    return f"/static/uploads/menu/{filename}"


def _get_or_create_other_category():
    other = MenuCategory.query.filter(db.func.lower(MenuCategory.name) == "other").first()
    if other:
        return other
    other = MenuCategory(name="Other")
    db.session.add(other)
    db.session.flush()
    return other


def _get_or_create_utility_category():
    utility = MenuCategory.query.filter(db.func.lower(MenuCategory.name) == "utility").first()
    if utility:
        return utility
    utility = MenuCategory(name="Utility")
    db.session.add(utility)
    db.session.flush()
    return utility


def _ensure_protected_menu_categories():
    _get_or_create_other_category()
    _get_or_create_utility_category()
    db.session.flush()


def _is_protected_menu_category(category: MenuCategory | None) -> bool:
    return bool(category and (category.name or "").strip().lower() in PROTECTED_MENU_CATEGORY_NAMES)


def _public_menu_category_ids(item: MenuItem, category_name_by_id: dict[int, str]) -> list[int]:
    visible_ids: list[int] = []
    seen: set[int] = set()
    for cid in _menu_item_category_ids(item):
        cname = (category_name_by_id.get(cid) or "").strip().lower()
        if not cname or cname in PROTECTED_MENU_CATEGORY_NAMES or cid in seen:
            continue
        visible_ids.append(cid)
        seen.add(cid)
    return visible_ids


def _is_public_menu_item(item: MenuItem, category_name_by_id: dict[int, str]) -> bool:
    return len(_public_menu_category_ids(item, category_name_by_id)) > 0


def _menu_item_category_ids(item: MenuItem) -> list[int]:
    parsed: list[int] = []
    if item.category_ids_json:
        try:
            raw = json.loads(item.category_ids_json)
            if isinstance(raw, list):
                for value in raw:
                    try:
                        parsed.append(int(value))
                    except (TypeError, ValueError):
                        continue
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if not parsed and item.category_id:
        parsed = [item.category_id]
    return list(dict.fromkeys(parsed))


def _get_item_category_names(item: MenuItem, category_name_by_id: dict[int, str], include_protected: bool = True) -> list[str]:
    names: list[str] = []
    names_seen: set[str] = set()
    for cid in _menu_item_category_ids(item):
        cname = category_name_by_id.get(cid)
        if not include_protected and cname and cname.strip().lower() in PROTECTED_MENU_CATEGORY_NAMES:
            continue
        if cname and cname.lower() not in names_seen:
            names.append(cname)
            names_seen.add(cname.lower())
    return names


def _visible_categories_for_available_menu(include_protected: bool = False) -> list[MenuCategory]:
    all_categories = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    category_name_by_id = {c.id: c.name for c in all_categories}
    categories = list(all_categories)
    if not include_protected:
        categories = [c for c in categories if (c.name or "").strip().lower() not in PROTECTED_MENU_CATEGORY_NAMES]
    available_items = MenuItem.query.filter_by(available=True, is_deleted=False).all()
    used_category_ids: set[int] = set()
    for item in available_items:
        category_ids = (
            _menu_item_category_ids(item)
            if include_protected
            else _public_menu_category_ids(item, category_name_by_id)
        )
        for cid in category_ids:
            used_category_ids.add(cid)
    return [
        c
        for c in categories
        if c.id in used_category_ids
    ]


def _build_staff_id_card(user: User, profile: StaffProfile):
    canvas = Image.new("RGB", (960, 540), "#f6eee5")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.load_default()
    text_font = ImageFont.load_default()
    draw.rectangle((24, 24, 936, 516), outline="#3f2b1d", width=4)
    logo_path = os.path.join(current_app.static_folder, "images", "cafe-logo.png")
    if os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA").resize((140, 140))
        canvas.paste(logo, (50, 42), logo)
    draw.text((220, 58), "Brownberries Cafe Staff ID", fill="#2b1d13", font=title_font)
    draw.text((220, 95), f"Name: {user.full_name}", fill="#2b1d13", font=text_font)
    draw.text((220, 122), f"Role: {user.role.replace('_', ' ').title()}", fill="#2b1d13", font=text_font)
    draw.text((220, 149), f"Email: {user.email}", fill="#2b1d13", font=text_font)
    draw.text((220, 176), f"Phone: {profile.phone or '-'}", fill="#2b1d13", font=text_font)
    draw.text((220, 203), f"DOB: {profile.dob or '-'}", fill="#2b1d13", font=text_font)
    draw.text((220, 230), f"Gender: {profile.gender or '-'}", fill="#2b1d13", font=text_font)
    draw.text((220, 257), f"Govt ID: {(profile.govt_id_type or '-')} / {(profile.govt_id_number or '-')}", fill="#2b1d13", font=text_font)
    draw.text((220, 284), f"Joined: {profile.joining_date or '-'}", fill="#2b1d13", font=text_font)
    photo_box = (50, 220, 180, 380)
    draw.rectangle(photo_box, outline="#7d6654", width=2)
    if profile.photo_file_path:
        photo_path = os.path.join(current_app.config["UPLOADS_ROOT"], profile.photo_file_path)
        if os.path.exists(photo_path):
            photo = Image.open(photo_path).convert("RGB").resize((120, 150))
            canvas.paste(photo, (55, 225))
    out = BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out


def _serialize_order(order: CafeOrder):
    pending_count = sum(1 for item in order.order_items if (item.approval_status or "pending") == "pending")
    try:
        payment_breakdown = json.loads(order.payment_breakdown_json or "[]")
        if not isinstance(payment_breakdown, list):
            payment_breakdown = []
    except (TypeError, ValueError, json.JSONDecodeError):
        payment_breakdown = []
    return {
        "id": order.id,
        "order_code": order.display_code or _format_internal_order_code(order),
        "pickup_no": _format_pickup_number(order),
        "pickup_no_compact": _format_pickup_number(order, compact=True),
        "table_id": order.table_id,
        "table": "For Delivery" if order.is_delivery else (order.table.name if order.table else "-"),
        "status": order.status,
        "payment_type": order.payment_type or "-",
        "payment_breakdown": payment_breakdown,
        "total_amount": round(order.total_amount, 2),
        "is_delivery": bool(order.is_delivery),
        "delivery_customer_name": order.delivery_customer_name,
        "delivery_customer_mobile": order.delivery_customer_mobile,
        "delivery_address": order.delivery_address,
        "delivery_map_url": order.delivery_map_url,
        "packaging_charge": round(order.packaging_charge or 0, 2),
        "delivery_distance_km": round(order.delivery_distance_km or 0, 2),
        "delivery_charge": round(order.delivery_charge or 0, 2),
        "created_at": _format_ist(order.created_at),
        "pending_approval_count": pending_count,
        "items": [
            {
                "id": item.id,
                "name": item.menu_item.name if item.menu_item else "-",
                "qty": item.quantity,
                "size_label": item.size_label,
                "is_parcel": bool(item.is_parcel),
                "prep_station": item.menu_item.prep_station if item.menu_item else "kitchen",
                "approval_status": item.approval_status or "pending",
            }
            for item in order.order_items
        ],
    }


def _ensure_menu_types_seeded():
    existing_types = {t.name.lower(): t for t in MenuType.query.all()}
    item_types = (
        db.session.query(MenuItem.item_type)
        .filter(MenuItem.item_type.isnot(None), MenuItem.item_type != "")
        .distinct()
        .all()
    )
    created = False
    for row in item_types:
        name = row[0].strip()
        if name and name.lower() not in existing_types:
            db.session.add(MenuType(name=name))
            created = True
    if created:
        db.session.commit()


def _parse_category_ids_from_form():
    raw_ids = request.form.getlist("category_ids")
    parsed = []
    for value in raw_ids:
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid not in parsed:
            parsed.append(cid)
    return parsed


def _parse_size_pricing_from_form():
    pairs = []
    indexes = []
    for key in request.form.keys():
        if key.startswith("size_name_"):
            suffix = key.removeprefix("size_name_")
            if suffix.isdigit():
                indexes.append(int(suffix))
    if not indexes:
        indexes = [1, 2]
    for i in sorted(set(indexes)):
        name = (request.form.get(f"size_name_{i}") or "").strip()
        price_raw = (request.form.get(f"size_price_{i}") or "").strip()
        if not name:
            continue
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        if price < 0:
            continue
        pairs.append({"size": name, "price": round(price, 2)})
    return pairs


def _apply_category_filter(query, category_id: int | None):
    if not category_id:
        return query
    return query.filter(
        db.or_(
            MenuItem.category_id == category_id,
            MenuItem.category_ids_json.ilike(f"%{category_id}%"),
        )
    )


def _get_qr_guest_user_id() -> int:
    guest_email = "qr.guest@brownberries.local"
    guest = User.query.filter_by(email=guest_email).first()
    if guest:
        return guest.id
    guest = User(
        full_name="QR Guest",
        email=guest_email,
        password_hash=generate_password_hash("qr-guest"),
        role="staff",
        active=True,
    )
    db.session.add(guest)
    db.session.commit()
    return guest.id


def _parse_line_items_from_request():
    cart_payload = (request.form.get("cart_payload") or "").strip()
    cart_entries = []
    if cart_payload:
        try:
            parsed = json.loads(cart_payload)
            if isinstance(parsed, list):
                for row in parsed:
                    if not isinstance(row, dict):
                        continue
                    try:
                        menu_item_id = int(row.get("menu_item_id"))
                        quantity = int(row.get("quantity"))
                        is_parcel = True if row.get("is_parcel") else False
                        size_label = (row.get("size_label") or "").strip() or None
                        unit_price = float(row.get("unit_price")) if row.get("unit_price") is not None else None
                    except (TypeError, ValueError):
                        continue
                    if quantity > 0:
                        cart_entries.append((menu_item_id, quantity, is_parcel, size_label, unit_price))
        except (json.JSONDecodeError, TypeError):
            pass

    if not cart_entries:
        for value in request.form.getlist("menu_item_id"):
            try:
                menu_item_id = int(value)
                quantity = int(request.form.get(f"qty_{menu_item_id}", 1))
                if quantity > 0:
                    cart_entries.append((menu_item_id, quantity, False, None, None))
            except (TypeError, ValueError):
                continue

    if not cart_entries:
        return []

    merged = {}
    for menu_item_id, quantity, is_parcel, size_label, unit_price in cart_entries:
        key = (menu_item_id, bool(is_parcel), size_label or "", unit_price if unit_price is not None else -1.0)
        merged[key] = merged.get(key, 0) + quantity

    item_ids = list({key[0] for key in merged.keys()})
    items = MenuItem.query.filter(
        MenuItem.id.in_(item_ids),
        MenuItem.available.is_(True),
        MenuItem.is_deleted.is_(False),
    ).all()
    item_by_id = {item.id: item for item in items}
    line_items = []
    for (menu_item_id, is_parcel, size_label, unit_price_key), quantity in merged.items():
        menu_item = item_by_id.get(menu_item_id)
        if menu_item:
            line_items.append((menu_item, quantity, is_parcel, size_label or None, None if unit_price_key == -1.0 else unit_price_key))
    return line_items


def _haversine_km(lat1, lng1, lat2, lng2):
    radius_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


def _create_order(table_id: int, ordered_by_user_id: int, status: str, payment_type, payment_reference):
    line_items = _parse_line_items_from_request()
    if not line_items:
        return None
    return create_cafe_order(
        table_id=table_id,
        ordered_by_user_id=ordered_by_user_id,
        line_items=line_items,
        status=status,
        payment_type=payment_type,
        payment_reference=payment_reference,
    )


def _recalculate_order_totals(order: CafeOrder):
    line_total = 0.0
    packaging_charge = 0.0
    order_items = (
        CafeOrderItem.query.filter_by(order_id=order.id)
        .order_by(CafeOrderItem.id.asc())
        .all()
    )
    for oi in order_items:
        if (oi.approval_status or "pending") == "rejected":
            continue
        line_total += float(oi.unit_price or 0) * int(oi.quantity or 0)
        if order.is_delivery:
            packaging_charge += 20.0 * int(oi.quantity or 0)
        elif oi.is_parcel:
            packaging_charge += 20.0 * int(oi.quantity or 0)
    order.packaging_charge = round(packaging_charge, 2)
    order.total_amount = round(line_total + order.packaging_charge + float(order.delivery_charge or 0), 2)


def _apply_recipe_inventory_deduction(order: CafeOrder):
    if not order or not order.order_items:
        return
    item_qty_map: dict[int, int] = {}
    for oi in order.order_items:
        item_qty_map[oi.menu_item_id] = item_qty_map.get(oi.menu_item_id, 0) + int(oi.quantity or 0)
    if not item_qty_map:
        return
    recipes = InventoryRecipe.query.filter(
        InventoryRecipe.menu_item_id.in_(list(item_qty_map.keys())),
        InventoryRecipe.active.is_(True),
    ).all()
    recipe_by_menu = {r.menu_item_id: r for r in recipes}
    for menu_item_id, qty in item_qty_map.items():
        recipe = recipe_by_menu.get(menu_item_id)
        if not recipe:
            continue
        for ing in recipe.ingredients:
            inv_item = ing.inventory_item
            if not inv_item:
                continue
            deduct_qty = float(ing.qty_per_menu or 0) * float(qty)
            inv_item.current_amount = round(max(0.0, float(inv_item.current_amount or 0) - deduct_qty), 3)


def _receipt_link_for_order(order: CafeOrder) -> str:
    base = (current_app.config.get("PUBLIC_BASE_URL") or request.host_url.rstrip("/")).rstrip("/")
    return f"{base}/cafe/receipt/{order.id}"


def _parse_split_payment_rows():
    rows = []
    for idx in range(1, 5):
        payment_type = (request.form.get(f"payment_type_{idx}") or "").strip()
        amount = _safe_float(request.form.get(f"payment_amount_{idx}"), 0)
        reference = (request.form.get(f"payment_reference_{idx}") or "").strip()
        if not payment_type and amount <= 0 and not reference:
            continue
        if not payment_type or amount <= 0:
            return None, "Each payment row must have a method and an amount."
        rows.append(
            {
                "method": payment_type,
                "amount": round(float(amount), 2),
                "reference": reference,
            }
        )
    if not rows:
        return None, "Add at least one payment entry."
    return rows, ""


def _send_receipt_sms(country_code: str, mobile: str, message: str):
    cfg = load_deployment_config(current_app.instance_path)
    enabled = str(cfg.get("SMS_ENABLED", "0")).strip() in ["1", "true", "True"]
    provider = (cfg.get("SMS_PROVIDER") or "twilio").strip().lower()
    if not enabled:
        return False, "SMS gateway is disabled."
    if provider != "twilio":
        return False, "Unsupported SMS provider configured."
    sid = (cfg.get("TWILIO_ACCOUNT_SID") or "").strip()
    token = (cfg.get("TWILIO_AUTH_TOKEN") or "").strip()
    from_number = (cfg.get("SMS_FROM") or cfg.get("TWILIO_FROM_NUMBER") or "").strip()
    if not sid or not token or not from_number:
        return False, "SMS provider credentials are incomplete."
    to_number = f"{country_code}{mobile}"
    payload = urlencode({"To": to_number, "From": from_number, "Body": message}).encode("utf-8")
    auth_raw = f"{sid}:{token}".encode("utf-8")
    auth_b64 = base64.b64encode(auth_raw).decode("utf-8")
    req = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {auth_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    ca_bundle = (cfg.get("SMS_CA_BUNDLE") or "").strip()
    allow_insecure_ssl = str(cfg.get("SMS_ALLOW_INSECURE_SSL", "0")).strip() in ["1", "true", "True"]
    context = None
    try:
        if allow_insecure_ssl:
            context = ssl._create_unverified_context()
        elif ca_bundle and os.path.exists(ca_bundle):
            context = ssl.create_default_context(cafile=ca_bundle)
        else:
            import certifi  # type: ignore

            context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = ssl.create_default_context()
    try:
        with urlopen(req, timeout=12, context=context) as resp:
            if 200 <= resp.status < 300:
                return True, "SMS sent."
            return False, f"SMS gateway returned status {resp.status}."
    except Exception as exc:
        return False, f"SMS sending failed: {exc}"


def create_cafe_order(
    table_id: int,
    ordered_by_user_id: int,
    line_items,
    status: str = "open",
    payment_type=None,
    payment_reference=None,
    is_delivery: bool = False,
    delivery_customer_name: str | None = None,
    delivery_customer_mobile: str | None = None,
    delivery_address: str | None = None,
    delivery_lat: float | None = None,
    delivery_lng: float | None = None,
    delivery_map_url: str | None = None,
):
    if not line_items:
        return None
    today = datetime.now(IST_TZ).date()
    day_start_utc = _utc_naive_from_ist(datetime.combine(today, time.min))
    day_end_utc = _utc_naive_from_ist(datetime.combine(today + timedelta(days=1), time.min))
    today_count = CafeOrder.query.filter(
        CafeOrder.created_at >= day_start_utc,
        CafeOrder.created_at < day_end_utc,
    ).count()
    daily_seq = today_count + 1

    order = CafeOrder(
        table_id=table_id,
        ordered_by_user_id=ordered_by_user_id,
        status=status,
        payment_type=payment_type,
        payment_reference=payment_reference,
        is_delivery=is_delivery,
        delivery_customer_name=delivery_customer_name,
        delivery_customer_mobile=delivery_customer_mobile,
        delivery_address=delivery_address,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        delivery_map_url=delivery_map_url,
        daily_sequence=daily_seq,
        display_code="",
    )
    db.session.add(order)
    db.session.flush()
    _normalize_single_order_code(order, daily_seq)

    for row in line_items:
        if len(row) == 5:
            menu_item, qty, is_parcel, size_label, unit_price = row
        elif len(row) == 3:
            menu_item, qty, is_parcel = row
            size_label = None
            unit_price = None
        else:
            menu_item, qty = row
            is_parcel = False
            size_label = None
            unit_price = None
        price_to_use = float(unit_price) if unit_price is not None else float(menu_item.price)
        db.session.add(
            CafeOrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                quantity=qty,
                unit_price=price_to_use,
                size_label=size_label,
                is_parcel=bool(is_parcel),
                approval_status="pending" if status == "pending_approval" else "approved",
            )
        )

    delivery_distance_km = 0.0
    delivery_charge = 0.0
    if is_delivery and delivery_lat is not None and delivery_lng is not None:
        cafe_lat = 25.207989477704068
        cafe_lng = 80.87374457551877
        direct_km = _haversine_km(cafe_lat, cafe_lng, float(delivery_lat), float(delivery_lng))
        delivery_distance_km = round(direct_km * 1.3, 2)
        delivery_charge = round(delivery_distance_km * 5.0, 2)

    order.delivery_distance_km = delivery_distance_km
    order.delivery_charge = delivery_charge
    _recalculate_order_totals(order)
    _apply_recipe_inventory_deduction(order)
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_created", payload, namespace="/kitchen")
    socketio.emit("order_created", payload, namespace="/table")
    return order


@bp.route("/")
@login_required
def home():
    cfg = load_deployment_config(current_app.instance_path)
    today_start, today_end = _current_ist_day_bounds()
    sms_enabled = str(cfg.get("SMS_ENABLED", "0")).strip() in ["1", "true", "True"]
    sms_provider = (cfg.get("SMS_PROVIDER") or "twilio").strip().lower() or "twilio"
    sms_from = (cfg.get("SMS_FROM") or cfg.get("TWILIO_FROM_NUMBER") or "").strip()
    twilio_sid = (cfg.get("TWILIO_ACCOUNT_SID") or "").strip()
    twilio_auth_token = (cfg.get("TWILIO_AUTH_TOKEN") or "").strip()
    sid_hint = f"{twilio_sid[:4]}...{twilio_sid[-4:]}" if len(twilio_sid) >= 10 else twilio_sid
    token_hint = f"{twilio_auth_token[:3]}...{twilio_auth_token[-3:]}" if len(twilio_auth_token) >= 8 else ("Set" if twilio_auth_token else "")
    sms_ca_bundle = (cfg.get("SMS_CA_BUNDLE") or "").strip()
    sms_allow_insecure_ssl = str(cfg.get("SMS_ALLOW_INSECURE_SSL", "0")).strip() in ["1", "true", "True"]
    qr_order_cutoff_time = _format_cutoff_value(cfg.get("QR_ORDER_CUTOFF_TIME"))
    staff_order_cutoff_time = _format_cutoff_value(cfg.get("STAFF_ORDER_CUTOFF_TIME"))
    return render_template(
        "cafe/home.html",
        tables=CafeTable.query.count(),
        open_orders=CafeOrder.query.filter(
            CafeOrder.status.notin_(["paid", "cancelled"]),
            CafeOrder.created_at >= today_start,
            CafeOrder.created_at <= today_end,
        ).count(),
        low_stock=InventoryItem.query.filter(
            InventoryItem.current_amount <= InventoryItem.reorder_level
        ).count(),
        delivery_open_orders=CafeOrder.query.filter_by(is_delivery=True).filter(
            CafeOrder.status.notin_(["paid", "cancelled"]),
            CafeOrder.created_at >= today_start,
            CafeOrder.created_at <= today_end,
        ).count(),
        upcoming_bookings=TableBooking.query.filter(
            TableBooking.booking_date >= date.today(),
            TableBooking.status == "booked",
        ).count(),
        public_notice_text=(cfg.get("PUBLIC_NOTICE_TEXT", "") or "").strip(),
        public_notice_enabled=(cfg.get("PUBLIC_NOTICE_ENABLED", "0") in [1, "1", True, "true", "True"]),
        sms_enabled=sms_enabled,
        sms_provider=sms_provider,
        sms_from=sms_from,
        twilio_sid_hint=sid_hint,
        twilio_token_hint=token_hint,
        sms_ca_bundle=sms_ca_bundle,
        sms_allow_insecure_ssl=sms_allow_insecure_ssl,
        qr_order_cutoff_time=qr_order_cutoff_time,
        staff_order_cutoff_time=staff_order_cutoff_time,
    )


@bp.route("/public-notice", methods=["POST"])
@roles_required("admin", "manager")
def update_public_notice():
    text = (request.form.get("notice_text") or "").strip()
    enabled = True if request.form.get("notice_enabled") else False
    save_deployment_config(
        current_app.instance_path,
        {
            "PUBLIC_NOTICE_TEXT": text,
            "PUBLIC_NOTICE_ENABLED": "1" if (enabled and text) else "0",
        },
    )
    flash("Public notification updated.", "success")
    return redirect(url_for("cafe.home"))


@bp.route("/sms-settings", methods=["POST"])
@roles_required("admin", "manager")
def update_sms_settings():
    cfg = load_deployment_config(current_app.instance_path)
    sms_enabled = True if request.form.get("sms_enabled") else False
    provider = (request.form.get("sms_provider") or "twilio").strip().lower()
    if provider not in ["twilio"]:
        provider = "twilio"
    sid = (request.form.get("twilio_account_sid") or "").strip()
    token = (request.form.get("twilio_auth_token") or "").strip()
    from_number = (request.form.get("sms_from") or "").strip()
    ca_bundle = (request.form.get("sms_ca_bundle") or "").strip()
    allow_insecure_ssl = True if request.form.get("sms_allow_insecure_ssl") else False

    updates = {
        "SMS_ENABLED": "1" if sms_enabled else "0",
        "SMS_PROVIDER": provider,
        "TWILIO_ACCOUNT_SID": sid or (cfg.get("TWILIO_ACCOUNT_SID") or ""),
        "TWILIO_AUTH_TOKEN": token or (cfg.get("TWILIO_AUTH_TOKEN") or ""),
        "SMS_FROM": from_number or (cfg.get("SMS_FROM") or cfg.get("TWILIO_FROM_NUMBER") or ""),
        "SMS_CA_BUNDLE": ca_bundle or (cfg.get("SMS_CA_BUNDLE") or ""),
        "SMS_ALLOW_INSECURE_SSL": "1" if allow_insecure_ssl else "0",
    }
    save_deployment_config(current_app.instance_path, updates)
    flash("SMS gateway settings saved.", "success")
    return redirect(url_for("cafe.home"))


@bp.route("/order-cutoff-settings", methods=["POST"])
@roles_required("admin", "manager")
def update_order_cutoff_settings():
    qr_cutoff = _format_cutoff_value(request.form.get("qr_order_cutoff_time"))
    staff_cutoff = _format_cutoff_value(request.form.get("staff_order_cutoff_time"))
    save_deployment_config(
        current_app.instance_path,
        {
            "QR_ORDER_CUTOFF_TIME": qr_cutoff,
            "STAFF_ORDER_CUTOFF_TIME": staff_cutoff,
        },
    )
    flash("Order cutoff settings saved.", "success")
    return redirect(url_for("cafe.home"))


@bp.route("/sms-settings/test", methods=["POST"])
@roles_required("admin", "manager")
def test_sms_settings():
    cc = (request.form.get("test_country_code") or "+91").strip()
    mobile = "".join(ch for ch in (request.form.get("test_mobile") or "") if ch.isdigit())
    message = (request.form.get("test_message") or "").strip() or "Brownberries Cafe SMS test message."
    if not mobile:
        flash("Please enter a valid test mobile number.", "error")
        return redirect(url_for("cafe.home"))
    ok, msg = _send_receipt_sms(cc, mobile, message)
    if ok:
        flash("Test SMS sent successfully.", "success")
    else:
        flash(f"Test SMS failed: {msg}", "error")
    return redirect(url_for("cafe.home"))


@bp.route("/bookings", methods=["GET", "POST"])
@roles_required("admin", "manager", "cashier")
def bookings():
    if request.method == "POST":
        booking = TableBooking.query.get_or_404(int(request.form["booking_id"]))
        new_status = (request.form.get("status") or "booked").strip().lower()
        if new_status not in ["booked", "confirmed", "arrived", "completed", "cancelled", "no_show"]:
            new_status = "booked"
        booking.status = new_status
        db.session.commit()
        flash("Booking status updated.", "success")
        return redirect(url_for("cafe.bookings", status=request.args.get("status", "")))

    status = (request.args.get("status") or "").strip().lower()
    bookings_query = TableBooking.query
    if status:
        bookings_query = bookings_query.filter(TableBooking.status == status)
    bookings_list = (
        bookings_query.order_by(TableBooking.booking_date.asc(), TableBooking.start_hour.asc(), TableBooking.created_at.asc())
        .all()
    )
    return render_template("cafe/bookings.html", bookings=bookings_list, selected_status=status)


@bp.route("/deliveries")
@roles_required("admin", "manager", "delivery_partner")
def delivery_locations():
    cafe_lat = 25.207989477704068
    cafe_lng = 80.87374457551877
    orders = (
        CafeOrder.query.options(joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item))
        .filter(CafeOrder.is_delivery.is_(True), CafeOrder.status.notin_(["paid", "cancelled"]))
        .order_by(CafeOrder.created_at.asc())
        .all()
    )
    return render_template(
        "cafe/deliveries.html",
        orders=orders,
        cafe_lat=cafe_lat,
        cafe_lng=cafe_lng,
    )


@bp.route("/tables", methods=["GET", "POST"])
@roles_required("admin", "manager")
def tables():
    if request.method == "POST":
        table = CafeTable(
            name=request.form["name"].strip(),
            seating_capacity=int(request.form["seating_capacity"]),
            metadata_note=request.form.get("metadata_note", "").strip() or None,
            qr_slug=f"{request.form['name'].strip().lower().replace(' ', '-')}-{uuid4().hex[:6]}",
            active=True if request.form.get("active") else False,
        )
        db.session.add(table)
        db.session.commit()
        flash("Table added.", "success")
        return redirect(url_for("cafe.tables"))
    return render_template("cafe/tables.html", tables=CafeTable.query.order_by(CafeTable.name).all())


@bp.route("/tables/<int:table_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_table(table_id):
    table = CafeTable.query.get_or_404(table_id)
    db.session.delete(table)
    db.session.commit()
    flash("Table deleted.", "success")
    return redirect(url_for("cafe.tables"))


@bp.route("/tables/<int:table_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def update_table(table_id):
    table = CafeTable.query.get_or_404(table_id)
    table.name = request.form["name"].strip()
    table.seating_capacity = int(request.form["seating_capacity"])
    table.metadata_note = request.form.get("metadata_note", "").strip() or None
    table.active = True if request.form.get("active") else False
    db.session.commit()
    flash("Table updated.", "success")
    return redirect(url_for("cafe.tables"))


@bp.route("/tables/<int:table_id>/qr")
@roles_required("admin", "manager")
def table_qr_download(table_id):
    table = CafeTable.query.get_or_404(table_id)
    base_url = (
        request.args.get("base_url")
        or current_app.config.get("PUBLIC_BASE_URL")
        or request.host_url.rstrip("/")
    ).strip()
    qr_url = f"{base_url}/table?slug={table.qr_slug}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=20,
        border=4,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    safe_table_name = table.name.lower().replace(" ", "-")
    filename = f"{safe_table_name}-qr.png"
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/png",
        },
    )


@bp.route("/menu", methods=["GET", "POST"])
@roles_required("admin", "manager")
def menu():
    _ensure_protected_menu_categories()
    _ensure_menu_types_seeded()
    stale_count = MenuItem.query.filter(MenuItem.subcategory_id.isnot(None)).count()
    if stale_count:
        MenuItem.query.filter(MenuItem.subcategory_id.isnot(None)).update(
            {MenuItem.subcategory_id: None}, synchronize_session=False
        )
        db.session.commit()
    if request.method == "POST":
        form_state = _menu_form_state_from_request()
        menu_type_id_raw = request.form.get("menu_type_id", "").strip()
        if not menu_type_id_raw.isdigit():
            flash("Please select a valid item type.", "error")
            return _render_menu_page("add_item", form_state)
        menu_type = MenuType.query.get(int(menu_type_id_raw))
        if not menu_type:
            flash("Please select a valid item type.", "error")
            return _render_menu_page("add_item", form_state)
        category_ids = _parse_category_ids_from_form()
        if not category_ids:
            flash("Please select at least one category.", "error")
            return _render_menu_page("add_item", form_state)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Please enter the item name.", "error")
            return _render_menu_page("add_item", form_state)
        price_raw = request.form.get("price", "").strip()
        if not price_raw:
            flash("Please enter the item price.", "error")
            return _render_menu_page("add_item", form_state)
        try:
            price = float(price_raw)
        except ValueError:
            flash("Please enter a valid item price.", "error")
            return _render_menu_page("add_item", form_state)
        uploaded_image = _save_menu_image(request.files.get("image_file"))
        image_url = uploaded_image or (request.form.get("image_url", "").strip() or None)
        item = MenuItem(
            category_id=category_ids[0],
            subcategory_id=None,
            item_type=menu_type.name,
            category_ids_json=json.dumps(category_ids),
            name=name,
            image_url=image_url,
            short_description=request.form.get("short_description", "").strip() or None,
            description=request.form.get("description", "").strip() or None,
            calories=int(request.form["calories"]) if request.form.get("calories") else None,
            price=price,
            has_size_variants=True if request.form.get("has_size_variants") else False,
            size_pricing_json=None,
            prep_station=request.form.get("prep_station", "kitchen"),
            available=True if request.form.get("available") else False,
            is_deleted=False,
        )
        if item.has_size_variants:
            size_pairs = _parse_size_pricing_from_form()
            if not size_pairs:
                flash("Please add at least one serving size with price.", "error")
                return _render_menu_page("add_item", form_state)
            item.size_pricing_json = json.dumps(size_pairs) if size_pairs else None
        db.session.add(item)
        db.session.commit()
        flash("Menu item added.", "success")
        return redirect(url_for("cafe.menu", section="items"))
    active_menu_section = (request.args.get("section") or "catalog").strip().lower()
    return _render_menu_page(active_menu_section)


@bp.route("/menu/availability", methods=["POST"])
@login_required
def update_menu_availability():
    category_filter = request.form.get("category_filter", "").strip()
    category_filter_id = int(category_filter) if category_filter.isdigit() else None
    scoped_items = _apply_category_filter(MenuItem.query.filter(MenuItem.is_deleted.is_(False)), category_filter_id).all()
    selected_ids = {
        int(x) for x in request.form.getlist("available_item_ids") if str(x).isdigit()
    }
    for item in scoped_items:
        item.available = item.id in selected_ids
    db.session.commit()
    flash("Menu item availability updated.", "success")
    next_url = request.form.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("cafe.items_availability", category_filter=category_filter or ""))


@bp.route("/items-availability")
@login_required
def items_availability():
    selected_category_filter = request.args.get("category_filter", type=int)
    categories = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    availability_items = _apply_category_filter(
        MenuItem.query.filter(MenuItem.is_deleted.is_(False)).order_by(MenuItem.name.asc()),
        selected_category_filter,
    ).all()
    return render_template(
        "cafe/items_availability.html",
        categories=categories,
        availability_items=availability_items,
        selected_category_filter=selected_category_filter,
    )


@bp.route("/menu/categories", methods=["POST"])
@roles_required("admin", "manager")
def add_category():
    name = request.form["name"].strip()
    if not name:
        flash("Category name is required.", "error")
        return redirect(url_for("cafe.menu"))
    if name.lower() in PROTECTED_MENU_CATEGORY_NAMES:
        flash("This category already exists as a protected system category.", "error")
        return redirect(url_for("cafe.menu"))
    existing = MenuCategory.query.filter(db.func.lower(MenuCategory.name) == name.lower()).first()
    if existing:
        flash("Category already exists.", "error")
        return redirect(url_for("cafe.menu"))
    db.session.add(MenuCategory(name=name))
    db.session.commit()
    flash("Category added.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/categories/<int:category_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def update_category(category_id):
    category = MenuCategory.query.get_or_404(category_id)
    if _is_protected_menu_category(category):
        flash("Protected categories cannot be renamed.", "error")
        return redirect(url_for("cafe.menu"))
    name = request.form["name"].strip()
    if not name:
        flash("Category name is required.", "error")
        return redirect(url_for("cafe.menu"))
    if name.lower() in PROTECTED_MENU_CATEGORY_NAMES:
        flash("This category name is reserved for a protected system category.", "error")
        return redirect(url_for("cafe.menu"))
    duplicate = MenuCategory.query.filter(
        db.func.lower(MenuCategory.name) == name.lower(), MenuCategory.id != category.id
    ).first()
    if duplicate:
        flash("Category with same name already exists.", "error")
        return redirect(url_for("cafe.menu"))
    category.name = name
    db.session.commit()
    flash("Category updated.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/categories/<int:category_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_category(category_id):
    category = MenuCategory.query.get_or_404(category_id)
    other = _get_or_create_other_category()
    if _is_protected_menu_category(category) or category.id == other.id:
        flash("Protected categories cannot be deleted.", "error")
        return redirect(url_for("cafe.menu"))
    linked_items = MenuItem.query.filter(
        db.or_(
            MenuItem.category_id == category.id,
            MenuItem.category_ids_json.ilike(f"%{category.id}%"),
        )
    ).all()
    for item in linked_items:
        cat_ids = []
        if item.category_ids_json:
            try:
                raw = json.loads(item.category_ids_json)
                if isinstance(raw, list):
                    cat_ids = [int(x) for x in raw if str(x).isdigit()]
            except (TypeError, ValueError, json.JSONDecodeError):
                cat_ids = []
        if not cat_ids and item.category_id:
            cat_ids = [item.category_id]
        cat_ids = [cid for cid in cat_ids if cid != category.id]
        if not cat_ids:
            cat_ids = [other.id]
        item.category_id = cat_ids[0]
        item.category_ids_json = json.dumps(cat_ids)
    if category.subcategories:
        for sub in category.subcategories:
            db.session.delete(sub)
    db.session.delete(category)
    db.session.commit()
    flash("Category deleted. Linked items moved to Other.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/types", methods=["POST"])
@roles_required("admin", "manager")
def add_type():
    name = request.form["name"].strip()
    if not name:
        flash("Type name is required.", "error")
        return redirect(url_for("cafe.menu"))
    duplicate = MenuType.query.filter(db.func.lower(MenuType.name) == name.lower()).first()
    if duplicate:
        flash("Type already exists.", "error")
        return redirect(url_for("cafe.menu"))
    db.session.add(MenuType(name=name))
    db.session.commit()
    flash("Type added.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/types/<int:type_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def update_type(type_id):
    menu_type = MenuType.query.get_or_404(type_id)
    old_name = menu_type.name
    new_name = request.form["name"].strip()
    if not new_name:
        flash("Type name is required.", "error")
        return redirect(url_for("cafe.menu"))
    duplicate = MenuType.query.filter(
        db.func.lower(MenuType.name) == new_name.lower(), MenuType.id != menu_type.id
    ).first()
    if duplicate:
        flash("Type already exists.", "error")
        return redirect(url_for("cafe.menu"))
    menu_type.name = new_name
    MenuItem.query.filter_by(item_type=old_name).update({"item_type": new_name})
    db.session.commit()
    flash("Type updated.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/types/<int:type_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_type(type_id):
    menu_type = MenuType.query.get_or_404(type_id)
    if MenuItem.query.filter_by(item_type=menu_type.name).first():
        flash("Type cannot be deleted while linked menu items exist.", "error")
        return redirect(url_for("cafe.menu"))
    db.session.delete(menu_type)
    db.session.commit()
    flash("Type deleted.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/<int:item_id>/availability", methods=["POST"])
@roles_required("admin", "manager")
def toggle_item(item_id):
    item = MenuItem.query.get_or_404(item_id)
    item.available = not item.available
    db.session.commit()
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/items/<int:item_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def update_menu_item(item_id):
    item = MenuItem.query.get_or_404(item_id)
    menu_type_id = request.form.get("menu_type_id", "").strip()
    if menu_type_id:
        menu_type = MenuType.query.get(int(menu_type_id))
        if not menu_type:
            flash("Please select a valid type.", "error")
            return redirect(url_for("cafe.menu", section="items"))
        item.item_type = menu_type.name

    category_ids = _parse_category_ids_from_form()
    if category_ids:
        item.category_id = category_ids[0]
        item.category_ids_json = json.dumps(category_ids)
    item.subcategory_id = None
    item_name = request.form.get("name", "").strip()
    if item_name:
        item.name = item_name

    uploaded_image = _save_menu_image(request.files.get("image_file"))
    if uploaded_image:
        item.image_url = uploaded_image
    else:
        image_url = request.form.get("image_url", "").strip()
        if image_url:
            item.image_url = image_url

    if "description" in request.form:
        description = request.form.get("description", "").strip()
        if description:
            item.description = description
    if "short_description" in request.form:
        short_description = request.form.get("short_description", "").strip()
        if short_description:
            item.short_description = short_description

    calories_raw = request.form.get("calories", "").strip()
    if calories_raw:
        item.calories = int(calories_raw)

    price_raw = request.form.get("price", "").strip()
    if price_raw:
        item.price = float(price_raw)

    has_size_variants = True if request.form.get("has_size_variants") else False
    if has_size_variants:
        item.has_size_variants = True
        size_pairs = _parse_size_pricing_from_form()
        if size_pairs:
            item.size_pricing_json = json.dumps(size_pairs)
    else:
        item.has_size_variants = False
        item.size_pricing_json = None

    prep_station = request.form.get("prep_station", "").strip().lower()
    if prep_station in PREP_STATION_OPTIONS:
        item.prep_station = prep_station

    item.available = True if request.form.get("available") else False
    db.session.commit()
    flash("Menu item updated.", "success")
    return redirect(url_for("cafe.menu", section="items"))


@bp.route("/menu/items/<int:item_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_menu_item(item_id):
    item = MenuItem.query.get_or_404(item_id)
    item.is_deleted = True
    item.available = False
    db.session.commit()
    flash("Menu item moved to Deleted Items. You can restore it later.", "success")
    return redirect(url_for("cafe.menu", section="deleted_items"))


@bp.route("/menu/items/<int:item_id>/restore", methods=["POST"])
@roles_required("admin", "manager")
def restore_menu_item(item_id):
    item = MenuItem.query.get_or_404(item_id)
    item.is_deleted = False
    db.session.commit()
    flash("Menu item restored.", "success")
    return redirect(url_for("cafe.menu", section="deleted_items"))


@bp.route("/orders", methods=["GET", "POST"])
@login_required
def orders():
    if request.method == "POST":
        selected_table_id = int(request.form["table_id"])
        cutoff_message = _order_cutoff_message("staff")
        if cutoff_message:
            flash(cutoff_message, "error")
            return redirect(url_for("cafe.orders", table_id=selected_table_id))
        order = _create_order(
            table_id=selected_table_id,
            ordered_by_user_id=g.current_user.id,
            status="pending_approval",
            payment_type=None,
            payment_reference=None,
        )
        if not order:
            flash("Please add at least one menu item in cart.", "error")
            return redirect(url_for("cafe.orders", table_id=selected_table_id))
        flash("Order created.", "success")
        return redirect(url_for("cafe.orders", table_id=selected_table_id))

    table_id = request.args.get("table_id", type=int)
    category_id = request.args.get("category_id", type=int)
    item_type = (request.args.get("item_type") or "").strip()
    tables = CafeTable.query.filter_by(active=True).order_by(CafeTable.name).all()
    if not table_id and tables:
        table_id = tables[0].id

    menu_query = MenuItem.query.filter_by(available=True, is_deleted=False)
    menu_query = _apply_category_filter(menu_query, category_id)
    if item_type:
        menu_query = menu_query.filter(MenuItem.item_type == item_type)
    filtered_items = menu_query.all()

    frequency_rows = (
        db.session.query(
            CafeOrderItem.menu_item_id,
            db.func.coalesce(db.func.sum(CafeOrderItem.quantity), 0).label("order_qty"),
        )
        .group_by(CafeOrderItem.menu_item_id)
        .all()
    )
    item_frequency = {row.menu_item_id: int(row.order_qty) for row in frequency_rows}
    menu_items = sorted(
        filtered_items,
        key=lambda item: (-item_frequency.get(item.id, 0), item.name.lower()),
    )
    item_size_map = {}
    for item in menu_items:
        sizes = []
        if item.size_pricing_json:
            try:
                raw = json.loads(item.size_pricing_json)
                if isinstance(raw, list):
                    for row in raw:
                        if isinstance(row, dict) and row.get("size") and row.get("price") is not None:
                            sizes.append({"size": str(row["size"]), "price": float(row["price"])})
            except (TypeError, ValueError, json.JSONDecodeError):
                sizes = []
        item_size_map[item.id] = sizes

    categories = _visible_categories_for_available_menu(include_protected=True)
    category_name_by_id = {c.id: c.name for c in categories}
    item_category_names_map = {
        item.id: _get_item_category_names(item, category_name_by_id) for item in menu_items
    }
    item_types = [
        row[0]
        for row in db.session.query(MenuItem.item_type)
        .filter(MenuItem.available.is_(True))
        .distinct()
        .order_by(MenuItem.item_type.asc())
        .all()
    ]

    return render_template(
        "cafe/orders.html",
        tables=tables,
        menu_items=menu_items,
        orders=CafeOrder.query.order_by(CafeOrder.created_at.desc()).limit(50).all(),
        categories=categories,
        item_types=item_types,
        selected_category_id=category_id,
        selected_item_type=item_type,
        item_frequency=item_frequency,
        item_size_map=item_size_map,
        item_category_names_map=item_category_names_map,
        selected_table_id=table_id,
        table_orders=CafeOrder.query.filter(
            CafeOrder.table_id == table_id, CafeOrder.status.notin_(["paid", "cancelled"])
        )
        .order_by(CafeOrder.created_at.desc())
        .all()
        if table_id
        else [],
    )


@bp.route("/orders/<int:order_id>/mark-paid", methods=["POST"])
@roles_required("admin", "manager", "cashier")
def mark_order_paid(order_id):
    order = CafeOrder.query.get(order_id)
    if not order:
        flash("Order not found. It may have already been updated on another screen.", "error")
        return redirect(request.form.get("next") or url_for("cafe.cashier"))
    payment_type = request.form.get("payment_type", "").strip() or order.payment_type or "Cash"
    payment_reference = request.form.get("payment_reference", "").strip() or order.payment_reference
    order.status = "paid"
    order.paid_at = datetime.utcnow()
    order.payment_type = payment_type
    order.payment_reference = payment_reference
    sms_message = ""
    if request.form.get("send_receipt_sms"):
        cc = (request.form.get("country_code") or "+91").strip()
        mobile = "".join(ch for ch in (request.form.get("mobile") or "") if ch.isdigit())
        if mobile:
            msg = (
                f"Brownberries Cafe receipt for Order #{_format_pickup_number(order)} "
                f"(Ref: {order.display_code or _format_internal_order_code(order)}): {_receipt_link_for_order(order)}"
            )
            ok, sms_resp = _send_receipt_sms(cc, mobile, msg)
            sms_message = " SMS sent." if ok else f" SMS not sent: {sms_resp}"
        else:
            sms_message = " SMS not sent: mobile missing."
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash(f"Order #{_format_pickup_number(order)} marked as paid.{sms_message}", "success")
    next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("cafe.cashier", table_id=order.table_id))


@bp.route("/orders/<int:order_id>/approve", methods=["POST"])
@roles_required("admin", "manager", "cashier", "librarian", "staff")
def approve_order(order_id):
    order = CafeOrder.query.get(order_id)
    if not order:
        flash("Order not found. It may have already been updated on another screen.", "error")
        return redirect(request.form.get("next") or url_for("cafe.cashier"))
    decision = (request.form.get("decision") or "approve").strip().lower()
    if decision not in ["approve", "reject"]:
        decision = "approve"
    pending_items = [oi for oi in order.order_items if (oi.approval_status or "pending") == "pending"]
    if not pending_items:
        flash("No pending items left for action.", "error")
        return redirect(request.form.get("next") or url_for("cafe.cashier", table_id=order.table_id))
    selected_ids = {int(v) for v in request.form.getlist("pending_item_ids") if str(v).isdigit()}
    if not selected_ids:
        flash("Select at least one pending item.", "error")
        return redirect(request.form.get("next") or url_for("cafe.cashier", table_id=order.table_id))
    approved_names = []
    rejected_names = []
    for oi in pending_items:
        if oi.id not in selected_ids:
            continue
        if decision == "approve":
            oi.approval_status = "approved"
            approved_names.append(f"{oi.menu_item.name if oi.menu_item else 'Item'} x {oi.quantity}")
        else:
            oi.approval_status = "rejected"
            rejected_names.append(f"{oi.menu_item.name if oi.menu_item else 'Item'} x {oi.quantity}")

    pending_left = [oi for oi in order.order_items if (oi.approval_status or "pending") == "pending"]
    approved_items = [oi for oi in order.order_items if (oi.approval_status or "pending") == "approved"]
    _recalculate_order_totals(order)
    if approved_items:
        order.status = "open"
    elif pending_left:
        order.status = "pending_approval"
    else:
        order.status = "cancelled"
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    _emit_ops_notification(
        f"Order #{_format_pickup_number(order)} decision updated",
        kind="order_decision",
        payload={
            "order_id": order.display_code or _format_internal_order_code(order),
            "pickup_no": _format_pickup_number(order),
            "table_id": order.table_id,
            "approved_items": approved_names,
            "rejected_items": rejected_names,
        },
    )
    flash(f"Order #{_format_pickup_number(order)}: selected items {decision}d.", "success")
    return redirect(request.form.get("next") or url_for("cafe.cashier", table_id=order.table_id))


@bp.route("/tables/<int:table_id>/clear-orders", methods=["POST"])
@roles_required("admin", "manager", "cashier")
def clear_table_orders(table_id):
    table = CafeTable.query.get_or_404(table_id)
    split_rows, split_error = _parse_split_payment_rows()
    if split_error:
        flash(split_error, "error")
        next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
        return redirect(next_url or url_for("cafe.cashier", table_id=table.id, tab="running"))
    selected_order_ids = {
        int(value) for value in request.form.getlist("order_ids") if str(value).isdigit()
    }
    if not selected_order_ids:
        flash("Select at least one order to settle.", "error")
        next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
        return redirect(next_url or url_for("cafe.cashier", table_id=table.id, tab="running"))
    orders = CafeOrder.query.filter(
        CafeOrder.table_id == table.id,
        CafeOrder.status.notin_(["paid", "cancelled"]),
    ).all()
    orders = [order for order in orders if order.id in selected_order_ids]
    payable_orders = [
        order for order in orders
        if not any((oi.approval_status or "pending") == "pending" for oi in order.order_items)
    ]
    if not payable_orders:
        flash("Select at least one approved order to settle.", "error")
        next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
        return redirect(next_url or url_for("cafe.cashier", table_id=table.id, tab="running"))
    selected_total = round(sum(float(order.total_amount or 0) for order in payable_orders), 2)
    split_total = round(sum(float(row["amount"]) for row in split_rows), 2)
    if abs(split_total - selected_total) > 0.01:
        flash(f"Payment split total ₹{split_total:.2f} must match selected orders total ₹{selected_total:.2f}.", "error")
        next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
        return redirect(next_url or url_for("cafe.cashier", table_id=table.id, tab="running"))
    count = 0
    paid_now = datetime.utcnow()
    summary_label = split_rows[0]["method"] if len(split_rows) == 1 else "Split Payment"
    summary_ref = ", ".join(
        [f'{row["method"]}: ₹{row["amount"]:.2f}' + (f' ({row["reference"]})' if row["reference"] else "") for row in split_rows]
    )[:120] or None
    payment_breakdown_json = json.dumps(split_rows)
    for order in payable_orders:
        order.status = "paid"
        order.paid_at = paid_now
        order.payment_type = summary_label
        order.payment_reference = summary_ref or order.payment_reference
        order.payment_breakdown_json = payment_breakdown_json
        payload = _serialize_order(order)
        socketio.emit("order_updated", payload, namespace="/kitchen")
        socketio.emit("order_updated", payload, namespace="/table")
        count += 1
    db.session.commit()
    flash(f"Settled {count} order(s) for {table.name}.", "success")
    next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("cafe.cashier", table_id=table.id))


@bp.route("/cashier")
@roles_required("admin", "manager", "cashier")
def cashier():
    tab = (request.args.get("tab") or "running").strip().lower()
    if tab not in ["running", "all_orders"]:
        tab = "running"
    table_id = request.args.get("table_id", type=int)
    today_ist = datetime.now(IST_TZ).date()
    sel_year = request.args.get("year", type=int) or today_ist.year
    sel_month = request.args.get("month", type=int) or today_ist.month
    sel_day = request.args.get("day", type=int) or today_ist.day
    try:
        selected_date = date(sel_year, sel_month, sel_day)
    except ValueError:
        selected_date = today_ist
        sel_year, sel_month, sel_day = selected_date.year, selected_date.month, selected_date.day
    day_start = _utc_naive_from_ist(datetime.combine(selected_date, time.min))
    day_end = _utc_naive_from_ist(datetime.combine(selected_date + timedelta(days=1), time.min))
    today_day_start = _utc_naive_from_ist(datetime.combine(today_ist, time.min))
    today_day_end = _utc_naive_from_ist(datetime.combine(today_ist + timedelta(days=1), time.min))
    tables = CafeTable.query.filter_by(active=True).order_by(CafeTable.name).all()
    running_rows = (
        db.session.query(
            CafeOrder.table_id,
            db.func.count(CafeOrder.id).label("order_count"),
            db.func.coalesce(db.func.sum(CafeOrder.total_amount), 0).label("pending_total"),
        )
        .filter(
            CafeOrder.status.notin_(["paid", "cancelled"]),
            CafeOrder.created_at >= day_start,
            CafeOrder.created_at < day_end,
        )
        .group_by(CafeOrder.table_id)
        .all()
    )
    running_map = {
        row.table_id: {
            "count": int(row.order_count or 0),
            "total": float(row.pending_total or 0),
        }
        for row in running_rows
    }
    if not table_id and tables:
        running_table_ids = [t.id for t in tables if running_map.get(t.id, {}).get("count", 0) > 0]
        table_id = running_table_ids[0] if running_table_ids else tables[0].id
    selected_table = CafeTable.query.get(table_id) if table_id else None
    total_sale_today = (
        db.session.query(db.func.coalesce(db.func.sum(CafeOrder.total_amount), 0))
        .filter(
            CafeOrder.status == "paid",
            CafeOrder.paid_at.is_not(None),
            CafeOrder.paid_at >= today_day_start,
            CafeOrder.paid_at < today_day_end,
        )
        .scalar()
        or 0
    )
    unpaid_orders = []
    table_total = 0
    table_settle_total = 0
    payable_order_id_map = {}
    if selected_table:
        unpaid_orders = (
            CafeOrder.query.filter(
                CafeOrder.table_id == selected_table.id,
                CafeOrder.status.notin_(["paid", "cancelled"]),
                CafeOrder.created_at >= day_start,
                CafeOrder.created_at < day_end,
            )
            .order_by(CafeOrder.created_at.asc())
            .all()
        )
        table_total = round(sum(o.total_amount for o in unpaid_orders), 2)
        payable_order_id_map = {
            o.id: (not any((oi.approval_status or "pending") == "pending" for oi in o.order_items))
            for o in unpaid_orders
        }
        table_settle_total = round(
            sum(float(o.total_amount or 0) for o in unpaid_orders if payable_order_id_map.get(o.id)),
            2,
        )
    running_order_time_map = {
        o.id: _format_ist(o.created_at, "%I:%M:%S %p")
        for o in unpaid_orders
    }
    all_orders = (
        CafeOrder.query.options(joinedload(CafeOrder.table), joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item))
        .filter(CafeOrder.created_at >= day_start, CafeOrder.created_at < day_end)
        .order_by(CafeOrder.created_at.desc())
        .all()
    )
    all_orders_payload = []
    public_base = (current_app.config.get("PUBLIC_BASE_URL") or request.host_url.rstrip("/")).rstrip("/")
    for o in all_orders:
        line_subtotal = round(
            sum(
                float(x.unit_price or 0) * int(x.quantity or 0)
                for x in o.order_items
                if (x.approval_status or "pending") != "rejected"
            ),
            2,
        )
        all_orders_payload.append(
            {
                "id": o.id,
                "code": o.display_code or _format_internal_order_code(o),
                "pickup_no": _format_pickup_number(o),
                "pickup_no_compact": _format_pickup_number(o, compact=True),
                "table": o.table.name if o.table else "-",
                "status": o.status,
                "payment_type": o.payment_type or "-",
                "payment_reference": o.payment_reference or "-",
                "payment_breakdown": (
                    json.loads(o.payment_breakdown_json)
                    if (o.payment_breakdown_json and str(o.payment_breakdown_json).strip().startswith("["))
                    else []
                ),
                "created_at": _format_ist(o.created_at, "%I:%M:%S %p"),
                "items": [
                    {
                        "name": (oi.menu_item.name if oi.menu_item else "Item"),
                        "qty": int(oi.quantity or 0),
                        "size_label": oi.size_label or "",
                        "is_parcel": bool(oi.is_parcel),
                        "unit_price": round(float(oi.unit_price or 0), 2),
                    }
                    for oi in o.order_items
                    if (oi.approval_status or "pending") != "rejected"
                ],
                "subtotal": line_subtotal,
                "packaging_charge": round(float(o.packaging_charge or 0), 2),
                "service_charge": 0.0,
                "tax_amount": 0.0,
                "total_amount": round(float(o.total_amount or 0), 2),
                "receipt_link": f"{public_base}/cafe/receipt/{o.id}",
            }
        )
    years = list(range(max(2024, date.today().year - 2), date.today().year + 3))
    return render_template(
        "cafe/cashier.html",
        tab=tab,
        tables=tables,
        running_map=running_map,
        selected_table=selected_table,
        unpaid_orders=unpaid_orders,
        payable_order_id_map=payable_order_id_map,
        running_order_time_map=running_order_time_map,
        table_total=table_total,
        table_settle_total=table_settle_total,
        total_sale_today=round(float(total_sale_today), 2),
        all_orders=all_orders,
        all_orders_payload=all_orders_payload,
        selected_date=selected_date,
        sel_year=sel_year,
        sel_month=sel_month,
        sel_day=sel_day,
        years=years,
    )


@bp.route("/receipt/<int:order_id>")
def public_receipt(order_id):
    order = CafeOrder.query.options(
        joinedload(CafeOrder.table),
        joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item),
    ).get_or_404(order_id)
    subtotal = round(sum(float(oi.unit_price or 0) * int(oi.quantity or 0) for oi in order.order_items if (oi.approval_status or "pending") != "rejected"), 2)
    packaging_charge = round(float(order.packaging_charge or 0), 2)
    service_charge = 0.0
    tax_amount = 0.0
    return render_template(
        "cafe/receipt_public.html",
        order=order,
        subtotal=subtotal,
        packaging_charge=packaging_charge,
        service_charge=service_charge,
        tax_amount=tax_amount,
    )


@bp.route("/orders/<int:order_id>/send-receipt-sms", methods=["POST"])
@roles_required("admin", "manager", "cashier")
def send_order_receipt_sms(order_id):
    order = CafeOrder.query.options(joinedload(CafeOrder.table)).get_or_404(order_id)
    country_code = (request.form.get("country_code") or "+91").strip()
    mobile = (request.form.get("mobile") or "").strip()
    if not mobile or not any(ch.isdigit() for ch in mobile):
        return jsonify({"ok": False, "message": "Valid mobile number is required."}), 400
    mobile_digits = "".join(ch for ch in mobile if ch.isdigit())
    message = (
        f"Brownberries Cafe receipt for Order #{_format_pickup_number(order)} "
        f"(Ref: {order.display_code or _format_internal_order_code(order)}): {_receipt_link_for_order(order)}"
    )
    ok, msg = _send_receipt_sms(country_code, mobile_digits, message)
    if ok:
        return jsonify({"ok": True, "message": "Receipt SMS sent."})
    return jsonify({"ok": False, "message": msg}), 400


@bp.route("/orders/<int:order_id>/edit-items", methods=["POST"])
@roles_required("admin", "manager", "cashier")
def edit_order_items(order_id):
    order = CafeOrder.query.get_or_404(order_id)
    if order.status in ["paid", "cancelled"]:
        flash("Paid/cancelled orders cannot be edited.", "error")
        return redirect(request.form.get("next") or url_for("cafe.cashier", table_id=order.table_id, tab="running"))
    rows = list(order.order_items)
    kept = 0
    for oi in rows:
        keep_values = request.form.getlist(f"keep_{oi.id}")
        include_flag = "1" in keep_values
        qty_raw = (request.form.get(f"qty_{oi.id}") or "").strip()
        qty = int(qty_raw) if qty_raw.isdigit() else 0
        if not include_flag or qty <= 0:
            db.session.delete(oi)
            continue
        oi.quantity = qty
        oi.is_parcel = True if request.form.get(f"is_parcel_{oi.id}") else False
        kept += 1
    db.session.flush()
    order = CafeOrder.query.options(joinedload(CafeOrder.order_items)).get(order_id)
    if kept == 0:
        order.status = "cancelled"
    _recalculate_order_totals(order)
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash("Order updated.", "success")
    return redirect(request.form.get("next") or url_for("cafe.cashier", table_id=order.table_id, tab="running"))


@bp.route("/table-order", methods=["POST"])
def table_order():
    slug = (request.form.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        flash("Invalid table QR.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    cutoff_message = _order_cutoff_message("qr")
    if cutoff_message:
        flash(cutoff_message, "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    order = _create_order(
        table_id=table.id,
        ordered_by_user_id=g.current_user.id if getattr(g, "current_user", None) else _get_qr_guest_user_id(),
        status="pending_approval",
        payment_type=None,
        payment_reference=None,
    )
    if not order:
        flash("Please add at least one menu item in cart.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    session["qr_success_toast"] = "Order placed successfully"
    return redirect(url_for("main.table_qr_page", slug=slug))


@bp.route("/table-order/<int:order_id>/delete", methods=["POST"])
def delete_pending_table_order(order_id):
    slug = (request.form.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        flash("Invalid table QR.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    order = CafeOrder.query.get_or_404(order_id)
    if order.table_id != table.id or order.status != "pending_approval":
        flash("Only pending approval orders can be deleted.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    for oi in list(order.order_items):
        db.session.delete(oi)
    db.session.delete(order)
    db.session.commit()
    flash("Pending order deleted.", "success")
    return redirect(url_for("main.table_qr_page", slug=slug))


@bp.route("/table-order/<int:order_id>/edit", methods=["POST"])
def edit_pending_table_order(order_id):
    slug = (request.form.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        flash("Invalid table QR.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    order = CafeOrder.query.get_or_404(order_id)
    if order.table_id != table.id or order.status != "pending_approval":
        flash("Only pending approval orders can be edited.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    line_items = _parse_line_items_from_request()
    if not line_items:
        flash("Please add at least one menu item in cart.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    for oi in list(order.order_items):
        db.session.delete(oi)
    for row in line_items:
        menu_item, qty, is_parcel, size_label, unit_price = row if len(row) == 5 else (*row, None, None)  # type: ignore
        price_to_use = float(unit_price) if unit_price is not None else float(menu_item.price)
        db.session.add(
            CafeOrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                quantity=qty,
                unit_price=price_to_use,
                size_label=size_label,
                is_parcel=bool(is_parcel),
                approval_status="pending",
            )
        )
    db.session.flush()
    _recalculate_order_totals(order)
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash("Pending order updated.", "success")
    return redirect(url_for("main.table_qr_page", slug=slug))


@bp.route("/table/call-staff", methods=["POST"])
def call_staff_for_table():
    slug = (request.form.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        return jsonify({"ok": False, "message": "Table not found."}), 404
    _emit_ops_notification(
        f"Staff requested at {table.name}",
        kind="staff_call",
        payload={"table_id": table.id, "table_name": table.name},
    )
    return jsonify({"ok": True, "message": f"Staff has been notified for {table.name}."})


@bp.route("/kitchen")
@login_required
def kitchen_display():
    station = (request.args.get("station") or "kitchen").strip().lower()
    if station not in PREP_STATION_OPTIONS:
        station = "kitchen"
    orders = (
        CafeOrder.query.join(CafeOrderItem, CafeOrderItem.order_id == CafeOrder.id)
        .join(MenuItem, MenuItem.id == CafeOrderItem.menu_item_id)
        .filter(
            CafeOrder.status.in_(["open", "preparing", "ready"]),
            MenuItem.prep_station == station,
        )
        .options(
            joinedload(CafeOrder.table),
            joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item),
        )
        .distinct()
        .order_by(CafeOrder.created_at.asc())
        .all()
    )
    recipes = (
        InventoryRecipe.query.join(MenuItem, MenuItem.id == InventoryRecipe.menu_item_id)
        .filter(InventoryRecipe.active.is_(True), MenuItem.prep_station == station)
        .options(joinedload(InventoryRecipe.menu_item), joinedload(InventoryRecipe.ingredients).joinedload(InventoryRecipeItem.inventory_item))
        .all()
    )
    recipe_map = {recipe.menu_item_id: recipe for recipe in recipes}
    order_cards = []
    prep_minutes = []
    for order in orders:
        station_items = []
        order_expected = 0
        for oi in order.order_items:
            if not oi.menu_item or oi.menu_item.prep_station != station:
                continue
            if (oi.approval_status or "pending") != "approved":
                continue
            recipe = recipe_map.get(oi.menu_item_id)
            sop = _serialize_recipe_sop(recipe, oi.size_label)
            expected_minutes = int(sop.get("prep_time_minutes") or 0)
            if expected_minutes > 0:
                order_expected = max(order_expected, expected_minutes)
            station_items.append(
                {
                    "id": oi.id,
                    "name": oi.menu_item.name,
                    "qty": int(oi.quantity or 0),
                    "size_label": oi.size_label or "",
                    "is_parcel": bool(oi.is_parcel),
                    "approval_status": oi.approval_status or "pending",
                    "sop": sop,
                }
            )
        if not station_items:
            continue
        created_local = _ist_from_utc_naive(order.created_at)
        elapsed_minutes = max(0, int(((datetime.now(IST_TZ) - created_local).total_seconds() // 60) if created_local else 0))
        prep_minutes.append(elapsed_minutes)
        order_cards.append(
            {
                "id": order.id,
                "pickup_no": _format_pickup_number(order),
                "pickup_no_compact": _format_pickup_number(order, compact=True),
                "display_code": order.display_code or _format_internal_order_code(order),
                "table_name": order.table.name if order.table else "-",
                "status": order.status,
                "total_amount": round(float(order.total_amount or 0), 2),
                "created_at": _format_ist(order.created_at, "%I:%M %p"),
                "created_at_iso": created_local.isoformat() if created_local else "",
                "elapsed_minutes": elapsed_minutes,
                "expected_minutes": order_expected,
                "items": station_items,
            }
        )
    sop_library = [
        {
            "menu_item_id": recipe.menu_item_id,
            "name": recipe.menu_item.name if recipe.menu_item else "Menu Item",
            "prep_station": recipe.menu_item.prep_station if recipe.menu_item else station,
            "sizes": _load_menu_item_size_variants(recipe.menu_item),
            "sop": _serialize_recipe_sop(recipe),
        }
        for recipe in sorted(recipes, key=lambda r: (r.menu_item.name.lower() if r.menu_item else ""))
    ]
    avg_ticket_minutes = round(sum(prep_minutes) / len(prep_minutes), 1) if prep_minutes else 0
    return render_template(
        "cafe/kitchen_display.html",
        orders=orders,
        order_cards=order_cards,
        station=station,
        active_orders=len(order_cards),
        avg_ticket_minutes=avg_ticket_minutes,
        sop_library=sop_library,
    )


@bp.route("/barista")
@login_required
def barista_display():
    return redirect(url_for("cafe.kitchen_display", station="barista"))


@bp.route("/orders/<int:order_id>/status", methods=["POST"])
@roles_required("admin", "manager", "staff", "server", "barista", "chef", "cashier")
def update_order_status(order_id):
    order = CafeOrder.query.get_or_404(order_id)
    new_status = request.form["status"]
    if new_status not in ["open", "preparing", "ready", "served", "cancelled", "paid"]:
        flash("Invalid status.", "error")
        next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
        return redirect(next_url or url_for("cafe.kitchen_display"))
    order.status = new_status
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash("Order status updated.", "success")
    next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
    return redirect(next_url or url_for("cafe.kitchen_display"))


def _inventory_item_status(item: InventoryItem):
    current_amount = float(item.current_amount or 0)
    reorder_level = float(item.reorder_level or 0)
    required_amount = float(item.required_amount or 0)
    if current_amount <= reorder_level:
        return "low"
    if required_amount > 0 and current_amount >= required_amount * 1.35:
        return "overstock"
    return "healthy"


def _inventory_filtered_items(items, search_text: str, area_filter: str, category_filter: str, status_filter: str):
    filtered = []
    q = search_text.strip().lower()
    for item in items:
        status_key = _inventory_item_status(item)
        if q:
            hay = " ".join(
                [
                    item.name or "",
                    item.item_code or "",
                    item.category_name or "",
                    item.subcategory_name or "",
                    item.storage_location or "",
                    item.selling_relation or "",
                ]
            ).lower()
            if q not in hay:
                continue
        if area_filter != "all" and (item.area or "").lower() != area_filter:
            continue
        if category_filter != "all" and (item.category_name or "").strip().lower() != category_filter:
            continue
        if status_filter != "all" and status_key != status_filter:
            continue
        filtered.append(item)
    return filtered


@bp.route("/inventory", methods=["GET", "POST"])
@roles_required("owner", "admin", "manager", "accountant", "barista", "inventory_manager")
def inventory():
    section = (request.args.get("section") or "dashboard").strip().lower()
    allowed_sections = {
        "dashboard", "daily_closing", "stock_levels", "purchases", "vendors",
        "recipes", "wastage", "analytics", "categories", "settings"
    }
    if section not in allowed_sections:
        section = "dashboard"

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        if action == "add_item":
            item = InventoryItem(
                item_code=(request.form.get("item_code") or "").strip() or None,
                area=(request.form.get("area") or "kitchen").strip(),
                name=request.form.get("name", "").strip(),
                category_name=(request.form.get("category_name") or "").strip() or None,
                subcategory_name=(request.form.get("subcategory_name") or "").strip() or None,
                unit=(request.form.get("unit") or "pcs").strip(),
                current_amount=_safe_float(request.form.get("current_amount"), 0),
                required_amount=_safe_float(request.form.get("required_amount"), 0),
                reorder_level=_safe_float(request.form.get("reorder_level"), 0),
                average_daily_usage=_safe_float(request.form.get("average_daily_usage"), 0),
                purchase_price=_safe_float(request.form.get("purchase_price"), 0),
                selling_relation=(request.form.get("selling_relation") or "").strip() or None,
                shelf_life_days=_safe_int(request.form.get("shelf_life_days"), 0) if request.form.get("shelf_life_days") else None,
                expiry_tracking=True if request.form.get("expiry_tracking") else False,
                storage_location=(request.form.get("storage_location") or "").strip() or None,
                vendor_id=int(request.form.get("vendor_id")) if request.form.get("vendor_id") else None,
                note=(request.form.get("note") or "").strip() or None,
            )
            if not item.name:
                flash("Item name is required.", "error")
                return redirect(url_for("cafe.inventory", section="stock_levels"))
            db.session.add(item)
            db.session.commit()
            flash("Inventory item added.", "success")
            return redirect(url_for("cafe.inventory", section="stock_levels"))

        if action == "update_item":
            item = InventoryItem.query.get_or_404(_safe_int(request.form.get("item_id"), 0))
            item.item_code = (request.form.get("item_code") or "").strip() or None
            item.name = (request.form.get("name") or "").strip()
            item.category_name = (request.form.get("category_name") or "").strip() or None
            item.subcategory_name = (request.form.get("subcategory_name") or "").strip() or None
            item.area = (request.form.get("area") or item.area or "kitchen").strip()
            item.unit = (request.form.get("unit") or item.unit or "pcs").strip()
            item.current_amount = _safe_float(request.form.get("current_amount"), item.current_amount or 0)
            item.reorder_level = _safe_float(request.form.get("reorder_level"), item.reorder_level or 0)
            item.required_amount = _safe_float(request.form.get("required_amount"), item.required_amount or 0)
            item.average_daily_usage = _safe_float(request.form.get("average_daily_usage"), item.average_daily_usage or 0)
            item.purchase_price = _safe_float(request.form.get("purchase_price"), item.purchase_price or 0)
            item.shelf_life_days = _safe_int(request.form.get("shelf_life_days"), 0) if request.form.get("shelf_life_days") else None
            item.expiry_tracking = True if request.form.get("expiry_tracking") else False
            item.vendor_id = _safe_int(request.form.get("vendor_id"), 0) or None
            item.storage_location = (request.form.get("storage_location") or "").strip() or None
            item.selling_relation = (request.form.get("selling_relation") or "").strip() or None
            item.note = (request.form.get("note") or "").strip() or None
            if not item.name:
                flash("Item name is required.", "error")
                return redirect(url_for("cafe.inventory", section="stock_levels", edit_item_id=item.id))
            db.session.commit()
            flash("Inventory item updated.", "success")
            return redirect(url_for("cafe.inventory", section="stock_levels", edit_item_id=item.id))

        if action == "adjust_stock":
            item = InventoryItem.query.get_or_404(_safe_int(request.form.get("item_id"), 0))
            adjustment = _safe_float(request.form.get("adjustment"), 0)
            if adjustment == 0:
                flash("Enter a stock adjustment value.", "error")
                return redirect(url_for("cafe.inventory", section="stock_levels"))
            item.current_amount = round(max(0.0, float(item.current_amount or 0) + adjustment), 3)
            db.session.commit()
            flash("Stock adjusted.", "success")
            return redirect(url_for("cafe.inventory", section="stock_levels", edit_item_id=item.id))

        if action == "daily_closing_save":
            closing_date = date.fromisoformat(request.form.get("closing_date") or date.today().isoformat())
            item_ids = [int(v) for v in request.form.getlist("item_id") if str(v).isdigit()]
            for item_id in item_ids:
                item = InventoryItem.query.get(item_id)
                if not item:
                    continue
                closing_raw = (request.form.get(f"closing_stock_{item_id}") or "").strip()
                if closing_raw == "":
                    continue
                closing_stock = _safe_float(closing_raw, 0)
                prev_row = (
                    InventoryDailyClosing.query.filter(
                        InventoryDailyClosing.item_id == item_id,
                        InventoryDailyClosing.closing_date < closing_date,
                    )
                    .order_by(InventoryDailyClosing.closing_date.desc())
                    .first()
                )
                opening_stock = float(prev_row.closing_stock) if prev_row else float(item.current_amount or 0)
                consumed = round(opening_stock - closing_stock, 3)
                variance = round(consumed - float(item.average_daily_usage or 0), 3)
                row = InventoryDailyClosing.query.filter_by(item_id=item_id, closing_date=closing_date).first()
                if not row:
                    row = InventoryDailyClosing(item_id=item_id, closing_date=closing_date)
                    db.session.add(row)
                row.opening_stock = opening_stock
                row.closing_stock = closing_stock
                row.consumed_amount = consumed
                row.variance_amount = variance
                row.note = (request.form.get(f"closing_note_{item_id}") or "").strip() or None
                item.current_amount = closing_stock
            db.session.commit()
            flash("Daily closing saved.", "success")
            return redirect(url_for("cafe.inventory", section="daily_closing", closing_date=closing_date.isoformat()))

        if action == "add_vendor":
            vendor = InventoryVendor(
                name=(request.form.get("name") or "").strip(),
                vendor_category=(request.form.get("vendor_category") or "").strip() or None,
                contact_person=(request.form.get("contact_person") or "").strip() or None,
                phone=(request.form.get("phone") or "").strip() or None,
                email=(request.form.get("email") or "").strip() or None,
                gst_number=(request.form.get("gst_number") or "").strip() or None,
                payment_terms=(request.form.get("payment_terms") or "").strip() or None,
                outstanding_balance=_safe_float(request.form.get("outstanding_balance"), 0),
                average_rate_note=(request.form.get("average_rate_note") or "").strip() or None,
                note=(request.form.get("note") or "").strip() or None,
                active=True,
            )
            if not vendor.name:
                flash("Vendor name is required.", "error")
                return redirect(url_for("cafe.inventory", section="vendors"))
            db.session.add(vendor)
            db.session.commit()
            flash("Vendor added.", "success")
            return redirect(url_for("cafe.inventory", section="vendors"))

        if action == "update_vendor":
            vendor = InventoryVendor.query.get_or_404(_safe_int(request.form.get("vendor_id"), 0))
            vendor.name = (request.form.get("name") or "").strip() or vendor.name
            vendor.vendor_category = (request.form.get("vendor_category") or "").strip() or None
            vendor.contact_person = (request.form.get("contact_person") or "").strip() or None
            vendor.phone = (request.form.get("phone") or "").strip() or None
            vendor.email = (request.form.get("email") or "").strip() or None
            vendor.gst_number = (request.form.get("gst_number") or "").strip() or None
            vendor.payment_terms = (request.form.get("payment_terms") or "").strip() or None
            vendor.outstanding_balance = _safe_float(request.form.get("outstanding_balance"), vendor.outstanding_balance or 0)
            vendor.average_rate_note = (request.form.get("average_rate_note") or "").strip() or None
            vendor.note = (request.form.get("note") or "").strip() or None
            db.session.commit()
            flash("Vendor updated.", "success")
            return redirect(url_for("cafe.inventory", section="vendors", edit_vendor_id=vendor.id))

        if action == "add_purchase":
            purchase = InventoryPurchase(
                purchase_date=date.fromisoformat(request.form.get("purchase_date") or date.today().isoformat()),
                vendor_id=int(request.form.get("vendor_id")) if request.form.get("vendor_id") else None,
                invoice_number=(request.form.get("invoice_number") or "").strip() or None,
                tax_amount=_safe_float(request.form.get("tax_amount"), 0),
                payment_status=(request.form.get("payment_status") or "pending").strip(),
                note=(request.form.get("note") or "").strip() or None,
            )
            db.session.add(purchase)
            db.session.flush()
            subtotal = 0.0
            item_ids = [int(v) for v in request.form.getlist("purchase_item_id") if str(v).isdigit()]
            for item_id in item_ids:
                qty = _safe_float(request.form.get(f"purchase_qty_{item_id}"), 0)
                unit_price = _safe_float(request.form.get(f"purchase_price_{item_id}"), 0)
                if qty <= 0:
                    continue
                line_total = round(qty * unit_price, 2)
                db.session.add(
                    InventoryPurchaseLine(
                        purchase_id=purchase.id,
                        item_id=item_id,
                        quantity=qty,
                        unit_price=unit_price,
                        line_total=line_total,
                    )
                )
                item = InventoryItem.query.get(item_id)
                if item:
                    item.current_amount = round(float(item.current_amount or 0) + qty, 3)
                    if unit_price > 0:
                        item.purchase_price = unit_price
                subtotal += line_total
            purchase.subtotal = round(subtotal, 2)
            purchase.total_amount = round(subtotal + float(purchase.tax_amount or 0), 2)
            db.session.commit()
            flash("Purchase logged and stock updated.", "success")
            return redirect(url_for("cafe.inventory", section="purchases"))

        if action == "save_recipe":
            menu_item_id = int(request.form.get("menu_item_id") or 0)
            if menu_item_id <= 0:
                flash("Please select a menu item.", "error")
                return redirect(url_for("cafe.inventory", section="recipes"))
            recipe = InventoryRecipe.query.filter_by(menu_item_id=menu_item_id).first()
            if not recipe:
                recipe = InventoryRecipe(menu_item_id=menu_item_id, yield_qty=1, active=True)
                db.session.add(recipe)
                db.session.flush()
            recipe.yield_qty = _safe_float(request.form.get("yield_qty"), 1)
            recipe.yield_unit = (request.form.get("yield_unit") or "").strip() or None
            recipe.prep_time_minutes = _safe_int(request.form.get("prep_time_minutes"), 0) or None
            recipe.ingredients_note = (request.form.get("ingredients_note") or "").strip() or None
            recipe.preparation_steps = (request.form.get("preparation_steps") or "").strip() or None
            recipe.plating_notes = (request.form.get("plating_notes") or "").strip() or None
            recipe.quality_checks = (request.form.get("quality_checks") or "").strip() or None
            recipe.allergy_alerts = (request.form.get("allergy_alerts") or "").strip() or None
            recipe.training_notes = (request.form.get("training_notes") or "").strip() or None
            recipe.sop_photo_url = (request.form.get("sop_photo_url") or "").strip() or None
            size_notes = _parse_recipe_size_notes_from_form()
            recipe.size_sop_json = json.dumps(size_notes) if size_notes else None
            for old in list(recipe.ingredients):
                db.session.delete(old)
            ingredient_ids = [int(v) for v in request.form.getlist("recipe_item_id") if str(v).isdigit()]
            for inv_id in ingredient_ids:
                qty = _safe_float(request.form.get(f"recipe_qty_{inv_id}"), 0)
                unit = (request.form.get(f"recipe_unit_{inv_id}") or "pcs").strip()
                if qty <= 0:
                    continue
                db.session.add(
                    InventoryRecipeItem(
                        recipe_id=recipe.id,
                        inventory_item_id=inv_id,
                        qty_per_menu=qty,
                        unit=unit,
                    )
                )
            db.session.commit()
            flash("Recipe and SOP saved.", "success")
            return redirect(url_for("cafe.inventory", section="recipes"))

        if action == "add_wastage":
            item_id = int(request.form.get("item_id") or 0)
            qty = _safe_float(request.form.get("quantity"), 0)
            if item_id <= 0 or qty <= 0:
                flash("Select item and quantity.", "error")
                return redirect(url_for("cafe.inventory", section="wastage"))
            wastage = InventoryWastage(
                wastage_date=date.fromisoformat(request.form.get("wastage_date") or date.today().isoformat()),
                item_id=item_id,
                quantity=qty,
                reason=(request.form.get("reason") or "").strip() or None,
            )
            db.session.add(wastage)
            item = InventoryItem.query.get(item_id)
            if item:
                item.current_amount = round(max(0.0, float(item.current_amount or 0) - qty), 3)
            db.session.commit()
            flash("Wastage logged.", "success")
            return redirect(url_for("cafe.inventory", section="wastage"))

        if action == "add_category":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Category name is required.", "error")
                return redirect(url_for("cafe.inventory", section="categories"))
            if InventoryCategory.query.filter(db.func.lower(InventoryCategory.name) == name.lower()).first():
                flash("Category already exists.", "error")
                return redirect(url_for("cafe.inventory", section="categories"))
            db.session.add(
                InventoryCategory(
                    name=name,
                    icon=(request.form.get("icon") or "").strip() or None,
                    color=(request.form.get("color") or "").strip() or None,
                    active=True,
                )
            )
            db.session.commit()
            flash("Category added.", "success")
            return redirect(url_for("cafe.inventory", section="categories"))

        if action == "update_category":
            category = InventoryCategory.query.get_or_404(_safe_int(request.form.get("category_id"), 0))
            category.name = (request.form.get("name") or "").strip() or category.name
            category.icon = (request.form.get("icon") or "").strip() or None
            category.color = (request.form.get("color") or "").strip() or None
            category.active = True if request.form.get("active") else False
            db.session.commit()
            flash("Category updated.", "success")
            return redirect(url_for("cafe.inventory", section="categories", edit_category_id=category.id))

    closing_date = date.fromisoformat(request.args.get("closing_date") or date.today().isoformat())
    inventory_search = (request.args.get("q") or "").strip()
    inventory_area = (request.args.get("area") or "all").strip().lower()
    if inventory_area not in ["all", "kitchen", "barista", "cafe"]:
        inventory_area = "all"
    inventory_category_filter = (request.args.get("category") or "all").strip().lower()
    inventory_status_filter = (request.args.get("status") or "all").strip().lower()
    if inventory_status_filter not in ["all", "healthy", "low", "overstock"]:
        inventory_status_filter = "all"
    categories = InventoryCategory.query.filter_by(active=True).order_by(InventoryCategory.name.asc()).all()
    vendors = InventoryVendor.query.filter_by(active=True).order_by(InventoryVendor.name.asc()).all()
    items = InventoryItem.query.order_by(InventoryItem.category_name.asc(), InventoryItem.name.asc()).all()
    filtered_items = _inventory_filtered_items(items, inventory_search, inventory_area, inventory_category_filter, inventory_status_filter)
    purchases = InventoryPurchase.query.order_by(InventoryPurchase.purchase_date.desc(), InventoryPurchase.id.desc()).limit(80).all()
    wastage_rows = InventoryWastage.query.order_by(InventoryWastage.wastage_date.desc(), InventoryWastage.id.desc()).limit(120).all()
    recipes = (
        InventoryRecipe.query.options(
            joinedload(InventoryRecipe.menu_item),
            joinedload(InventoryRecipe.ingredients).joinedload(InventoryRecipeItem.inventory_item),
        )
        .order_by(InventoryRecipe.id.desc())
        .all()
    )
    menu_items = MenuItem.query.filter_by(available=True, is_deleted=False).order_by(MenuItem.name.asc()).all()
    menu_item_meta = {
        item.id: {
            "name": item.name,
            "prep_station": item.prep_station,
            "sizes": _load_menu_item_size_variants(item),
        }
        for item in menu_items
    }
    recipe_payload_map = {
        recipe.menu_item_id: {
            "yield_qty": round(float(recipe.yield_qty or 0), 3),
            "yield_unit": recipe.yield_unit or "",
            "prep_time_minutes": int(recipe.prep_time_minutes or 0),
            "ingredients_note": recipe.ingredients_note or "",
            "preparation_steps": recipe.preparation_steps or "",
            "plating_notes": recipe.plating_notes or "",
            "quality_checks": recipe.quality_checks or "",
            "allergy_alerts": recipe.allergy_alerts or "",
            "training_notes": recipe.training_notes or "",
            "sop_photo_url": recipe.sop_photo_url or "",
            "size_notes": _recipe_size_note_map(recipe),
            "ingredient_ids": [int(ing.inventory_item_id) for ing in recipe.ingredients if ing.inventory_item_id],
            "ingredients": {
                int(ing.inventory_item_id): {
                    "qty": round(float(ing.qty_per_menu or 0), 3),
                    "unit": ing.unit or (ing.inventory_item.unit if ing.inventory_item else ""),
                }
                for ing in recipe.ingredients
                if ing.inventory_item_id
            },
        }
        for recipe in recipes
    }
    edit_item_id = request.args.get("edit_item_id", type=int)
    selected_item = InventoryItem.query.get(edit_item_id) if edit_item_id else (filtered_items[0] if filtered_items else None)
    edit_vendor_id = request.args.get("edit_vendor_id", type=int)
    selected_vendor = InventoryVendor.query.get(edit_vendor_id) if edit_vendor_id else (vendors[0] if vendors else None)
    edit_category_id = request.args.get("edit_category_id", type=int)
    selected_category = InventoryCategory.query.get(edit_category_id) if edit_category_id else (categories[0] if categories else None)

    category_stats = []
    for cat in categories:
        cat_items = [x for x in items if (x.category_name or "").strip().lower() == cat.name.lower()]
        stock_value = round(sum(float(x.current_amount or 0) * float(x.purchase_price or 0) for x in cat_items), 2)
        category_stats.append({"category": cat, "item_count": len(cat_items), "stock_value": stock_value})

    low_stock_items = [x for x in items if float(x.current_amount or 0) <= float(x.reorder_level or 0)]
    overstock_items = [x for x in items if _inventory_item_status(x) == "overstock"]
    items_without_vendor = [x for x in items if not x.vendor_id]
    total_stock_value = round(sum(float(x.current_amount or 0) * float(x.purchase_price or 0) for x in items), 2)
    today_purchase_spend = round(
        sum(float(p.total_amount or 0) for p in purchases if p.purchase_date == date.today()),
        2,
    )
    today_wastage_value = round(
        sum(float(w.quantity or 0) * float((w.item.purchase_price if w.item else 0) or 0) for w in wastage_rows if w.wastage_date == date.today()),
        2,
    )

    today_consumption = (
        db.session.query(db.func.coalesce(db.func.sum(InventoryDailyClosing.consumed_amount), 0.0))
        .filter(InventoryDailyClosing.closing_date == closing_date)
        .scalar()
        or 0.0
    )

    daily_rows = []
    for item in items:
        existing = InventoryDailyClosing.query.filter_by(item_id=item.id, closing_date=closing_date).first()
        prev_row = (
            InventoryDailyClosing.query.filter(
                InventoryDailyClosing.item_id == item.id,
                InventoryDailyClosing.closing_date < closing_date,
            )
            .order_by(InventoryDailyClosing.closing_date.desc())
            .first()
        )
        opening = float(prev_row.closing_stock) if prev_row else float(item.current_amount or 0)
        daily_rows.append({
            "item": item,
            "opening": opening,
            "existing": existing,
            "consumed": float(existing.consumed_amount) if existing else 0.0,
        })
    daily_rows_grouped = {}
    for row in daily_rows:
        key = row["item"].category_name or "Uncategorized"
        daily_rows_grouped.setdefault(key, []).append(row)

    vendor_purchase_map = {}
    for purchase in purchases:
        if purchase.vendor_id:
            data = vendor_purchase_map.setdefault(purchase.vendor_id, {"count": 0, "spend": 0.0})
            data["count"] += 1
            data["spend"] = round(data["spend"] + float(purchase.total_amount or 0), 2)

    top_stock_value_items = sorted(
        items,
        key=lambda x: float(x.current_amount or 0) * float(x.purchase_price or 0),
        reverse=True,
    )[:8]
    top_consumption_rows = sorted(
        daily_rows,
        key=lambda x: float(x["existing"].consumed_amount if x["existing"] else 0),
        reverse=True,
    )[:8]
    recent_purchase_lines = (
        InventoryPurchaseLine.query.options(joinedload(InventoryPurchaseLine.item), joinedload(InventoryPurchaseLine.purchase))
        .order_by(InventoryPurchaseLine.created_at.desc())
        .limit(12)
        .all()
    )

    inventory_analytics = {
        "total_items": len(items),
        "low_stock_count": len(low_stock_items),
        "overstock_count": len(overstock_items),
        "unassigned_vendor_count": len(items_without_vendor),
        "total_stock_value": total_stock_value,
        "today_purchase_spend": today_purchase_spend,
        "today_wastage_value": today_wastage_value,
        "today_consumption": round(today_consumption, 2),
    }

    return render_template(
        "cafe/inventory.html",
        section=section,
        categories=categories,
        category_stats=category_stats,
        vendors=vendors,
        items=items,
        purchases=purchases,
        recent_purchase_lines=recent_purchase_lines,
        recipes=recipes,
        menu_items=menu_items,
        wastage_rows=wastage_rows,
        low_stock_items=low_stock_items,
        overstock_items=overstock_items,
        items_without_vendor=items_without_vendor,
        daily_rows=daily_rows,
        daily_rows_grouped=daily_rows_grouped,
        closing_date=closing_date,
        inventory_analytics=inventory_analytics,
        filtered_items=filtered_items,
        selected_item=selected_item,
        selected_vendor=selected_vendor,
        selected_category=selected_category,
        inventory_search=inventory_search,
        inventory_area=inventory_area,
        inventory_category_filter=inventory_category_filter,
        inventory_status_filter=inventory_status_filter,
        top_stock_value_items=top_stock_value_items,
        top_consumption_rows=top_consumption_rows,
        vendor_purchase_map=vendor_purchase_map,
        menu_item_meta=menu_item_meta,
        recipe_payload_map=recipe_payload_map,
    )


@bp.route("/stats")
@roles_required("admin", "manager")
def stats():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    category_options = _stats_category_options()
    return render_template(
        "cafe/stats.html",
        stats_payload=payload,
        filters=payload["filters"],
        category_options=category_options,
    )


@bp.route("/stats/export")
@roles_required("admin", "manager")
def export_stats():
    payload = _build_stats_payload(_parse_stats_filters(request.args), use_cache=False)
    filters = payload["filters"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(
        [
            "Order",
            "Table",
            "Order Type",
            "Sales Source Mix",
            "Status",
            "Payment Type",
            "Subtotal",
            "Packaging",
            "Delivery",
            "Total",
            "Created At",
        ]
    )
    for order in payload["orders"]:
        ws.append(
            [
                order["code"],
                order["table"],
                order["order_type_label"],
                order["sales_source_mix"],
                order["status"],
                order["payment_type"],
                order["line_subtotal"],
                order["packaging_charge"],
                order["delivery_charge"],
                order["total_amount"],
                order["created_at"],
            ]
        )
    ws2 = wb.create_sheet(title="Top Items")
    ws2.append(["Rank", "Item", "Category", "Qty", "Revenue", "Avg Price", "Contribution %"])
    for row in payload["item_analytics"]["top_items"]:
        ws2.append(
            [
                row["rank"],
                row["name"],
                row["category"],
                row["qty"],
                row["revenue"],
                row["avg_price"],
                row["contribution_pct"],
            ]
        )
    ws3 = wb.create_sheet(title="Summary")
    ws3.append(["Period", payload["period"]["label"]])
    ws3.append(["Preset", filters["preset"]])
    ws3.append(["Sales Source", filters["salesSource"]])
    ws3.append(["Order Type", filters["orderType"]])
    ws3.append(["Category", filters["category"]])
    ws3.append([])
    kpi = payload["summary"]
    ws3.append(["Total Sales", kpi["revenue"]["total_sales"]])
    ws3.append(["Total Orders", kpi["revenue"]["total_orders"]])
    ws3.append(["Average Order Value", kpi["revenue"]["average_order_value"]])
    ws3.append(["Kitchen Sales", kpi["operations"]["kitchen_sales"]])
    ws3.append(["Barista Sales", kpi["operations"]["barista_sales"]])
    from io import BytesIO

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": f'attachment; filename="cafe_stats_{filters["preset"]}.xlsx"',
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )


_STATS_CACHE = {}
_STATS_CACHE_TTL_SECONDS = 120
_STAT_FILTER_PRESETS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "last_7_days": "Last 7 Days",
    "last_30_days": "Last 30 Days",
    "this_month": "This Month",
    "last_month": "Last Month",
    "this_quarter": "This Quarter",
    "last_quarter": "Last Quarter",
    "this_year": "This Year",
    "custom": "Custom Date Range",
}


def _parse_date_only(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_datetime_flexible(value):
    if not value:
        return None
    raw = value.strip()
    for parser in (datetime.fromisoformat,):
        try:
            return parser(raw)
        except ValueError:
            continue
    parsed_date = _parse_date_only(raw)
    if parsed_date:
        return datetime.combine(parsed_date, time.min)
    return None


def _quarter_start(d: date):
    q = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, q, 1)


def _month_start(d: date):
    return date(d.year, d.month, 1)


def _next_month_start(d: date):
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _resolve_period_from_filters(filters):
    today = datetime.now(IST_TZ).date()
    preset = filters["preset"]
    if preset == "yesterday":
        start = datetime.combine(today - timedelta(days=1), time.min)
        end = start + timedelta(days=1)
        label = "Yesterday"
        granularity = "hour"
    elif preset == "last_7_days":
        start = datetime.combine(today - timedelta(days=6), time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = "Last 7 Days"
        granularity = "day"
    elif preset == "last_30_days":
        start = datetime.combine(today - timedelta(days=29), time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = "Last 30 Days"
        granularity = "day"
    elif preset == "this_month":
        month_start = _month_start(today)
        start = datetime.combine(month_start, time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = today.strftime("%B %Y")
        granularity = "day"
    elif preset == "last_month":
        this_month_start = _month_start(today)
        prev_month_end = this_month_start - timedelta(days=1)
        prev_month_start = _month_start(prev_month_end)
        start = datetime.combine(prev_month_start, time.min)
        end = datetime.combine(this_month_start, time.min)
        label = prev_month_start.strftime("%B %Y")
        granularity = "day"
    elif preset == "this_quarter":
        qstart = _quarter_start(today)
        start = datetime.combine(qstart, time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        quarter_no = ((today.month - 1) // 3) + 1
        label = f"Q{quarter_no} {today.year}"
        granularity = "day"
    elif preset == "last_quarter":
        this_q_start = _quarter_start(today)
        prev_q_end = this_q_start - timedelta(days=1)
        prev_q_start = _quarter_start(prev_q_end)
        start = datetime.combine(prev_q_start, time.min)
        end = datetime.combine(this_q_start, time.min)
        quarter_no = ((prev_q_start.month - 1) // 3) + 1
        label = f"Q{quarter_no} {prev_q_start.year}"
        granularity = "day"
    elif preset == "this_year":
        year_start = date(today.year, 1, 1)
        start = datetime.combine(year_start, time.min)
        end = datetime.combine(today + timedelta(days=1), time.min)
        label = str(today.year)
        granularity = "month"
    elif preset == "custom":
        start_date = _parse_date_only(filters.get("startDate"))
        end_date = _parse_date_only(filters.get("endDate"))
        if not start_date:
            start_date = today
        if not end_date:
            end_date = start_date
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        start = datetime.combine(start_date, time.min)
        end = datetime.combine(end_date + timedelta(days=1), time.min)
        label = f"{start_date.isoformat()} to {end_date.isoformat()}"
        days = (end - start).days
        granularity = "hour" if days <= 1 else ("day" if days <= 92 else "month")
    else:
        start = datetime.combine(today, time.min)
        end = start + timedelta(days=1)
        label = "Today"
        granularity = "hour"
    return start, end, label, granularity


def _parse_stats_filters(args):
    mode = (args.get("mode") or "").strip().lower()
    day_str = (args.get("day") or "").strip()
    from_str = (args.get("from") or "").strip()
    to_str = (args.get("to") or "").strip()
    preset = (args.get("preset") or "").strip().lower()
    if not preset and mode in ["day", "week", "month", "range"]:
        if mode == "day":
            preset = "custom" if day_str else "today"
            if day_str:
                args = dict(args)
                args["startDate"] = day_str
                args["endDate"] = day_str
        elif mode == "week":
            preset = "last_7_days"
        elif mode == "month":
            preset = "this_month"
        else:
            preset = "custom"
            if from_str:
                parsed = _parse_datetime_flexible(from_str)
                if parsed:
                    args = dict(args)
                    args["startDate"] = parsed.date().isoformat()
            if to_str:
                parsed = _parse_datetime_flexible(to_str)
                if parsed:
                    args = dict(args)
                    args["endDate"] = parsed.date().isoformat()
    if preset not in _STAT_FILTER_PRESETS:
        preset = "today"
    sales_source = (args.get("salesSource") or "all").strip().lower()
    if sales_source not in ["all", "barista", "kitchen"]:
        sales_source = "all"
    order_type = (args.get("orderType") or "all").strip().lower()
    if order_type not in ["all", "dine_in", "takeaway", "online_order"]:
        order_type = "all"
    category = (args.get("category") or "all").strip()
    return {
        "preset": preset,
        "startDate": (args.get("startDate") or "").strip(),
        "endDate": (args.get("endDate") or "").strip(),
        "salesSource": sales_source,
        "orderType": order_type,
        "category": category or "all",
    }


def _stats_category_options():
    defaults = ["Coffee", "Tea", "Shake", "Mocktail", "Pizza", "Pasta", "Noodles", "Snacks", "Other"]
    from_db = [c.name for c in MenuCategory.query.order_by(MenuCategory.name.asc()).all() if c.name]
    out = []
    seen = set()
    for name in (defaults + from_db):
        key = name.strip().lower()
        if key and key not in seen:
            out.append(name.strip())
            seen.add(key)
    return out


def _menu_item_category_names_for_stats(item: MenuItem, category_map: dict[int, str]):
    names = _get_item_category_names(item, category_map)
    return names or ["Other"]


def _order_type_key(order: CafeOrder, approved_items):
    if order.is_delivery:
        return "online_order"
    if approved_items and all(bool(x.is_parcel) for x in approved_items):
        return "takeaway"
    return "dine_in"


def _order_type_label(key):
    return {
        "dine_in": "Dine In",
        "takeaway": "Takeaway",
        "online_order": "Online Order",
    }.get(key, "Dine In")


def _growth(curr, prev):
    if prev == 0:
        if curr == 0:
            return 0.0
        return 100.0
    return round(((curr - prev) / prev) * 100.0, 2)


def _bucket_key(dt: datetime, granularity: str):
    if granularity == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_label(dt: datetime, granularity: str):
    if granularity == "hour":
        return dt.strftime("%H:00")
    if granularity == "month":
        return dt.strftime("%b %Y")
    return dt.strftime("%d %b")


def _build_stats_payload(filters, use_cache=True):
    start_dt, end_dt, period_label, granularity = _resolve_period_from_filters(filters)
    query_start_dt = _utc_naive_from_ist(start_dt)
    query_end_dt = _utc_naive_from_ist(end_dt)
    prev_start = start_dt - (end_dt - start_dt)
    prev_end = start_dt
    prev_query_start = _utc_naive_from_ist(prev_start)
    prev_query_end = _utc_naive_from_ist(prev_end)
    cache_key = (
        start_dt.isoformat(),
        end_dt.isoformat(),
        filters["salesSource"],
        filters["orderType"],
        filters["category"].strip().lower(),
    )
    now_ts = datetime.utcnow().timestamp()
    if use_cache:
        existing = _STATS_CACHE.get(cache_key)
        if existing and (now_ts - existing["ts"] < _STATS_CACHE_TTL_SECONDS):
            return existing["payload"]

    category_rows = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    category_map = {c.id: c.name for c in category_rows}
    selected_category = (filters["category"] or "all").strip().lower()
    sales_source = filters["salesSource"]
    order_type_filter = filters["orderType"]

    orders = (
        CafeOrder.query.options(
            joinedload(CafeOrder.table),
            joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item),
        )
        .filter(
            CafeOrder.created_at >= query_start_dt,
            CafeOrder.created_at < query_end_dt,
            CafeOrder.status != "cancelled",
        )
        .order_by(CafeOrder.created_at.asc())
        .all()
    )
    prev_orders = (
        CafeOrder.query.options(joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item))
        .filter(
            CafeOrder.created_at >= prev_query_start,
            CafeOrder.created_at < prev_query_end,
            CafeOrder.status != "cancelled",
        )
        .all()
    )

    def _filtered_rows(order_rows):
        filtered = []
        for order in order_rows:
            approved_items = [oi for oi in order.order_items if (oi.approval_status or "pending") != "rejected" and oi.menu_item]
            if not approved_items:
                continue
            otype = _order_type_key(order, approved_items)
            if order_type_filter != "all" and otype != order_type_filter:
                continue
            all_line_subtotal = round(sum(float(oi.unit_price or 0) * int(oi.quantity or 0) for oi in approved_items), 2)
            matched_items = []
            for oi in approved_items:
                if sales_source != "all" and (oi.menu_item.prep_station or "kitchen") != sales_source:
                    continue
                categories = _menu_item_category_names_for_stats(oi.menu_item, category_map)
                if selected_category != "all" and selected_category not in [c.lower() for c in categories]:
                    continue
                matched_items.append((oi, categories))
            if not matched_items:
                continue
            matched_subtotal = round(sum(float(oi.unit_price or 0) * int(oi.quantity or 0) for oi, _ in matched_items), 2)
            extra = float(order.packaging_charge or 0) + float(order.delivery_charge or 0)
            ratio = (matched_subtotal / all_line_subtotal) if all_line_subtotal > 0 else 1.0
            matched_total = round(matched_subtotal + (extra * ratio), 2)
            filtered.append(
                {
                    "order": order,
                    "order_type": otype,
                    "approved_items": approved_items,
                    "matched_items": matched_items,
                    "matched_subtotal": matched_subtotal,
                    "matched_total": matched_total,
                    "ratio": ratio,
                }
            )
        return filtered

    filtered_orders = _filtered_rows(orders)
    filtered_prev_orders = _filtered_rows(prev_orders)

    total_sales = round(sum(row["matched_total"] for row in filtered_orders), 2)
    total_orders = len(filtered_orders)
    avg_order_value = round(total_sales / total_orders, 2) if total_orders else 0.0
    highest_order = round(max((row["matched_total"] for row in filtered_orders), default=0), 2)
    lowest_order = round(min((row["matched_total"] for row in filtered_orders), default=0), 2)
    prev_total_sales = round(sum(row["matched_total"] for row in filtered_prev_orders), 2)
    prev_total_orders = len(filtered_prev_orders)
    prev_avg = round(prev_total_sales / prev_total_orders, 2) if prev_total_orders else 0.0

    kitchen_sales = 0.0
    barista_sales = 0.0
    station_order_counts = {"kitchen": 0, "barista": 0}
    station_items = {"kitchen": {}, "barista": {}}

    total_items_sold = 0
    unique_item_ids = set()
    item_stats = {}
    orders_payload = []
    order_type_counts = {"dine_in": 0, "takeaway": 0, "online_order": 0}
    bucket_sales = {}
    bucket_orders = {}
    bucket_kitchen = {}
    bucket_barista = {}
    peak_hour_orders = {h: 0 for h in range(24)}
    peak_hour_sales = {h: 0.0 for h in range(24)}

    category_stats = {}
    category_order_ids = {}
    customers = set()
    mobile_customers = set()

    for row in filtered_orders:
        order = row["order"]
        order_local_dt = _ist_from_utc_naive(order.created_at)
        order_type_counts[row["order_type"]] += 1
        bucket = _bucket_key(order_local_dt.replace(tzinfo=None), granularity)
        bucket_sales[bucket] = round(bucket_sales.get(bucket, 0.0) + row["matched_total"], 2)
        bucket_orders[bucket] = bucket_orders.get(bucket, 0) + 1
        peak_hour_orders[order_local_dt.hour] += 1
        peak_hour_sales[order_local_dt.hour] = round(peak_hour_sales[order_local_dt.hour] + row["matched_total"], 2)
        if order.is_delivery and (order.delivery_customer_mobile or "").strip():
            key = f"m:{order.delivery_customer_mobile.strip()}"
            customers.add(key)
            mobile_customers.add(order.delivery_customer_mobile.strip())
        else:
            customers.add(f"o:{order.id}")

        order_station_mix = {"kitchen": 0.0, "barista": 0.0}
        for oi, categories in row["matched_items"]:
            qty = int(oi.quantity or 0)
            line_amount = round(float(oi.unit_price or 0) * qty, 2)
            total_items_sold += qty
            unique_item_ids.add(oi.menu_item_id)

            stat = item_stats.setdefault(
                oi.menu_item_id,
                {
                    "id": oi.menu_item_id,
                    "name": oi.menu_item.name,
                    "category": ", ".join(categories[:2]),
                    "qty": 0,
                    "revenue": 0.0,
                    "order_ids": set(),
                    "station": oi.menu_item.prep_station or "kitchen",
                },
            )
            stat["qty"] += qty
            stat["revenue"] = round(stat["revenue"] + line_amount, 2)
            stat["order_ids"].add(order.id)

            station = (oi.menu_item.prep_station or "kitchen")
            order_station_mix[station] = round(order_station_mix.get(station, 0.0) + line_amount, 2)
            st_item = station_items[station].setdefault(
                oi.menu_item_id,
                {"name": oi.menu_item.name, "qty": 0, "revenue": 0.0},
            )
            st_item["qty"] += qty
            st_item["revenue"] = round(st_item["revenue"] + line_amount, 2)

            cats_for_agg = categories or ["Other"]
            split = 1.0 / len(cats_for_agg)
            for cname in cats_for_agg:
                c = category_stats.setdefault(cname, {"revenue": 0.0, "qty": 0.0})
                c["revenue"] = round(c["revenue"] + line_amount * split, 2)
                c["qty"] = round(c["qty"] + qty * split, 2)
                category_order_ids.setdefault(cname, set()).add(order.id)

        order_mix_parts = []
        for station in ["kitchen", "barista"]:
            val = order_station_mix[station]
            if val > 0:
                order_mix_parts.append(f"{station.title()} ₹{val:.2f}")
                station_order_counts[station] += 1
        mix_label = ", ".join(order_mix_parts) if order_mix_parts else "-"

        line_subtotal = round(sum(float(oi.unit_price or 0) * int(oi.quantity or 0) for oi, _ in row["matched_items"]), 2)
        ratio = (line_subtotal / row["matched_subtotal"]) if row["matched_subtotal"] > 0 else 0
        packaging_m = round(float(order.packaging_charge or 0) * row["ratio"], 2)
        delivery_m = round(float(order.delivery_charge or 0) * row["ratio"], 2)
        orders_payload.append(
            {
                "id": order.id,
                "code": order.display_code or _format_internal_order_code(order),
                "pickup_no": _format_pickup_number(order),
                "table": "For Delivery" if order.is_delivery else (order.table.name if order.table else "-"),
                "order_type": row["order_type"],
                "order_type_label": _order_type_label(row["order_type"]),
                "sales_source_mix": mix_label,
                "status": order.status,
                "payment_type": order.payment_type or "-",
                "line_subtotal": line_subtotal,
                "packaging_charge": packaging_m,
                "delivery_charge": delivery_m,
                "total_amount": row["matched_total"],
                "created_at": _format_ist(order.created_at),
                "items": [
                    {
                        "name": oi.menu_item.name,
                        "qty": int(oi.quantity or 0),
                        "unit_price": float(oi.unit_price or 0),
                        "size_label": oi.size_label or "",
                        "is_parcel": bool(oi.is_parcel),
                    }
                    for oi, _ in row["matched_items"]
                ],
            }
        )

        total_line_revenue_for_alloc = sum(order_station_mix.values())
        if total_line_revenue_for_alloc <= 0:
            total_line_revenue_for_alloc = 1.0
        extra = row["matched_total"] - row["matched_subtotal"]
        for station in ["kitchen", "barista"]:
            station_line = order_station_mix[station]
            alloc = extra * (station_line / total_line_revenue_for_alloc)
            station_total = station_line + alloc
            if station == "kitchen":
                kitchen_sales = round(kitchen_sales + station_total, 2)
                bucket_kitchen[bucket] = round(bucket_kitchen.get(bucket, 0.0) + station_total, 2)
            else:
                barista_sales = round(barista_sales + station_total, 2)
                bucket_barista[bucket] = round(bucket_barista.get(bucket, 0.0) + station_total, 2)

    prev_kitchen = 0.0
    prev_barista = 0.0
    for row in filtered_prev_orders:
        order_station_mix = {"kitchen": 0.0, "barista": 0.0}
        for oi, _ in row["matched_items"]:
            station = oi.menu_item.prep_station or "kitchen"
            order_station_mix[station] = round(order_station_mix[station] + float(oi.unit_price or 0) * int(oi.quantity or 0), 2)
        total_line_revenue = sum(order_station_mix.values()) or 1.0
        extra = row["matched_total"] - row["matched_subtotal"]
        prev_kitchen += order_station_mix["kitchen"] + (extra * (order_station_mix["kitchen"] / total_line_revenue))
        prev_barista += order_station_mix["barista"] + (extra * (order_station_mix["barista"] / total_line_revenue))
    prev_kitchen = round(prev_kitchen, 2)
    prev_barista = round(prev_barista, 2)

    kitchen_pct = round((kitchen_sales / total_sales) * 100, 2) if total_sales else 0.0
    barista_pct = round((barista_sales / total_sales) * 100, 2) if total_sales else 0.0

    recurring_mobile_set = set()
    if mobile_customers:
        prior_mobile_rows = (
            db.session.query(CafeOrder.delivery_customer_mobile)
            .filter(
                CafeOrder.delivery_customer_mobile.in_(list(mobile_customers)),
                CafeOrder.created_at < query_start_dt,
                CafeOrder.status != "cancelled",
            )
            .all()
        )
        recurring_mobile_set = {row[0].strip() for row in prior_mobile_rows if row and row[0]}
    returning = len(recurring_mobile_set)
    new_customers = max(len(customers) - returning, 0)

    item_rows = []
    for _, s in item_stats.items():
        avg_price = round((s["revenue"] / s["qty"]), 2) if s["qty"] else 0.0
        contribution = round((s["revenue"] / total_sales) * 100, 2) if total_sales else 0.0
        item_rows.append(
            {
                "id": s["id"],
                "name": s["name"],
                "category": s["category"] or "Other",
                "qty": int(s["qty"]),
                "revenue": round(float(s["revenue"]), 2),
                "avg_price": avg_price,
                "contribution_pct": contribution,
                "station": s["station"],
                "order_count": len(s["order_ids"]),
            }
        )
    top_by_qty = sorted(item_rows, key=lambda x: (-x["qty"], -x["revenue"], x["name"].lower()))
    for idx, row in enumerate(top_by_qty, start=1):
        row["rank"] = idx
    top_items = top_by_qty[:15]
    bottom_items = sorted(item_rows, key=lambda x: (x["qty"], x["revenue"], x["name"].lower()))[:10]
    menu_q = MenuItem.query.filter(MenuItem.available.is_(True), MenuItem.is_deleted.is_(False))
    if sales_source in ["kitchen", "barista"]:
        menu_q = menu_q.filter(MenuItem.prep_station == sales_source)
    menu_all = menu_q.all()
    zero_sales = []
    sold_ids = {x["id"] for x in item_rows}
    for item in menu_all:
        if item.id in sold_ids:
            continue
        c_names = _menu_item_category_names_for_stats(item, category_map)
        if selected_category != "all" and selected_category not in [c.lower() for c in c_names]:
            continue
        zero_sales.append({"id": item.id, "name": item.name, "category": ", ".join(c_names[:2])})
    zero_sales = sorted(zero_sales, key=lambda x: x["name"].lower())[:20]

    sorted_buckets = sorted(bucket_sales.keys())
    revenue_trend_labels = [_bucket_label(b, granularity) for b in sorted_buckets]
    revenue_trend_values = [round(bucket_sales.get(b, 0.0), 2) for b in sorted_buckets]
    order_trend_values = [int(bucket_orders.get(b, 0)) for b in sorted_buckets]
    kitchen_trend = [round(bucket_kitchen.get(b, 0.0), 2) for b in sorted_buckets]
    barista_trend = [round(bucket_barista.get(b, 0.0), 2) for b in sorted_buckets]
    kitchen_contrib_trend = []
    barista_contrib_trend = []
    for i in range(len(sorted_buckets)):
        total_b = kitchen_trend[i] + barista_trend[i]
        if total_b <= 0:
            kitchen_contrib_trend.append(0.0)
            barista_contrib_trend.append(0.0)
        else:
            kitchen_contrib_trend.append(round((kitchen_trend[i] / total_b) * 100, 2))
            barista_contrib_trend.append(round((barista_trend[i] / total_b) * 100, 2))

    peak_hours = [
        {
            "hour": f"{h:02d}:00-{(h + 1) % 24:02d}:00",
            "orders": int(peak_hour_orders[h]),
            "sales": round(peak_hour_sales[h], 2),
        }
        for h in range(24)
    ]

    def _best_day_for_range(day_start_dt, day_end_dt):
        day_start_query = _utc_naive_from_ist(day_start_dt)
        day_end_query = _utc_naive_from_ist(day_end_dt)
        rows = (
            db.session.query(
                db.func.date(CafeOrder.created_at).label("day"),
                db.func.count(CafeOrder.id),
                db.func.coalesce(db.func.sum(CafeOrder.total_amount), 0.0),
            )
            .filter(
                CafeOrder.created_at >= day_start_query,
                CafeOrder.created_at < day_end_query,
                CafeOrder.status != "cancelled",
            )
            .group_by(db.func.date(CafeOrder.created_at))
            .all()
        )
        if not rows:
            return {"day": "-", "revenue": 0.0, "orders": 0}
        remapped = []
        for row_day, row_count, row_revenue in rows:
            parsed_day = datetime.fromisoformat(str(row_day))
            remapped.append(
                {
                    "day": _format_ist(parsed_day, "%Y-%m-%d"),
                    "revenue": round(float(row_revenue or 0), 2),
                    "orders": int(row_count or 0),
                }
            )
        best = max(remapped, key=lambda x: x["revenue"])
        return best

    today = datetime.now(IST_TZ).date()
    month_start = datetime.combine(_month_start(today), time.min)
    month_end = datetime.combine(_next_month_start(today), time.min)
    prev_month_start_date = _month_start(_month_start(today) - timedelta(days=1))
    prev_month_start = datetime.combine(prev_month_start_date, time.min)
    prev_month_end = datetime.combine(_month_start(today), time.min)
    best_day_ever = _best_day_for_range(datetime(2020, 1, 1), datetime.combine(today + timedelta(days=1), time.min))
    best_day_this_month = _best_day_for_range(month_start, month_end)
    best_day_last_month = _best_day_for_range(prev_month_start, prev_month_end)

    category_rows_out = []
    for name, data in sorted(category_stats.items(), key=lambda kv: kv[1]["revenue"], reverse=True):
        qty = float(data["qty"])
        rev = float(data["revenue"])
        category_rows_out.append(
            {
                "name": name,
                "revenue": round(rev, 2),
                "orders": len(category_order_ids.get(name, set())),
                "qty": round(qty, 2),
                "avg_price": round(rev / qty, 2) if qty else 0.0,
            }
        )

    def _best_station_item(station):
        rows = list(station_items[station].values())
        if not rows:
            return {"name": "-", "qty": 0, "revenue": 0.0}
        best = max(rows, key=lambda x: x["revenue"])
        return {"name": best["name"], "qty": int(best["qty"]), "revenue": round(best["revenue"], 2)}

    recipes = (
        InventoryRecipe.query.options(joinedload(InventoryRecipe.ingredients).joinedload(InventoryRecipeItem.inventory_item))
        .filter(InventoryRecipe.active.is_(True))
        .all()
    )
    recipe_map = {r.menu_item_id: r for r in recipes}
    ingredient_usage = {}
    for row in filtered_orders:
        for oi, _ in row["matched_items"]:
            recipe = recipe_map.get(oi.menu_item_id)
            if not recipe:
                continue
            for ing in recipe.ingredients:
                if not ing.inventory_item:
                    continue
                key = ing.inventory_item.name
                total_qty = float(ing.qty_per_menu or 0) * int(oi.quantity or 0)
                u = ingredient_usage.setdefault(
                    key,
                    {
                        "ingredient": key,
                        "unit": ing.unit or ing.inventory_item.unit or "unit",
                        "consumption": 0.0,
                    },
                )
                u["consumption"] = round(u["consumption"] + total_qty, 3)
    inventory_consumption_rows = sorted(ingredient_usage.values(), key=lambda x: x["consumption"], reverse=True)

    item_contrib_rows = sorted(item_rows, key=lambda x: x["revenue"], reverse=True)
    top10 = item_contrib_rows[:10]
    top10_sum = sum(x["revenue"] for x in top10)
    others_sum = round(max(total_sales - top10_sum, 0), 2)

    summary = {
        "revenue": {
            "total_sales": round(total_sales, 2),
            "total_orders": total_orders,
            "average_order_value": round(avg_order_value, 2),
            "highest_order_value": highest_order,
            "lowest_order_value": lowest_order,
        },
        "customers": {
            "total_customers_served": len(customers),
            "new_customers": int(new_customers),
            "returning_customers": int(returning),
        },
        "products": {
            "total_items_sold": int(total_items_sold),
            "unique_items_sold": len(unique_item_ids),
        },
        "operations": {
            "kitchen_sales": round(kitchen_sales, 2),
            "barista_sales": round(barista_sales, 2),
            "kitchen_contribution_pct": kitchen_pct,
            "barista_contribution_pct": barista_pct,
        },
        "growth": {
            "total_sales_pct": _growth(total_sales, prev_total_sales),
            "orders_pct": _growth(total_orders, prev_total_orders),
            "average_order_value_pct": _growth(avg_order_value, prev_avg),
            "kitchen_sales_pct": _growth(kitchen_sales, prev_kitchen),
            "barista_sales_pct": _growth(barista_sales, prev_barista),
        },
    }

    payload = {
        "filters": filters,
        "period": {
            "label": period_label,
            "start": query_start_dt.isoformat(),
            "end": query_end_dt.isoformat(),
            "previous_start": prev_query_start.isoformat(),
            "previous_end": prev_query_end.isoformat(),
            "granularity": granularity,
            "display_start_ist": start_dt.isoformat(),
            "display_end_ist": end_dt.isoformat(),
        },
        "summary": summary,
        "revenue": {
            "trend": {"labels": revenue_trend_labels, "values": revenue_trend_values},
            "split": {"kitchen": round(kitchen_sales, 2), "barista": round(barista_sales, 2)},
            "comparison": {"kitchen": round(kitchen_sales, 2), "barista": round(barista_sales, 2)},
            "contribution_trend": {
                "labels": revenue_trend_labels,
                "kitchen_pct": kitchen_contrib_trend,
                "barista_pct": barista_contrib_trend,
            },
        },
        "orders_analytics": {
            "orders_over_time": {"labels": revenue_trend_labels, "values": order_trend_values},
            "order_type_distribution": order_type_counts,
            "peak_hours": peak_hours,
            "best_sales_days": {
                "best_day_ever": best_day_ever,
                "best_day_this_month": best_day_this_month,
                "best_day_last_month": best_day_last_month,
            },
        },
        "item_analytics": {
            "top_items": top_items,
            "bottom_items": bottom_items,
            "zero_sales_items": zero_sales,
            "item_revenue_contribution": {
                "labels": [x["name"] for x in top10] + (["Others"] if others_sum > 0 else []),
                "values": [round(x["revenue"], 2) for x in top10] + ([others_sum] if others_sum > 0 else []),
            },
            "profitability": [
                {
                    "item_id": x["id"],
                    "name": x["name"],
                    "revenue": x["revenue"],
                    "estimated_cost": 0.0,
                    "profit": x["revenue"],
                    "profit_margin_pct": 100.0 if x["revenue"] > 0 else 0.0,
                }
                for x in top_items[:20]
            ],
        },
        "category_analytics": {
            "rows": category_rows_out,
            "pie": {"labels": [x["name"] for x in category_rows_out], "values": [x["revenue"] for x in category_rows_out]},
            "bar": {"labels": [x["name"] for x in category_rows_out], "values": [x["revenue"] for x in category_rows_out]},
            "trend": {"labels": revenue_trend_labels, "values": revenue_trend_values},
        },
        "kitchen_vs_barista": {
            "revenue_comparison": {"kitchen": round(kitchen_sales, 2), "barista": round(barista_sales, 2)},
            "order_count_comparison": {
                "kitchen_orders": int(station_order_counts["kitchen"]),
                "barista_orders": int(station_order_counts["barista"]),
            },
            "best_kitchen_item": _best_station_item("kitchen"),
            "best_barista_item": _best_station_item("barista"),
            "average_ticket_size": {
                "kitchen": round(kitchen_sales / station_order_counts["kitchen"], 2) if station_order_counts["kitchen"] else 0.0,
                "barista": round(barista_sales / station_order_counts["barista"], 2) if station_order_counts["barista"] else 0.0,
            },
            "contribution_trend": {
                "labels": revenue_trend_labels,
                "kitchen_pct": kitchen_contrib_trend,
                "barista_pct": barista_contrib_trend,
            },
        },
        "inventory_consumption": {
            "estimated_consumption": inventory_consumption_rows[:30],
            "most_consumed": inventory_consumption_rows[:10],
            "trend": {"labels": revenue_trend_labels, "values": [0 for _ in revenue_trend_labels]},
        },
        "orders": list(reversed(orders_payload))[:400],
        "generated_at": _format_ist(datetime.utcnow()),
    }

    _STATS_CACHE[cache_key] = {"ts": now_ts, "payload": payload}
    if len(_STATS_CACHE) > 250:
        for key in list(_STATS_CACHE.keys())[:80]:
            _STATS_CACHE.pop(key, None)
    return payload


@bp.route("/api/statistics/summary")
@roles_required("admin", "manager")
def api_stats_summary():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    return jsonify({"ok": True, "period": payload["period"], "filters": payload["filters"], "summary": payload["summary"]})


@bp.route("/api/statistics/revenue")
@roles_required("admin", "manager")
def api_stats_revenue():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    return jsonify({"ok": True, "period": payload["period"], "filters": payload["filters"], "revenue": payload["revenue"]})


@bp.route("/api/statistics/orders")
@roles_required("admin", "manager")
def api_stats_orders():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    return jsonify({"ok": True, "period": payload["period"], "filters": payload["filters"], "orders_analytics": payload["orders_analytics"]})


@bp.route("/api/statistics/items")
@roles_required("admin", "manager")
def api_stats_items():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    item_id = request.args.get("itemId", type=int)
    data = payload["item_analytics"]
    if item_id:
        start_dt = datetime.fromisoformat(payload["period"]["start"])
        end_dt = datetime.fromisoformat(payload["period"]["end"])
        rows = (
            CafeOrder.query.options(joinedload(CafeOrder.order_items))
            .filter(
                CafeOrder.created_at >= start_dt,
                CafeOrder.created_at < end_dt,
                CafeOrder.status != "cancelled",
            )
            .order_by(CafeOrder.created_at.asc())
            .all()
        )
        daily = {}
        for order in rows:
            order_local_day = _format_ist(order.created_at, "%Y-%m-%d")
            for oi in order.order_items:
                if oi.menu_item_id != item_id or (oi.approval_status or "pending") == "rejected":
                    continue
                key = order_local_day
                d = daily.setdefault(key, {"qty": 0, "revenue": 0.0})
                d["qty"] += int(oi.quantity or 0)
                d["revenue"] = round(d["revenue"] + float(oi.unit_price or 0) * int(oi.quantity or 0), 2)
        labels = sorted(daily.keys())
        trend = {
            "labels": labels,
            "qty_values": [daily[k]["qty"] for k in labels],
            "revenue_values": [daily[k]["revenue"] for k in labels],
        }
        return jsonify({"ok": True, "filters": payload["filters"], "item_trend": trend, "item_analytics": data})
    return jsonify({"ok": True, "filters": payload["filters"], "item_analytics": data})


@bp.route("/api/statistics/categories")
@roles_required("admin", "manager")
def api_stats_categories():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    return jsonify({"ok": True, "filters": payload["filters"], "category_analytics": payload["category_analytics"]})


@bp.route("/api/statistics/kitchen-vs-barista")
@roles_required("admin", "manager")
def api_stats_kitchen_barista():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    return jsonify({"ok": True, "filters": payload["filters"], "kitchen_vs_barista": payload["kitchen_vs_barista"]})


@bp.route("/api/statistics/inventory-consumption")
@roles_required("admin", "manager")
def api_stats_inventory_consumption():
    payload = _build_stats_payload(_parse_stats_filters(request.args))
    return jsonify({"ok": True, "filters": payload["filters"], "inventory_consumption": payload["inventory_consumption"]})


@bp.route("/staff", methods=["GET", "POST"])
@roles_required("admin", "manager")
def staff():
    _ensure_role_templates_exist()
    allowed_sections = {
        "add_new_staff",
        "active_staff",
        "archived_staff",
        "attendance_calendar",
        "attendance_entry",
        "leave_requests",
        "docs_review",
        "payroll_summary",
    }
    active_staff_section = (request.args.get("section") or "add_new_staff").strip().lower()
    if active_staff_section not in allowed_sections:
        active_staff_section = "add_new_staff"

    def _staff_redirect(section=None, **kwargs):
        target_section = (section or active_staff_section).strip().lower()
        if target_section not in allowed_sections:
            target_section = "add_new_staff"
        params = {"section": target_section}
        params.update(kwargs)
        return redirect(url_for("cafe.staff", **params))

    if request.method == "POST":
        action = request.form.get("action", "create")

        if action == "create":
            email = request.form["email"].strip().lower()
            existing = User.query.filter_by(email=email).first()
            if existing:
                flash("A user with this email already exists.", "error")
                return _staff_redirect("add_new_staff")
            role = request.form.get("role", "staff")
            if role not in _get_role_options():
                role = "staff"

            new_user = User(
                full_name=request.form["full_name"].strip(),
                email=email,
                password_hash=generate_password_hash(request.form["password"]),
                role=role,
                active=True,
            )
            _assign_user_type_from_role(new_user)
            db.session.add(new_user)
            db.session.flush()

            profile = _ensure_staff_profile(new_user)
            profile.joining_date = (
                date.fromisoformat(request.form["joining_date"])
                if request.form.get("joining_date")
                else None
            )
            profile.dob = date.fromisoformat(request.form["dob"]) if request.form.get("dob") else None
            profile.marital_status = request.form.get("marital_status", "").strip() or None
            profile.gender = request.form.get("gender", "").strip() or None
            profile.salary_type = request.form.get("salary_type", "").strip() or "monthly"
            salary_amount_raw = request.form.get("salary_amount", "").strip()
            profile.salary_amount = float(salary_amount_raw) if salary_amount_raw else None
            profile.probation_end_date = date.fromisoformat(request.form["probation_end_date"]) if request.form.get("probation_end_date") else None
            profile.emergency_contact = request.form.get("emergency_contact", "").strip() or None
            profile.archived = False
            db.session.commit()
            flash("Staff member added.", "success")
            return _staff_redirect("active_staff")

        if action == "update":
            user = User.query.get_or_404(int(request.form["user_id"]))
            profile = _ensure_staff_profile(user)

            user.full_name = request.form["full_name"].strip()
            user.email = request.form["email"].strip().lower()
            duplicate = User.query.filter(User.email == user.email, User.id != user.id).first()
            if duplicate:
                flash("Another user already has this email.", "error")
                return _staff_redirect("active_staff")
            user.role = request.form.get("role", user.role)
            if user.role not in _get_role_options():
                user.role = "staff"
            _assign_user_type_from_role(user)
            user.active = True if request.form.get("active") else False
            new_password = request.form.get("password", "").strip()
            if new_password:
                user.password_hash = generate_password_hash(new_password)

            profile.joining_date = (
                date.fromisoformat(request.form["joining_date"])
                if request.form.get("joining_date")
                else None
            )
            profile.dob = date.fromisoformat(request.form["dob"]) if request.form.get("dob") else None
            profile.marital_status = request.form.get("marital_status", "").strip() or None
            profile.gender = request.form.get("gender", "").strip() or None
            profile.phone = request.form.get("phone", "").strip() or None
            profile.alternate_contact = request.form.get("alternate_contact", "").strip() or None
            profile.emergency_contact = request.form.get("emergency_contact", "").strip() or None
            profile.address = request.form.get("address", "").strip() or None
            profile.salary_type = request.form.get("salary_type", "").strip() or None
            salary_amount_raw = request.form.get("salary_amount", "").strip()
            profile.salary_amount = float(salary_amount_raw) if salary_amount_raw else None
            profile.probation_end_date = date.fromisoformat(request.form["probation_end_date"]) if request.form.get("probation_end_date") else None
            profile.pan_number = request.form.get("pan_number", "").strip() or None
            profile.bank_account_name = request.form.get("bank_account_name", "").strip() or None
            profile.bank_account_number = request.form.get("bank_account_number", "").strip() or None
            profile.bank_ifsc = request.form.get("bank_ifsc", "").strip() or None
            profile.bank_name = request.form.get("bank_name", "").strip() or None
            profile.govt_id_type = request.form.get("govt_id_type", "").strip() or None
            profile.govt_id_number = request.form.get("govt_id_number", "").strip() or None
            govt_doc = request.files.get("govt_id_file")
            if govt_doc and govt_doc.filename:
                profile.govt_id_file_path = _save_uploaded_file(govt_doc, "staff_docs", f"govt-{user.id}")
            photo_file = request.files.get("photo_file")
            if photo_file and photo_file.filename:
                profile.photo_file_path = _save_uploaded_file(photo_file, "staff_photos", f"photo-{user.id}")
            profile.archived = not user.active
            db.session.commit()
            flash("Staff member updated.", "success")
            return _staff_redirect("active_staff")

        if action == "delete":
            user = User.query.get_or_404(int(request.form["user_id"]))
            if _is_protected_admin(user):
                flash("Cafe Admin cannot be deleted.", "error")
                return _staff_redirect("active_staff")
            if user.staff_profile:
                db.session.delete(user.staff_profile)
            StaffAttendance.query.filter_by(user_id=user.id).delete()
            StaffLeaveRequest.query.filter_by(user_id=user.id).delete()
            db.session.delete(user)
            db.session.commit()
            flash("Staff member deleted.", "success")
            return _staff_redirect("active_staff")

        if action == "archive":
            user = User.query.get_or_404(int(request.form["user_id"]))
            if user.role == "admin" and user.email == "admin@brownberries.local":
                flash("Cafe Admin cannot be archived.", "error")
                return _staff_redirect("active_staff")
            profile = _ensure_staff_profile(user)
            profile.archived = True
            user.active = False
            db.session.commit()
            flash("Staff member archived.", "success")
            return _staff_redirect("active_staff")

        if action == "restore":
            user = User.query.get_or_404(int(request.form["user_id"]))
            profile = _ensure_staff_profile(user)
            profile.archived = False
            user.active = True
            db.session.commit()
            flash("Staff member restored.", "success")
            return _staff_redirect("archived_staff")

        if action == "leave_decision":
            leave = StaffLeaveRequest.query.get_or_404(int(request.form["leave_id"]))
            decision = request.form.get("decision", "pending")
            if decision not in ["approved", "rejected", "pending"]:
                decision = "pending"
            leave.status = decision
            leave.admin_remarks = request.form.get("admin_remarks", "").strip() or None
            db.session.commit()
            flash("Leave request updated.", "success")
            return _staff_redirect("leave_requests")

        if action == "attendance_for_user":
            target_user_id = int(request.form["target_user_id"])
            attendance_date = date.fromisoformat(request.form["attendance_date"])
            status = request.form.get("status", "present_all_day").strip()
            if status not in dict(STAFF_ATTENDANCE_STATUS_OPTIONS):
                status = "present_all_day"
            notes = request.form.get("notes", "").strip() or None
            existing = StaffAttendance.query.filter_by(
                user_id=target_user_id, attendance_date=attendance_date
            ).first()
            if existing:
                existing.status = status
                existing.notes = notes
                existing.manager_override = True
            else:
                db.session.add(
                    StaffAttendance(
                        user_id=target_user_id,
                        attendance_date=attendance_date,
                        status=status,
                        manager_override=True,
                        notes=notes,
                    )
                )
            db.session.commit()
            flash("Attendance saved for selected staff member.", "success")
            return _staff_redirect("attendance_entry", attendance_user_id=target_user_id)

    excluded_staff_emails = ["qr.guest@brownberries.local", "delivery.guest@brownberries.local"]
    staff_users = (
        User.query.filter(~User.email.in_(excluded_staff_emails))
        .order_by(User.full_name.asc())
        .all()
    )
    for staff_user in staff_users:
        _ensure_staff_profile(staff_user)
    db.session.commit()
    staff_profiles = [u.staff_profile for u in staff_users if u.staff_profile]
    active_profiles = [s for s in staff_profiles if not s.archived]
    archived_profiles = [s for s in staff_profiles if s.archived]
    attendance_today = {
        row.user_id: row
        for row in StaffAttendance.query.filter_by(attendance_date=date.today()).all()
    }
    attendance_latest = {}
    latest_rows = (
        StaffAttendance.query.order_by(
            StaffAttendance.user_id.asc(), StaffAttendance.attendance_date.desc()
        ).all()
    )
    for row in latest_rows:
        if row.user_id not in attendance_latest:
            attendance_latest[row.user_id] = row
    leave_summary = {}
    for staff_user in staff_users:
        pending = StaffLeaveRequest.query.filter_by(user_id=staff_user.id, status="pending").count()
        approved = StaffLeaveRequest.query.filter_by(user_id=staff_user.id, status="approved").count()
        leave_summary[staff_user.id] = {"pending": pending, "approved": approved}
    leave_requests = (
        StaffLeaveRequest.query.join(User, StaffLeaveRequest.user_id == User.id)
        .order_by(StaffLeaveRequest.created_at.desc())
        .limit(80)
        .all()
    )

    selected_user_id = request.args.get("attendance_user_id", type=int)
    selected_month = request.args.get("attendance_month", type=int) or date.today().month
    selected_year = request.args.get("attendance_year", type=int) or date.today().year
    if selected_month < 1 or selected_month > 12:
        selected_month = date.today().month
    if selected_year < 2000 or selected_year > 2100:
        selected_year = date.today().year
    if not selected_user_id and active_profiles:
        selected_user_id = active_profiles[0].user_id

    days_in_month = calendar.monthrange(selected_year, selected_month)[1]
    month_rows = calendar.Calendar(firstweekday=0).monthdayscalendar(selected_year, selected_month)
    selected_attendance_map = {}
    if selected_user_id:
        rows = StaffAttendance.query.filter(
            StaffAttendance.user_id == selected_user_id,
            StaffAttendance.attendance_date >= date(selected_year, selected_month, 1),
            StaffAttendance.attendance_date <= date(selected_year, selected_month, days_in_month),
        ).all()
        selected_attendance_map = {row.attendance_date.day: row.status for row in rows}

    payroll_month = request.args.get("payroll_month", type=int) or date.today().month
    payroll_year = request.args.get("payroll_year", type=int) or date.today().year
    if payroll_month < 1 or payroll_month > 12:
        payroll_month = date.today().month
    if payroll_year < 2000 or payroll_year > 2100:
        payroll_year = date.today().year
    payroll_days = calendar.monthrange(payroll_year, payroll_month)[1]
    payroll_start = date(payroll_year, payroll_month, 1)
    payroll_end = date(payroll_year, payroll_month, payroll_days)
    payroll_rows = []
    total_payroll_estimate = 0.0
    today_attendance_rows = StaffAttendance.query.filter_by(attendance_date=date.today()).all()
    doc_pending_count = StaffDocument.query.filter(
        StaffDocument.verification_status.in_(["pending", "rejected"])
    ).count()
    pending_leave_count = StaffLeaveRequest.query.filter_by(status="pending").count()
    pending_documents = (
        StaffDocument.query.join(User, StaffDocument.user_id == User.id)
        .options(joinedload(StaffDocument.user), joinedload(StaffDocument.uploaded_by))
        .filter(StaffDocument.verification_status.in_(["pending", "rejected"]))
        .order_by(StaffDocument.created_at.desc())
        .all()
    )
    for staff_user in staff_users:
        profile = staff_user.staff_profile
        month_rows = StaffAttendance.query.filter(
            StaffAttendance.user_id == staff_user.id,
            StaffAttendance.attendance_date >= payroll_start,
            StaffAttendance.attendance_date <= payroll_end,
        ).all()
        present_days = sum(1 for row in month_rows if row.status in ["present_all_day", "late_entry", "early_exit", "weekly_off", "on_leave"])
        half_days = sum(1 for row in month_rows if row.status in ["first_half", "second_half"])
        sick_days = sum(1 for row in month_rows if row.status == "sick_leave")
        unpaid_days = sum(max(0.0, 1.0 - _staff_attendance_pay_fraction(row.status)) for row in month_rows)
        payable_days = sum(_staff_attendance_pay_fraction(row.status) for row in month_rows)
        salary_amount = float(profile.salary_amount or 0)
        per_day_salary = round((salary_amount / payroll_days), 2) if salary_amount else 0.0
        estimated_pay = round(payable_days * per_day_salary, 2)
        total_payroll_estimate += estimated_pay
        payroll_rows.append(
            {
                "user": staff_user,
                "profile": profile,
                "present_days": present_days,
                "half_days": half_days,
                "sick_days": sick_days,
                "unpaid_days": round(unpaid_days, 2),
                "payable_days": round(payable_days, 2),
                "per_day_salary": per_day_salary,
                "estimated_pay": estimated_pay,
            }
        )

    return render_template(
        "cafe/staff_admin.html",
        active_profiles=active_profiles,
        archived_profiles=archived_profiles,
        attendance_today=attendance_today,
        attendance_latest=attendance_latest,
        leave_summary=leave_summary,
        leave_requests=leave_requests,
        attendance_user_id=selected_user_id,
        attendance_month=selected_month,
        attendance_year=selected_year,
        attendance_month_rows=month_rows,
        selected_attendance_map=selected_attendance_map,
        staff_role_options=_get_role_options(),
        user_types=UserType.query.order_by(UserType.name.asc()).all(),
        active_staff_section=active_staff_section,
        attendance_status_options=STAFF_ATTENDANCE_STATUS_OPTIONS,
        payroll_rows=payroll_rows,
        payroll_month=payroll_month,
        payroll_year=payroll_year,
        total_payroll_estimate=round(total_payroll_estimate, 2),
        today_attendance_count=len(today_attendance_rows),
        document_pending_count=doc_pending_count,
        pending_documents=pending_documents,
        pending_leave_count=pending_leave_count,
    )


@bp.route("/user-types", methods=["GET", "POST"])
@roles_required("admin")
def user_types():
    _ensure_role_templates_exist()
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            name = request.form.get("name", "").strip()
            if not name:
                flash("User type name is required.", "error")
                return redirect(url_for("cafe.user_types"))
            if UserType.query.filter(db.func.lower(UserType.name) == name.lower()).first():
                flash("User type already exists.", "error")
                return redirect(url_for("cafe.user_types"))
            ut = UserType(name=name)
            for field in [
                "can_access_cafe", "can_access_library", "can_manage_staff", "can_manage_menu",
                "can_manage_orders", "can_manage_kitchen", "can_manage_inventory", "can_manage_cashier",
                "can_manage_stats", "can_manage_library_members", "can_manage_library_books",
                "can_manage_library_loans", "can_manage_library_payments", "can_manage_library_plans",
                "can_view_staff_profiles", "can_upload_salary", "can_view_delivery_locations",
            ]:
                setattr(ut, field, True if request.form.get(field) else False)
            # Operational requirement: all roles can take orders and view running orders.
            ut.can_manage_orders = True
            ut.can_manage_kitchen = True
            db.session.add(ut)
            db.session.commit()
            flash("User type created.", "success")
            return redirect(url_for("cafe.user_types"))
        if action == "update":
            ut = UserType.query.get_or_404(int(request.form["user_type_id"]))
            ut.name = request.form.get("name", ut.name).strip() or ut.name
            for field in [
                "can_access_cafe", "can_access_library", "can_manage_staff", "can_manage_menu",
                "can_manage_orders", "can_manage_kitchen", "can_manage_inventory", "can_manage_cashier",
                "can_manage_stats", "can_manage_library_members", "can_manage_library_books",
                "can_manage_library_loans", "can_manage_library_payments", "can_manage_library_plans",
                "can_view_staff_profiles", "can_upload_salary", "can_view_delivery_locations",
            ]:
                setattr(ut, field, True if request.form.get(f"{field}_{ut.id}") else False)
            ut.can_manage_orders = True
            ut.can_manage_kitchen = True
            db.session.commit()
            flash("User type updated.", "success")
            return redirect(url_for("cafe.user_types"))
        if action == "delete":
            ut = UserType.query.get_or_404(int(request.form["user_type_id"]))
            in_use = User.query.filter(
                db.or_(User.user_type_id == ut.id, db.func.lower(User.role) == ut.name.lower())
            ).count()
            if in_use > 0:
                flash(
                    f"Cannot delete role '{ut.name}' because it is assigned to {in_use} user(s). Reassign users first.",
                    "error",
                )
                return redirect(url_for("cafe.user_types"))
            db.session.delete(ut)
            db.session.commit()
            flash("Role template deleted.", "success")
            return redirect(url_for("cafe.user_types"))
    return render_template("cafe/user_types.html", user_types=UserType.query.order_by(UserType.name.asc()).all())


@bp.route("/staff/attendance/export")
@roles_required("admin", "manager")
def export_staff_attendance():
    user_id = request.args.get("user_id", type=int)
    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    user = User.query.get_or_404(user_id)
    if month is None or month < 1 or month > 12:
        month = date.today().month
    if year is None or year < 2000 or year > 2100:
        year = date.today().year
    days_in_month = calendar.monthrange(year, month)[1]
    from_dt = date(year, month, 1)
    to_dt = date(year, month, days_in_month)
    logs = (
        StaffAttendance.query.filter(
            StaffAttendance.user_id == user.id,
            StaffAttendance.attendance_date >= from_dt,
            StaffAttendance.attendance_date <= to_dt,
        )
        .order_by(StaffAttendance.attendance_date.asc())
        .all()
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    ws.append(["Staff Name", "Email", "Date", "Status", "Notes"])
    for row in logs:
        ws.append([user.full_name, user.email, row.attendance_date.isoformat(), row.status, row.notes or ""])
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    safe_name = user.full_name.lower().replace(" ", "-")
    filename = f"{safe_name}-attendance-{year}-{month:02d}.xlsx"
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )


@bp.route("/staff/<int:user_id>/id-card")
@roles_required("admin", "manager")
def download_staff_id_card(user_id):
    user = User.query.get_or_404(user_id)
    profile = _ensure_staff_profile(user)
    output = _build_staff_id_card(user, profile)
    filename = f"{user.full_name.lower().replace(' ', '-')}-staff-id.png"
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/png",
        },
    )


@bp.route("/staff/<int:user_id>/govt-id")
@roles_required("admin", "manager")
def download_staff_govt_id(user_id):
    user = User.query.get_or_404(user_id)
    profile = _ensure_staff_profile(user)
    if not profile.govt_id_file_path:
        flash("No Govt ID file uploaded for this staff member.", "error")
        return redirect(url_for("cafe.staff"))
    full_path = os.path.join(current_app.config["UPLOADS_ROOT"], profile.govt_id_file_path)
    if not os.path.exists(full_path):
        flash("Govt ID file is missing from storage.", "error")
        return redirect(url_for("cafe.staff"))
    with open(full_path, "rb") as f:
        data = f.read()
    filename = os.path.basename(full_path)
    return Response(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/octet-stream",
        },
    )


@bp.route("/my-staff", methods=["GET", "POST"])
@login_required
def my_staff():
    user = g.current_user
    if _is_staff_user(user):
        profile = _ensure_staff_profile(user)
        db.session.commit()
    else:
        profile = user.staff_profile
    if request.method == "POST":
        action = request.form.get("action")
        if action == "attendance":
            attendance_date = (
                date.fromisoformat(request.form["attendance_date"])
                if request.form.get("attendance_date")
                else date.today()
            )
            existing = StaffAttendance.query.filter_by(
                user_id=user.id, attendance_date=attendance_date
            ).first()
            if existing:
                existing.status = request.form.get("status", "present_all_day")
                existing.notes = request.form.get("notes", "").strip() or None
                flash("Attendance updated.", "success")
            else:
                db.session.add(
                    StaffAttendance(
                        user_id=user.id,
                        attendance_date=attendance_date,
                        status=request.form.get("status", "present_all_day"),
                        notes=request.form.get("notes", "").strip() or None,
                    )
                )
                flash("Attendance logged.", "success")
            db.session.commit()
            return redirect(url_for("cafe.my_staff"))

        if action == "leave":
            start_date = date.fromisoformat(request.form["start_date"])
            end_date = date.fromisoformat(request.form["end_date"])
            if end_date < start_date:
                flash("Leave end date cannot be before start date.", "error")
                return redirect(url_for("cafe.my_staff"))
            db.session.add(
                StaffLeaveRequest(
                    user_id=user.id,
                    leave_type=request.form.get("leave_type", "casual").strip() or "casual",
                    start_date=start_date,
                    end_date=end_date,
                    reason=request.form.get("reason", "").strip() or None,
                    status="pending",
                )
            )
            db.session.commit()
            flash("Leave request submitted.", "success")
            return redirect(url_for("cafe.my_staff"))

        if action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not check_password_hash(user.password_hash, current_password):
                flash("Current password is incorrect.", "error")
                return redirect(url_for("cafe.my_staff"))
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "error")
                return redirect(url_for("cafe.my_staff"))
            if new_password != confirm_password:
                flash("New password and confirmation do not match.", "error")
                return redirect(url_for("cafe.my_staff"))
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("cafe.my_staff"))

    attendance_logs = (
        StaffAttendance.query.filter_by(user_id=user.id)
        .order_by(StaffAttendance.attendance_date.desc())
        .limit(40)
        .all()
    )
    leave_logs = (
        StaffLeaveRequest.query.filter_by(user_id=user.id)
        .order_by(StaffLeaveRequest.created_at.desc())
        .limit(40)
        .all()
    )
    return render_template(
        "cafe/staff_self.html",
        profile=profile,
        attendance_logs=attendance_logs,
        leave_logs=leave_logs,
    )
