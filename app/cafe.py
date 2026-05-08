import json
import calendar
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
    InventoryItem,
    MenuCategory,
    MenuItem,
    MenuSubcategory,
    MenuType,
    StaffAttendance,
    UserType,
    StaffLeaveRequest,
    StaffProfile,
    User,
)

bp = Blueprint("cafe", __name__, url_prefix="/cafe")

DEFAULT_ROLE_OPTIONS = (
    "admin",
    "manager",
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
    return {
        "id": order.id,
        "table_id": order.table_id,
        "table": order.table.name if order.table else "-",
        "status": order.status,
        "payment_type": order.payment_type or "-",
        "total_amount": round(order.total_amount, 2),
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M"),
        "items": [
            {
                "name": item.menu_item.name if item.menu_item else "-",
                "qty": item.quantity,
                "prep_station": item.menu_item.prep_station if item.menu_item else "kitchen",
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


def _ensure_default_category_id() -> int:
    default_name = "General"
    existing = MenuCategory.query.filter(db.func.lower(MenuCategory.name) == default_name.lower()).first()
    if existing:
        return existing.id
    category = MenuCategory(name=default_name)
    db.session.add(category)
    db.session.commit()
    return category.id


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
                    except (TypeError, ValueError):
                        continue
                    if quantity > 0:
                        cart_entries.append((menu_item_id, quantity))
        except (json.JSONDecodeError, TypeError):
            pass

    if not cart_entries:
        for value in request.form.getlist("menu_item_id"):
            try:
                menu_item_id = int(value)
                quantity = int(request.form.get(f"qty_{menu_item_id}", 1))
                if quantity > 0:
                    cart_entries.append((menu_item_id, quantity))
            except (TypeError, ValueError):
                continue

    if not cart_entries:
        return []

    merged = {}
    for menu_item_id, quantity in cart_entries:
        merged[menu_item_id] = merged.get(menu_item_id, 0) + quantity

    items = MenuItem.query.filter(
        MenuItem.id.in_(list(merged.keys())), MenuItem.available.is_(True)
    ).all()
    item_by_id = {item.id: item for item in items}
    line_items = []
    for menu_item_id, quantity in merged.items():
        menu_item = item_by_id.get(menu_item_id)
        if menu_item:
            line_items.append((menu_item, quantity))
    return line_items


def _create_order(table_id: int, ordered_by_user_id: int, status: str, payment_type, payment_reference):
    line_items = _parse_line_items_from_request()
    if not line_items:
        return None

    order = CafeOrder(
        table_id=table_id,
        ordered_by_user_id=ordered_by_user_id,
        status=status,
        payment_type=payment_type,
        payment_reference=payment_reference,
    )
    db.session.add(order)
    db.session.flush()

    total = 0.0
    for menu_item, qty in line_items:
        total += menu_item.price * qty
        db.session.add(
            CafeOrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                quantity=qty,
                unit_price=menu_item.price,
            )
        )

    order.total_amount = round(total, 2)
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
    if request.method == "POST":
        menu_type = MenuType.query.get(int(request.form["menu_type_id"]))
        if not menu_type:
            flash("Please select a valid item type.", "error")
            return redirect(url_for("cafe.menu"))
        item = MenuItem(
            category_id=int(request.form["category_id"]),
            subcategory_id=int(request.form["subcategory_id"])
            if request.form.get("subcategory_id")
            else None,
            item_type=menu_type.name,
            name=request.form["name"].strip(),
            image_url=request.form.get("image_url", "").strip() or None,
            description=request.form.get("description", "").strip() or None,
            calories=int(request.form["calories"]) if request.form.get("calories") else None,
            price=float(request.form["price"]),
            prep_station=request.form.get("prep_station", "kitchen"),
            available=True if request.form.get("available") else False,
        )
        db.session.add(item)
        db.session.commit()
        flash("Menu item added.", "success")
        return redirect(url_for("cafe.menu"))
    return render_template(
        "cafe/menu.html",
        items=MenuItem.query.order_by(MenuItem.name).all(),
        categories=MenuCategory.query.order_by(MenuCategory.name).all(),
        subcategories=MenuSubcategory.query.order_by(MenuSubcategory.name).all(),
        menu_types=MenuType.query.order_by(MenuType.name).all(),
        prep_station_options=PREP_STATION_OPTIONS,
    )


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


@bp.route("/menu/subcategories", methods=["POST"])
@roles_required("admin", "manager")
def add_subcategory():
    name = request.form["name"].strip()
    if not name:
        flash("Subcategory name is required.", "error")
        return redirect(url_for("cafe.menu"))
    duplicate = MenuSubcategory.query.filter(
        db.func.lower(MenuSubcategory.name) == name.lower()
    ).first()
    if duplicate:
        flash("Subcategory already exists.", "error")
        return redirect(url_for("cafe.menu"))
    default_category_id = _ensure_default_category_id()
    db.session.add(
        MenuSubcategory(
            name=name,
            category_id=default_category_id,
        )
    )
    db.session.commit()
    flash("Subcategory added.", "success")
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
    if category.items or category.subcategories:
        flash("Category cannot be deleted while linked menu items/subcategories exist.", "error")
        return redirect(url_for("cafe.menu"))
    db.session.delete(category)
    db.session.commit()
    flash("Category deleted.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/subcategories/<int:subcategory_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def update_subcategory(subcategory_id):
    subcategory = MenuSubcategory.query.get_or_404(subcategory_id)
    name = request.form["name"].strip()
    if not name:
        flash("Subcategory name is required.", "error")
        return redirect(url_for("cafe.menu"))
    duplicate = MenuSubcategory.query.filter(
        db.func.lower(MenuSubcategory.name) == name.lower(), MenuSubcategory.id != subcategory.id
    ).first()
    if duplicate:
        flash("Subcategory with same name already exists.", "error")
        return redirect(url_for("cafe.menu"))
    subcategory.name = name
    db.session.commit()
    flash("Subcategory updated.", "success")
    return redirect(url_for("cafe.menu"))


@bp.route("/menu/subcategories/<int:subcategory_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_subcategory(subcategory_id):
    subcategory = MenuSubcategory.query.get_or_404(subcategory_id)
    if subcategory.items:
        flash("Subcategory cannot be deleted while linked menu items exist.", "error")
        return redirect(url_for("cafe.menu"))
    db.session.delete(subcategory)
    db.session.commit()
    flash("Subcategory deleted.", "success")
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
    menu_type = MenuType.query.get(int(request.form["menu_type_id"]))
    if not menu_type:
        flash("Please select a valid type.", "error")
        return redirect(url_for("cafe.menu"))
    item.category_id = int(request.form["category_id"])
    item.subcategory_id = int(request.form["subcategory_id"]) if request.form.get("subcategory_id") else None
    item.item_type = menu_type.name
    item.name = request.form["name"].strip()
    item.image_url = request.form.get("image_url", "").strip() or None
    item.description = request.form.get("description", "").strip() or None
    item.calories = int(request.form["calories"]) if request.form.get("calories") else None
    item.price = float(request.form["price"])
    item.prep_station = request.form.get("prep_station", "kitchen")
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
            status="open",
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
    subcategory_id = request.args.get("subcategory_id", type=int)
    item_type = (request.args.get("item_type") or "").strip()
    tables = CafeTable.query.filter_by(active=True).order_by(CafeTable.name).all()
    if not table_id and tables:
        table_id = tables[0].id

    menu_query = MenuItem.query.filter_by(available=True)
    if category_id:
        menu_query = menu_query.filter(MenuItem.category_id == category_id)
    if subcategory_id:
        menu_query = menu_query.filter(MenuItem.subcategory_id == subcategory_id)
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

    categories = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    subcategories = MenuSubcategory.query.order_by(MenuSubcategory.name.asc()).all()
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
        subcategories=subcategories,
        item_types=item_types,
        selected_category_id=category_id,
        selected_subcategory_id=subcategory_id,
        selected_item_type=item_type,
        item_frequency=item_frequency,
        selected_table_id=table_id,
        table_orders=CafeOrder.query.filter(
            CafeOrder.table_id == table_id, CafeOrder.status != "paid"
        )
        .order_by(CafeOrder.created_at.desc())
        .all()
        if table_id
        else [],
    )


@bp.route("/orders/<int:order_id>/mark-paid", methods=["POST"])
@roles_required("admin", "manager", "cashier")
def mark_order_paid(order_id):
    order = CafeOrder.query.get_or_404(order_id)
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
        status="open",
        payment_type=None,
        payment_reference=None,
    )
    if not order:
        flash("Please add at least one menu item in cart.", "error")
        return redirect(url_for("main.table_qr_page", slug=slug))
    flash("Order placed successfully.", "success")
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
@roles_required("admin", "manager", "barista", "inventory_manager")
def inventory():
    if request.method == "POST":
        item = InventoryItem(
            area=request.form["area"],
            name=request.form["name"].strip(),
            unit=request.form["unit"].strip(),
            current_amount=float(request.form["current_amount"]),
            required_amount=float(request.form["required_amount"]),
            reorder_level=float(request.form["reorder_level"]),
            note=request.form.get("note", "").strip() or None,
        )
        db.session.add(item)
        db.session.commit()
        flash("Inventory item added.", "success")
        return redirect(url_for("cafe.inventory"))
    return render_template(
        "cafe/inventory.html",
        items=InventoryItem.query.order_by(InventoryItem.area, InventoryItem.name).all(),
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
    if request.method == "POST":
        action = request.form.get("action", "create")

        if action == "create":
            email = request.form["email"].strip().lower()
            existing = User.query.filter_by(email=email).first()
            if existing:
                flash("A user with this email already exists.", "error")
                return redirect(url_for("cafe.staff"))
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
            return redirect(url_for("cafe.staff"))

        if action == "update":
            user = User.query.get_or_404(int(request.form["user_id"]))
            profile = _ensure_staff_profile(user)

            user.full_name = request.form["full_name"].strip()
            user.email = request.form["email"].strip().lower()
            duplicate = User.query.filter(User.email == user.email, User.id != user.id).first()
            if duplicate:
                flash("Another user already has this email.", "error")
                return redirect(url_for("cafe.staff"))
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
            return redirect(url_for("cafe.staff"))

        if action == "delete":
            user = User.query.get_or_404(int(request.form["user_id"]))
            if _is_protected_admin(user):
                flash("Cafe Admin cannot be deleted.", "error")
                return redirect(url_for("cafe.staff"))
            if user.staff_profile:
                db.session.delete(user.staff_profile)
            StaffAttendance.query.filter_by(user_id=user.id).delete()
            StaffLeaveRequest.query.filter_by(user_id=user.id).delete()
            db.session.delete(user)
            db.session.commit()
            flash("Staff member deleted.", "success")
            return redirect(url_for("cafe.staff"))

        if action == "archive":
            user = User.query.get_or_404(int(request.form["user_id"]))
            if user.role == "admin" and user.email == "admin@brownberries.local":
                flash("Cafe Admin cannot be archived.", "error")
                return redirect(url_for("cafe.staff"))
            profile = _ensure_staff_profile(user)
            profile.archived = True
            user.active = False
            db.session.commit()
            flash("Staff member archived.", "success")
            return redirect(url_for("cafe.staff"))

        if action == "leave_decision":
            leave = StaffLeaveRequest.query.get_or_404(int(request.form["leave_id"]))
            decision = request.form.get("decision", "pending")
            if decision not in ["approved", "rejected", "pending"]:
                decision = "pending"
            leave.status = decision
            leave.admin_remarks = request.form.get("admin_remarks", "").strip() or None
            db.session.commit()
            flash("Leave request updated.", "success")
            return redirect(url_for("cafe.staff"))

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
            return redirect(url_for("cafe.staff", attendance_user_id=target_user_id))

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
                "can_view_staff_profiles", "can_upload_salary",
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
                "can_view_staff_profiles", "can_upload_salary",
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
