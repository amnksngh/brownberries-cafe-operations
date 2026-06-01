import json
import calendar
import math
import os
from datetime import date, datetime
from io import BytesIO
from uuid import uuid4

import qrcode
from PIL import Image, ImageDraw, ImageFont
from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for
from openpyxl import Workbook
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .auth_helpers import login_required, roles_required
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


def _get_item_category_names(item: MenuItem, category_name_by_id: dict[int, str]) -> list[str]:
    names: list[str] = []
    names_seen: set[str] = set()
    for cid in _menu_item_category_ids(item):
        cname = category_name_by_id.get(cid)
        if cname and cname.lower() not in names_seen:
            names.append(cname)
            names_seen.add(cname.lower())
    return names


def _visible_categories_for_available_menu() -> list[MenuCategory]:
    categories = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    available_items = MenuItem.query.filter_by(available=True).all()
    used_category_ids: set[int] = set()
    for item in available_items:
        for cid in _menu_item_category_ids(item):
            used_category_ids.add(cid)
    return [
        c
        for c in categories
        if c.name.lower() != "other" or c.id in used_category_ids
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
    customer_order_no = f"{(order.daily_sequence or 0):02d}" if order.daily_sequence else f"{order.id:02d}"
    return {
        "id": order.id,
        "order_code": order.display_code or str(order.id),
        "customer_order_no": customer_order_no,
        "table_id": order.table_id,
        "table": "For Delivery" if order.is_delivery else (order.table.name if order.table else "-"),
        "status": order.status,
        "payment_type": order.payment_type or "-",
        "total_amount": round(order.total_amount, 2),
        "is_delivery": bool(order.is_delivery),
        "delivery_customer_name": order.delivery_customer_name,
        "delivery_customer_mobile": order.delivery_customer_mobile,
        "delivery_address": order.delivery_address,
        "delivery_map_url": order.delivery_map_url,
        "packaging_charge": round(order.packaging_charge or 0, 2),
        "delivery_distance_km": round(order.delivery_distance_km or 0, 2),
        "delivery_charge": round(order.delivery_charge or 0, 2),
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M"),
        "pending_approval_count": pending_count,
        "items": [
            {
                "id": item.id,
                "name": item.menu_item.name if item.menu_item else "-",
                "qty": item.quantity,
                "size_label": item.size_label,
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
    items = MenuItem.query.filter(MenuItem.id.in_(item_ids), MenuItem.available.is_(True)).all()
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
    for oi in order.order_items:
        line_total += float(oi.unit_price or 0) * int(oi.quantity or 0)
        if order.is_delivery:
            packaging_charge += 20.0 * int(oi.quantity or 0)
    if not order.is_delivery:
        packaging_charge = float(order.packaging_charge or 0)
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
    today = date.today()
    today_count = CafeOrder.query.filter(db.func.date(CafeOrder.created_at) == today.isoformat()).count()
    daily_seq = today_count + 1
    display_code = f"{today.strftime('%m-%y')}-{daily_seq:02d}"

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
        display_code=display_code,
    )
    db.session.add(order)
    db.session.flush()

    total = 0.0
    packaging_charge = 0.0
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
        total += price_to_use * qty
        if is_parcel:
            packaging_charge += 20.0 * qty
        db.session.add(
            CafeOrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                quantity=qty,
                unit_price=price_to_use,
                size_label=size_label,
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

    order.packaging_charge = round(packaging_charge, 2)
    order.delivery_distance_km = delivery_distance_km
    order.delivery_charge = delivery_charge
    order.total_amount = round(total + order.packaging_charge + order.delivery_charge, 2)
    _apply_recipe_inventory_deduction(order)
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_created", payload, namespace="/kitchen")
    socketio.emit("order_created", payload, namespace="/table")
    return order


@bp.route("/")
@login_required
def home():
    return render_template(
        "cafe/home.html",
        tables=CafeTable.query.count(),
        open_orders=CafeOrder.query.filter_by(status="open").count(),
        low_stock=InventoryItem.query.filter(
            InventoryItem.current_amount <= InventoryItem.reorder_level
        ).count(),
        delivery_open_orders=CafeOrder.query.filter_by(is_delivery=True).filter(CafeOrder.status.notin_(["paid", "cancelled"])).count(),
        upcoming_bookings=TableBooking.query.filter(
            TableBooking.booking_date >= date.today(),
            TableBooking.status == "booked",
        ).count(),
    )


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
    _ensure_menu_types_seeded()
    stale_count = MenuItem.query.filter(MenuItem.subcategory_id.isnot(None)).count()
    if stale_count:
        MenuItem.query.filter(MenuItem.subcategory_id.isnot(None)).update(
            {MenuItem.subcategory_id: None}, synchronize_session=False
        )
        db.session.commit()
    if request.method == "POST":
        menu_type = MenuType.query.get(int(request.form["menu_type_id"]))
        if not menu_type:
            flash("Please select a valid item type.", "error")
            return redirect(url_for("cafe.menu"))
        category_ids = _parse_category_ids_from_form()
        if not category_ids:
            flash("Please select at least one category.", "error")
            return redirect(url_for("cafe.menu"))
        uploaded_image = _save_menu_image(request.files.get("image_file"))
        image_url = uploaded_image or (request.form.get("image_url", "").strip() or None)
        item = MenuItem(
            category_id=category_ids[0],
            subcategory_id=None,
            item_type=menu_type.name,
            category_ids_json=json.dumps(category_ids),
            name=request.form["name"].strip(),
            image_url=image_url,
            short_description=request.form.get("short_description", "").strip() or None,
            description=request.form.get("description", "").strip() or None,
            calories=int(request.form["calories"]) if request.form.get("calories") else None,
            price=float(request.form["price"]),
            has_size_variants=True if request.form.get("has_size_variants") else False,
            size_pricing_json=None,
            prep_station=request.form.get("prep_station", "kitchen"),
            available=True if request.form.get("available") else False,
        )
        if item.has_size_variants:
            size_pairs = _parse_size_pricing_from_form()
            item.size_pricing_json = json.dumps(size_pairs) if size_pairs else None
        db.session.add(item)
        db.session.commit()
        flash("Menu item added.", "success")
        return redirect(url_for("cafe.menu"))
    items = MenuItem.query.order_by(MenuItem.name).all()
    item_category_map = {}
    item_size_map = {}
    for item in items:
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
        MenuItem.query.order_by(MenuItem.name.asc()), selected_category_filter
    ).all()
    active_menu_section = (request.args.get("section") or "catalog").strip().lower()
    if active_menu_section not in ["catalog", "add_item", "items", "availability"]:
        active_menu_section = "catalog"
    return render_template(
        "cafe/menu.html",
        items=items,
        categories=MenuCategory.query.order_by(MenuCategory.name).all(),
        other_category_id=(
            MenuCategory.query.filter(db.func.lower(MenuCategory.name) == "other").first().id
            if MenuCategory.query.filter(db.func.lower(MenuCategory.name) == "other").first()
            else None
        ),
        menu_types=MenuType.query.order_by(MenuType.name).all(),
        prep_station_options=PREP_STATION_OPTIONS,
        item_category_map=item_category_map,
        item_size_map=item_size_map,
        active_menu_section=active_menu_section,
        availability_items=availability_items,
        selected_category_filter=selected_category_filter,
    )


@bp.route("/menu/availability", methods=["POST"])
@roles_required("admin", "manager")
def update_menu_availability():
    category_filter = request.form.get("category_filter", "").strip()
    category_filter_id = int(category_filter) if category_filter.isdigit() else None
    scoped_items = _apply_category_filter(MenuItem.query, category_filter_id).all()
    selected_ids = {
        int(x) for x in request.form.getlist("available_item_ids") if str(x).isdigit()
    }
    for item in scoped_items:
        item.available = item.id in selected_ids
    db.session.commit()
    flash("Menu item availability updated.", "success")
    return redirect(url_for("cafe.menu", section="availability", category_filter=category_filter or ""))


@bp.route("/menu/categories", methods=["POST"])
@roles_required("admin", "manager")
def add_category():
    name = request.form["name"].strip()
    if not name:
        flash("Category name is required.", "error")
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
    name = request.form["name"].strip()
    if not name:
        flash("Category name is required.", "error")
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
    if category.id == other.id:
        flash("Other category cannot be deleted.", "error")
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
            return redirect(url_for("cafe.menu"))
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
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/items/<int:item_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_menu_item(item_id):
    item = MenuItem.query.get_or_404(item_id)
    if CafeOrderItem.query.filter_by(menu_item_id=item.id).first():
        flash("Menu item cannot be deleted because it is used in existing orders.", "error")
        return redirect(url_for("cafe.menu"))
    db.session.delete(item)
    db.session.commit()
    flash("Menu item deleted.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/orders", methods=["GET", "POST"])
@login_required
def orders():
    if request.method == "POST":
        selected_table_id = int(request.form["table_id"])
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

    menu_query = MenuItem.query.filter_by(available=True)
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

    categories = _visible_categories_for_available_menu()
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
    order.payment_type = payment_type
    order.payment_reference = payment_reference
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash(f"Order #{order.id} marked as paid.", "success")
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
    non_rejected_items = [oi for oi in order.order_items if (oi.approval_status or "pending") != "rejected"]
    line_total = round(sum(float(oi.unit_price or 0) * int(oi.quantity or 0) for oi in non_rejected_items), 2)
    order.total_amount = round(line_total + float(order.packaging_charge or 0) + float(order.delivery_charge or 0), 2)
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
        f"Order #{order.id} decision updated",
        kind="order_decision",
        payload={
            "order_id": order.id,
            "table_id": order.table_id,
            "approved_items": approved_names,
            "rejected_items": rejected_names,
        },
    )
    flash(f"Order #{order.id}: selected items {decision}d.", "success")
    return redirect(request.form.get("next") or url_for("cafe.cashier", table_id=order.table_id))


@bp.route("/tables/<int:table_id>/clear-orders", methods=["POST"])
@roles_required("admin", "manager", "cashier")
def clear_table_orders(table_id):
    table = CafeTable.query.get_or_404(table_id)
    payment_type = request.form.get("payment_type", "").strip() or "Cash"
    payment_reference = request.form.get("payment_reference", "").strip() or None
    orders = CafeOrder.query.filter(
        CafeOrder.table_id == table.id,
        CafeOrder.status.notin_(["paid", "cancelled"]),
    ).all()
    count = 0
    for order in orders:
        order.status = "paid"
        order.payment_type = payment_type
        order.payment_reference = payment_reference or order.payment_reference
        payload = _serialize_order(order)
        socketio.emit("order_updated", payload, namespace="/kitchen")
        socketio.emit("order_updated", payload, namespace="/table")
        count += 1
    db.session.commit()
    flash(f"Cleared {count} order(s) for {table.name}.", "success")
    next_url = request.form.get("next", "").strip() or request.args.get("next", "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("cafe.cashier", table_id=table.id))


@bp.route("/cashier")
@roles_required("admin", "manager", "cashier")
def cashier():
    table_id = request.args.get("table_id", type=int)
    tables = CafeTable.query.filter_by(active=True).order_by(CafeTable.name).all()
    running_rows = (
        db.session.query(
            CafeOrder.table_id,
            db.func.count(CafeOrder.id).label("order_count"),
            db.func.coalesce(db.func.sum(CafeOrder.total_amount), 0).label("pending_total"),
        )
        .filter(CafeOrder.status.notin_(["paid", "cancelled"]))
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
    today = date.today().isoformat()
    total_sale_today = (
        db.session.query(db.func.coalesce(db.func.sum(CafeOrder.total_amount), 0))
        .filter(CafeOrder.status == "paid", db.func.date(CafeOrder.updated_at) == today)
        .scalar()
        or 0
    )
    unpaid_orders = []
    table_total = 0
    if selected_table:
        unpaid_orders = (
            CafeOrder.query.filter(
                CafeOrder.table_id == selected_table.id,
                CafeOrder.status.notin_(["paid", "cancelled"]),
            )
            .order_by(CafeOrder.created_at.asc())
            .all()
        )
        table_total = round(sum(o.total_amount for o in unpaid_orders), 2)
    return render_template(
        "cafe/cashier.html",
        tables=tables,
        running_map=running_map,
        selected_table=selected_table,
        unpaid_orders=unpaid_orders,
        table_total=table_total,
        total_sale_today=round(float(total_sale_today), 2),
    )


@bp.route("/table-order", methods=["POST"])
def table_order():
    slug = (request.form.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        flash("Invalid table QR.", "error")
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
    flash("Order request sent for approval.", "success")
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
    total = 0.0
    packaging_charge = 0.0
    for row in line_items:
        menu_item, qty, is_parcel, size_label, unit_price = row if len(row) == 5 else (*row, None, None)  # type: ignore
        price_to_use = float(unit_price) if unit_price is not None else float(menu_item.price)
        total += price_to_use * qty
        if is_parcel:
            packaging_charge += 20.0 * qty
        db.session.add(
            CafeOrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                quantity=qty,
                unit_price=price_to_use,
                size_label=size_label,
                approval_status="pending",
            )
        )
    order.packaging_charge = round(packaging_charge, 2)
    order.total_amount = round(total + order.packaging_charge + (order.delivery_charge or 0), 2)
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash("Pending order updated.", "success")
    return redirect(url_for("main.table_qr_page", slug=slug))


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
    return render_template("cafe/kitchen_display.html", orders=orders, station=station)


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
        return redirect(url_for("cafe.kitchen_display"))
    order.status = new_status
    db.session.commit()
    payload = _serialize_order(order)
    socketio.emit("order_updated", payload, namespace="/kitchen")
    socketio.emit("order_updated", payload, namespace="/table")
    flash("Order status updated.", "success")
    return redirect(url_for("cafe.kitchen_display"))


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
                current_amount=float(request.form.get("current_amount") or 0),
                required_amount=float(request.form.get("required_amount") or 0),
                reorder_level=float(request.form.get("reorder_level") or 0),
                average_daily_usage=float(request.form.get("average_daily_usage") or 0),
                purchase_price=float(request.form.get("purchase_price") or 0),
                selling_relation=(request.form.get("selling_relation") or "").strip() or None,
                shelf_life_days=int(request.form.get("shelf_life_days")) if request.form.get("shelf_life_days") else None,
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
                closing_stock = float(closing_raw)
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
                outstanding_balance=float(request.form.get("outstanding_balance") or 0),
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

        if action == "add_purchase":
            purchase = InventoryPurchase(
                purchase_date=date.fromisoformat(request.form.get("purchase_date") or date.today().isoformat()),
                vendor_id=int(request.form.get("vendor_id")) if request.form.get("vendor_id") else None,
                invoice_number=(request.form.get("invoice_number") or "").strip() or None,
                tax_amount=float(request.form.get("tax_amount") or 0),
                payment_status=(request.form.get("payment_status") or "pending").strip(),
                note=(request.form.get("note") or "").strip() or None,
            )
            db.session.add(purchase)
            db.session.flush()
            subtotal = 0.0
            item_ids = [int(v) for v in request.form.getlist("purchase_item_id") if str(v).isdigit()]
            for item_id in item_ids:
                qty = float(request.form.get(f"purchase_qty_{item_id}") or 0)
                unit_price = float(request.form.get(f"purchase_price_{item_id}") or 0)
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
            recipe.yield_qty = float(request.form.get("yield_qty") or 1)
            recipe.yield_unit = (request.form.get("yield_unit") or "").strip() or None
            for old in list(recipe.ingredients):
                db.session.delete(old)
            ingredient_ids = [int(v) for v in request.form.getlist("recipe_item_id") if str(v).isdigit()]
            for inv_id in ingredient_ids:
                qty = float(request.form.get(f"recipe_qty_{inv_id}") or 0)
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
            flash("Recipe saved.", "success")
            return redirect(url_for("cafe.inventory", section="recipes"))

        if action == "add_wastage":
            item_id = int(request.form.get("item_id") or 0)
            qty = float(request.form.get("quantity") or 0)
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

    closing_date = date.fromisoformat(request.args.get("closing_date") or date.today().isoformat())
    categories = InventoryCategory.query.filter_by(active=True).order_by(InventoryCategory.name.asc()).all()
    vendors = InventoryVendor.query.filter_by(active=True).order_by(InventoryVendor.name.asc()).all()
    items = InventoryItem.query.order_by(InventoryItem.category_name.asc(), InventoryItem.name.asc()).all()
    purchases = InventoryPurchase.query.order_by(InventoryPurchase.purchase_date.desc(), InventoryPurchase.id.desc()).limit(80).all()
    wastage_rows = InventoryWastage.query.order_by(InventoryWastage.wastage_date.desc(), InventoryWastage.id.desc()).limit(120).all()
    recipes = InventoryRecipe.query.order_by(InventoryRecipe.id.desc()).all()
    menu_items = MenuItem.query.filter_by(available=True).order_by(MenuItem.name.asc()).all()

    category_stats = []
    for cat in categories:
        cat_items = [x for x in items if (x.category_name or "").strip().lower() == cat.name.lower()]
        stock_value = round(sum(float(x.current_amount or 0) * float(x.purchase_price or 0) for x in cat_items), 2)
        category_stats.append({"category": cat, "item_count": len(cat_items), "stock_value": stock_value})

    low_stock_items = [x for x in items if float(x.current_amount or 0) <= float(x.reorder_level or 0)]
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

    inventory_analytics = {
        "total_items": len(items),
        "low_stock_count": len(low_stock_items),
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
        recipes=recipes,
        menu_items=menu_items,
        wastage_rows=wastage_rows,
        low_stock_items=low_stock_items,
        daily_rows=daily_rows,
        closing_date=closing_date,
        inventory_analytics=inventory_analytics,
    )


@bp.route("/stats")
@roles_required("admin", "manager")
def stats():
    from_str = request.args.get("from")
    to_str = request.args.get("to")
    query = CafeOrder.query
    if from_str:
        query = query.filter(CafeOrder.created_at >= datetime.fromisoformat(from_str))
    if to_str:
        query = query.filter(CafeOrder.created_at <= datetime.fromisoformat(to_str))
    orders = query.order_by(CafeOrder.created_at.desc()).all()
    total_sales = round(sum(o.total_amount for o in orders), 2)
    return render_template("cafe/stats.html", orders=orders, total_sales=total_sales)


@bp.route("/stats/export")
@roles_required("admin", "manager")
def export_stats():
    wb = Workbook()
    ws = wb.active
    ws.title = "Cafe Sales"
    ws.append(["Order ID", "Table", "Status", "Payment Type", "Total", "Created At"])
    for order in CafeOrder.query.order_by(CafeOrder.created_at.desc()).all():
        ws.append(
            [
                order.id,
                order.table.name if order.table else "",
                order.status,
                order.payment_type or "",
                order.total_amount,
                order.created_at.strftime("%Y-%m-%d %H:%M"),
            ]
        )
    from io import BytesIO

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        headers={
            "Content-Disposition": 'attachment; filename="cafe_stats.xlsx"',
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )


@bp.route("/staff", methods=["GET", "POST"])
@roles_required("admin", "manager")
def staff():
    _ensure_role_templates_exist()
    allowed_sections = {
        "add_new_staff",
        "active_staff",
        "attendance_calendar",
        "attendance_entry",
        "leave_requests",
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
            profile.address = request.form.get("address", "").strip() or None
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
            notes = request.form.get("notes", "").strip() or None
            existing = StaffAttendance.query.filter_by(
                user_id=target_user_id, attendance_date=attendance_date
            ).first()
            if existing:
                existing.status = status
                existing.notes = notes
            else:
                db.session.add(
                    StaffAttendance(
                        user_id=target_user_id,
                        attendance_date=attendance_date,
                        status=status,
                        notes=notes,
                    )
                )
            db.session.commit()
            flash("Attendance saved for selected staff member.", "success")
            return _staff_redirect("attendance_entry", attendance_user_id=target_user_id)

    staff_users = (
        User.query.filter(User.role.in_(STAFF_ROLES), User.email != "qr.guest@brownberries.local")
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
