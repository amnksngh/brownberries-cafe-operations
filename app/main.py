import json
import os
from datetime import date, datetime
from io import BytesIO
from uuid import uuid4

import qrcode
from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .auth_helpers import login_required, roles_required
from .extensions import socketio
from .extensions import db
from .deploy_config import load_deployment_config
from .models import (
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    LibraryLoan,
    LibraryPayment,
    MenuCategory,
    MenuItem,
    Customer,
    StaffAttendance,
    StaffDocument,
    StaffProfile,
    SalaryReceipt,
    TableBooking,
    UserType,
    User,
)

bp = Blueprint("main", __name__)
PROTECTED_ADMIN_EMAIL = "admin@brownberries.local"


def _is_cafe_admin(user: User) -> bool:
    return user.email == PROTECTED_ADMIN_EMAIL or (
        user.role == "admin" and user.full_name.strip().lower() == "cafe admin"
    )


def _query_arg_case_insensitive(name: str, default: str = "") -> str:
    for key, value in request.args.items():
        if key.lower() == name.lower():
            return value
    return default


def _apply_menu_category_filter(query, category_id: int | None):
    if not category_id:
        return query
    return query.filter(
        db.or_(
            MenuItem.category_id == category_id,
            MenuItem.category_ids_json.ilike(f"%{category_id}%"),
        )
    )


def _get_item_category_names(item: MenuItem, category_name_by_id: dict[int, str]) -> list[str]:
    names: list[str] = []
    names_seen: set[str] = set()
    if item.category_ids_json:
        try:
            raw = json.loads(item.category_ids_json)
            if isinstance(raw, list):
                for value in raw:
                    try:
                        cid = int(value)
                    except (TypeError, ValueError):
                        continue
                    cname = category_name_by_id.get(cid)
                    if cname and cname.lower() not in names_seen:
                        names.append(cname)
                        names_seen.add(cname.lower())
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if item.category and item.category.name:
        if item.category.name.lower() not in names_seen:
            names.append(item.category.name)
            names_seen.add(item.category.name.lower())
    return names


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


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email, active=True).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid credentials.", "error")
            return render_template("login.html")
        session.permanent = True
        session["user_id"] = user.id
        return redirect(url_for("main.dashboard"))
    return render_template("login.html")


@bp.route("/staff-login")
def staff_login_redirect():
    return redirect(url_for("main.login"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/")
def public_home():
    map_url = "https://maps.app.goo.gl/FXkm55Fo5wteGf9P9"
    review_url = "https://g.page/r/CZclLI_Be-puEAI/review"
    slots = [(h, f"{(h-1)%12+1}:00 {'AM' if h < 12 else 'PM'} - {h%12+1 if h%12+1 != 13 else 1}:00 {'AM' if h+1 < 12 else 'PM'}") for h in range(8, 22)]
    today = date.today()
    upcoming = (
        TableBooking.query.filter(TableBooking.booking_date >= today, TableBooking.status == "booked")
        .order_by(TableBooking.booking_date.asc(), TableBooking.start_hour.asc())
        .limit(20)
        .all()
    )
    cfg = load_deployment_config(current_app.instance_path)
    notice_text = (cfg.get("PUBLIC_NOTICE_TEXT", "") or "").strip()
    notice_enabled = cfg.get("PUBLIC_NOTICE_ENABLED", "0") in [1, "1", True, "true", "True"]
    return render_template(
        "public_home.html",
        map_url=map_url,
        review_url=review_url,
        slots=slots,
        upcoming_bookings=upcoming,
        public_notice_text=notice_text,
        public_notice_enabled=notice_enabled and bool(notice_text),
    )


@bp.route("/book-table", methods=["POST"])
def book_table():
    customer_name = request.form.get("customer_name", "").strip()
    phone = request.form.get("phone", "").strip()
    booking_date_raw = request.form.get("booking_date", "").strip()
    people_count = request.form.get("people_count", "").strip()
    people_count = int(people_count) if people_count.isdigit() else 2
    if people_count < 1:
        people_count = 1
    if people_count > 20:
        people_count = 20
    selected_slots = sorted({int(x) for x in request.form.getlist("slots") if str(x).isdigit()})
    if not customer_name or not phone or not booking_date_raw or not selected_slots:
        flash("Please fill all booking details and select at least one slot.", "error")
        return redirect(url_for("main.public_home"))
    booking_date = date.fromisoformat(booking_date_raw)
    if booking_date < date.today():
        flash("Booking date cannot be in the past.", "error")
        return redirect(url_for("main.public_home"))
    for idx in range(1, len(selected_slots)):
        if selected_slots[idx] != selected_slots[idx - 1] + 1:
            flash("Please select consecutive one-hour slots only.", "error")
            return redirect(url_for("main.public_home"))
    start_hour = selected_slots[0]
    end_hour = selected_slots[-1] + 1
    overlap = TableBooking.query.filter(
        TableBooking.booking_date == booking_date,
        TableBooking.status == "booked",
        TableBooking.start_hour < end_hour,
        TableBooking.end_hour > start_hour,
    ).count()
    if overlap >= 40:
        flash("Selected time is heavily booked. Please choose another slot.", "error")
        return redirect(url_for("main.public_home"))
    booking = TableBooking(
        customer_name=customer_name,
        phone=phone,
        people_count=people_count,
        booking_date=booking_date,
        start_hour=start_hour,
        end_hour=end_hour,
        note=request.form.get("note", "").strip() or None,
        status="booked",
    )
    db.session.add(booking)
    db.session.commit()
    socketio.emit(
        "booking_created",
        {
            "id": booking.id,
            "customer_name": customer_name,
            "phone": phone,
            "people_count": people_count,
            "booking_date": booking_date.isoformat(),
            "start_hour": start_hour,
            "end_hour": end_hour,
            "status": "booked",
        },
        namespace="/table",
    )
    socketio.emit(
        "booking_created",
        {
            "id": booking.id,
            "customer_name": customer_name,
            "phone": phone,
            "people_count": people_count,
            "booking_date": booking_date.isoformat(),
            "start_hour": start_hour,
            "end_hour": end_hour,
            "status": "booked",
        },
        namespace="/kitchen",
    )
    flash("Table booking request submitted successfully.", "success")
    return redirect(url_for("main.public_home"))


@bp.route("/dashboard")
@login_required
def dashboard():
    active_loans = LibraryLoan.query.filter_by(status="issued").count()
    due_tomorrow = LibraryLoan.query.filter(
        LibraryLoan.status == "issued",
        LibraryLoan.due_date == db.func.date(db.func.datetime("now", "+1 day")),
    ).count()
    open_orders = CafeOrder.query.filter_by(status="open").count()
    return render_template(
        "dashboard.html",
        open_orders=open_orders,
        active_loans=active_loans,
        due_tomorrow=due_tomorrow,
    )


def _customer_from_session():
    customer_id = session.get("customer_id")
    if not customer_id:
        return None
    return Customer.query.filter_by(id=customer_id, active=True).first()


def _customer_login_required():
    customer = _customer_from_session()
    if not customer:
        return None, redirect(url_for("main.customer_login", next=request.path))
    return customer, None


def _ensure_delivery_table() -> CafeTable:
    table = CafeTable.query.filter(db.func.lower(CafeTable.name) == "for delivery").first()
    if table:
        return table
    table = CafeTable(
        name="For Delivery",
        seating_capacity=0,
        metadata_note="Delivery orders",
        qr_slug=f"for-delivery-{uuid4().hex[:6]}",
        active=True,
    )
    db.session.add(table)
    db.session.commit()
    return table


def _get_or_create_delivery_guest_user_id() -> int:
    guest_email = "delivery.guest@brownberries.local"
    guest = User.query.filter_by(email=guest_email).first()
    if guest:
        return guest.id
    guest = User(
        full_name="Delivery Guest",
        email=guest_email,
        password_hash=generate_password_hash("delivery-guest"),
        role="delivery_partner",
        active=True,
    )
    db.session.add(guest)
    db.session.commit()
    return guest.id


@bp.route("/maps-qr.png")
def maps_qr():
    img = qrcode.make("https://maps.app.goo.gl/FXkm55Fo5wteGf9P9")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return Response(bio.read(), mimetype="image/png")


@bp.route("/review-qr.png")
def review_qr():
    img = qrcode.make("https://g.page/r/CZclLI_Be-puEAI/review")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return Response(bio.read(), mimetype="image/png")


@bp.route("/customer/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        customer = Customer.query.filter_by(mobile=mobile, active=True).first()
        if not customer or not check_password_hash(customer.password_hash, password):
            flash("Invalid customer credentials.", "error")
            return render_template("customer_login.html")
        session["customer_id"] = customer.id
        next_url = request.args.get("next", "").strip()
        return redirect(next_url or url_for("main.customer_menu"))
    return render_template("customer_login.html")


@bp.route("/customer/logout")
def customer_logout():
    session.pop("customer_id", None)
    return redirect(url_for("main.public_home"))


@bp.route("/customer/register", methods=["GET", "POST"])
def customer_register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        default_address = request.form.get("default_address", "").strip()
        try:
            default_lat = float(request.form.get("default_lat")) if request.form.get("default_lat") else None
            default_lng = float(request.form.get("default_lng")) if request.form.get("default_lng") else None
        except ValueError:
            default_lat = None
            default_lng = None
        if not full_name or not mobile or len(password) < 6:
            flash("Please fill all required fields. Password must be at least 6 characters.", "error")
            return render_template("customer_register.html")
        if Customer.query.filter_by(mobile=mobile).first():
            flash("Mobile number already registered.", "error")
            return render_template("customer_register.html")
        customer = Customer(
            full_name=full_name,
            mobile=mobile,
            password_hash=generate_password_hash(password),
            default_address=default_address or None,
            default_lat=default_lat,
            default_lng=default_lng,
            default_map_url=request.form.get("default_map_url", "").strip() or None,
            active=True,
        )
        db.session.add(customer)
        db.session.commit()
        flash("Registration complete. Please login.", "success")
        return redirect(url_for("main.customer_login"))
    return render_template("customer_register.html")


@bp.route("/customer/menu", methods=["GET", "POST"])
def customer_menu():
    customer, redirect_resp = _customer_login_required()
    if redirect_resp:
        return redirect_resp

    if request.method == "POST":
        from .cafe import create_cafe_order

        cart_payload = (request.form.get("cart_payload") or "").strip()
        use_default = request.form.get("use_default_address") == "1"
        delivery_address = request.form.get("delivery_address", "").strip()
        if use_default and customer.default_address:
            delivery_address = customer.default_address
            delivery_lat = customer.default_lat
            delivery_lng = customer.default_lng
            delivery_map_url = customer.default_map_url
        else:
            try:
                delivery_lat = float(request.form.get("delivery_lat")) if request.form.get("delivery_lat") else None
                delivery_lng = float(request.form.get("delivery_lng")) if request.form.get("delivery_lng") else None
            except ValueError:
                delivery_lat = None
                delivery_lng = None
            delivery_map_url = request.form.get("delivery_map_url", "").strip() or None

        line_items = []
        try:
            raw = json.loads(cart_payload) if cart_payload else []
            qty_map = {}
            for row in raw if isinstance(raw, list) else []:
                if not isinstance(row, dict):
                    continue
                menu_item_id = int(row.get("menu_item_id") or 0)
                quantity = int(row.get("quantity") or 0)
                if menu_item_id > 0 and quantity > 0:
                    qty_map[menu_item_id] = qty_map.get(menu_item_id, 0) + quantity
            item_ids = list(qty_map.keys())
            menu_items = MenuItem.query.filter(MenuItem.id.in_(item_ids), MenuItem.available.is_(True)).all() if item_ids else []
            item_map = {m.id: m for m in menu_items}
            for menu_item_id, quantity in qty_map.items():
                if menu_item_id in item_map:
                    line_items.append((item_map[menu_item_id], quantity, True))
        except (ValueError, TypeError, json.JSONDecodeError):
            line_items = []

        if not line_items:
            flash("Please add at least one menu item in cart.", "error")
            return redirect(url_for("main.customer_menu"))
        if not delivery_address:
            flash("Please provide delivery address.", "error")
            return redirect(url_for("main.customer_menu"))

        delivery_table = _ensure_delivery_table()
        create_cafe_order(
            table_id=delivery_table.id,
            ordered_by_user_id=_get_or_create_delivery_guest_user_id(),
            line_items=line_items,
            status="open",
            is_delivery=True,
            delivery_customer_name=customer.full_name,
            delivery_customer_mobile=customer.mobile,
            delivery_address=delivery_address,
            delivery_lat=delivery_lat,
            delivery_lng=delivery_lng,
            delivery_map_url=delivery_map_url,
        )
        flash("Delivery order placed successfully.", "success")
        return redirect(url_for("main.customer_menu"))

    category_id = request.args.get("category_id", type=int)
    item_type = (request.args.get("item_type") or "").strip()
    menu_query = _apply_menu_category_filter(MenuItem.query.filter_by(available=True), category_id)
    if item_type:
        menu_query = menu_query.filter(MenuItem.item_type == item_type)
    menu_items = (
        menu_query.options(joinedload(MenuItem.category), joinedload(MenuItem.subcategory))
        .order_by(MenuItem.name.asc())
        .all()
    )
    categories = _visible_categories_for_available_menu()
    category_name_by_id = {c.id: c.name for c in categories}
    item_category_names_map = {item.id: _get_item_category_names(item, category_name_by_id) for item in menu_items}
    item_types = [
        row[0]
        for row in db.session.query(MenuItem.item_type)
        .filter(MenuItem.available.is_(True))
        .distinct()
        .order_by(MenuItem.item_type.asc())
        .all()
    ]
    return render_template(
        "customer_menu.html",
        customer=customer,
        menu_items=menu_items,
        categories=categories,
        item_category_names_map=item_category_names_map,
        item_types=item_types,
        selected_category_id=category_id,
        selected_item_type=item_type,
    )


def _ensure_profile(user: User):
    profile = user.staff_profile
    if not profile:
        profile = StaffProfile(user_id=user.id, archived=not user.active)
        db.session.add(profile)
        db.session.commit()
    return profile


def _save_profile_file(file_obj, subdir: str, prefix: str):
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


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    current_user = g.current_user
    target_user_id = request.args.get("user_id", type=int)
    can_admin_view = current_user.role in ["admin", "manager"] or (
        current_user.user_type and current_user.user_type.can_view_staff_profiles
    )
    if target_user_id and can_admin_view:
        user = User.query.get_or_404(target_user_id)
    else:
        user = current_user
    profile = _ensure_profile(user)
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "personal":
            if current_user.id != user.id and not can_admin_view:
                return redirect(url_for("main.profile"))
            user.full_name = request.form.get("full_name", user.full_name).strip() or user.full_name
            profile.dob = date.fromisoformat(request.form["dob"]) if request.form.get("dob") else None
            profile.gender = request.form.get("gender", "").strip() or None
            profile.marital_status = request.form.get("marital_status", "").strip() or None
            profile.phone = request.form.get("phone", "").strip() or None
            profile.alternate_contact = request.form.get("alternate_contact", "").strip() or None
            profile.address = request.form.get("address", "").strip() or None
            db.session.commit()
            flash("Personal information updated.", "success")
            return redirect(url_for("main.profile", section="personal", user_id=user.id if can_admin_view else None))

        if action == "bank":
            if current_user.id != user.id and not can_admin_view:
                return redirect(url_for("main.profile"))
            profile.bank_account_name = request.form.get("bank_account_name", "").strip() or None
            profile.bank_account_number = request.form.get("bank_account_number", "").strip() or None
            profile.bank_ifsc = request.form.get("bank_ifsc", "").strip() or None
            profile.bank_name = request.form.get("bank_name", "").strip() or None
            db.session.commit()
            flash("Bank details updated.", "success")
            return redirect(url_for("main.profile", section="bank", user_id=user.id if can_admin_view else None))

        if action == "password":
            if current_user.id != user.id:
                flash("Password can only be changed by the logged-in user.", "error")
                return redirect(url_for("main.profile", section="personal", user_id=user.id if can_admin_view else None))
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not check_password_hash(user.password_hash, current_password):
                flash("Current password is incorrect.", "error")
                return redirect(url_for("main.profile", section="personal"))
            if len(new_password) < 6:
                flash("New password should be at least 6 characters.", "error")
                return redirect(url_for("main.profile", section="personal"))
            if new_password != confirm_password:
                flash("Password confirmation does not match.", "error")
                return redirect(url_for("main.profile", section="personal"))
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("main.profile", section="personal"))

        if action == "attendance_status":
            if current_user.id != user.id and not can_admin_view:
                return redirect(url_for("main.profile"))
            selected_date = date.fromisoformat(request.form["attendance_date"]) if request.form.get("attendance_date") else date.today()
            status = request.form.get("status", "present_all_day")
            notes = request.form.get("notes", "").strip() or None
            row = StaffAttendance.query.filter_by(user_id=user.id, attendance_date=selected_date).first()
            if not row:
                row = StaffAttendance(user_id=user.id, attendance_date=selected_date)
                db.session.add(row)
            row.status = status
            row.notes = notes
            db.session.commit()
            flash("Attendance status saved.", "success")
            return redirect(url_for("main.profile", section="attendance", user_id=user.id if can_admin_view else None))

        if action == "check_in":
            if current_user.id != user.id:
                return redirect(url_for("main.profile"))
            today = date.today()
            row = StaffAttendance.query.filter_by(user_id=user.id, attendance_date=today).first()
            if not row:
                row = StaffAttendance(user_id=user.id, attendance_date=today, status="present_all_day")
                db.session.add(row)
            row.check_in_at = datetime.now()
            db.session.commit()
            flash("Check-in recorded.", "success")
            return redirect(url_for("main.profile", section="attendance"))

        if action == "check_out":
            if current_user.id != user.id:
                return redirect(url_for("main.profile"))
            today = date.today()
            row = StaffAttendance.query.filter_by(user_id=user.id, attendance_date=today).first()
            if not row:
                flash("Please check-in first.", "error")
                return redirect(url_for("main.profile", section="attendance"))
            row.check_out_at = datetime.now()
            if not row.status:
                row.status = "present_all_day"
            db.session.commit()
            flash("Check-out recorded.", "success")
            return redirect(url_for("main.profile", section="attendance"))

        if action == "upload_document":
            if current_user.id != user.id and not can_admin_view:
                return redirect(url_for("main.profile"))
            doc_file = request.files.get("doc_file")
            if not doc_file or not doc_file.filename:
                flash("Please choose a document to upload.", "error")
                return redirect(url_for("main.profile", section="documents", user_id=user.id if can_admin_view else None))
            file_path = _save_profile_file(doc_file, "staff_docs", f"profile-doc-{user.id}")
            db.session.add(
                StaffDocument(
                    user_id=user.id,
                    uploaded_by_user_id=current_user.id,
                    doc_type=request.form.get("doc_type", "Other").strip() or "Other",
                    doc_number=request.form.get("doc_number", "").strip() or None,
                    file_path=file_path,
                    released_by_admin=True if current_user.role in ["admin", "manager"] else False,
                )
            )
            db.session.commit()
            flash("Document uploaded.", "success")
            return redirect(url_for("main.profile", section="documents", user_id=user.id if can_admin_view else None))

        if action == "release_document":
            if not can_admin_view:
                return redirect(url_for("main.profile"))
            doc = StaffDocument.query.get_or_404(int(request.form["doc_id"]))
            doc.released_by_admin = True if request.form.get("released_by_admin") == "1" else False
            db.session.commit()
            flash("Document release status updated.", "success")
            return redirect(url_for("main.profile", section="documents", user_id=user.id))

        if action == "upload_salary_receipt":
            if not (current_user.role in ["admin", "manager"] or (current_user.user_type and current_user.user_type.can_upload_salary)):
                return redirect(url_for("main.profile"))
            receipt_file = request.files.get("receipt_file")
            target_id = int(request.form["target_user_id"])
            target_user = User.query.get_or_404(target_id)
            if not receipt_file or not receipt_file.filename:
                flash("Please select salary receipt file.", "error")
                return redirect(url_for("main.profile", section="receipts", user_id=target_user.id))
            file_path = _save_profile_file(receipt_file, "salary_receipts", f"salary-{target_user.id}")
            db.session.add(
                SalaryReceipt(
                    user_id=target_user.id,
                    uploaded_by_user_id=current_user.id,
                    salary_month=int(request.form["salary_month"]),
                    salary_year=int(request.form["salary_year"]),
                    amount=float(request.form["amount"]) if request.form.get("amount") else None,
                    file_path=file_path,
                    note=request.form.get("note", "").strip() or None,
                )
            )
            db.session.commit()
            flash("Salary receipt uploaded.", "success")
            return redirect(url_for("main.profile", section="receipts", user_id=target_user.id))

    attendance_logs = (
        StaffAttendance.query.filter_by(user_id=user.id)
        .order_by(StaffAttendance.attendance_date.desc())
        .limit(60)
        .all()
    )
    receipts = (
        CafeOrder.query.filter_by(ordered_by_user_id=user.id, status="paid")
        .order_by(CafeOrder.created_at.desc())
        .limit(40)
        .all()
    )
    library_payments = (
        LibraryPayment.query.order_by(LibraryPayment.created_at.desc()).limit(20).all()
        if user.role in ["admin", "manager", "librarian"]
        else []
    )
    salary_receipts = (
        SalaryReceipt.query.filter_by(user_id=user.id)
        .order_by(SalaryReceipt.salary_year.desc(), SalaryReceipt.salary_month.desc(), SalaryReceipt.created_at.desc())
        .all()
    )
    documents_uploaded_by_user = (
        StaffDocument.query.filter_by(user_id=user.id, uploaded_by_user_id=user.id)
        .order_by(StaffDocument.created_at.desc())
        .all()
    )
    documents_released = (
        StaffDocument.query.filter_by(user_id=user.id, released_by_admin=True)
        .order_by(StaffDocument.created_at.desc())
        .all()
    )
    documents_for_admin = (
        StaffDocument.query.filter_by(user_id=user.id)
        .order_by(StaffDocument.created_at.desc())
        .all()
        if can_admin_view
        else []
    )
    managed_users = User.query.filter_by(active=True).order_by(User.full_name.asc()).all() if can_admin_view else []
    active_section = (request.args.get("section") or "personal").strip().lower()
    if active_section not in ["personal", "bank", "documents", "salary", "attendance"]:
        active_section = "personal"
    return render_template(
        "profile.html",
        profile_user=user,
        profile=profile,
        receipts=receipts,
        library_payments=library_payments,
        salary_receipts=salary_receipts,
        documents_uploaded_by_user=documents_uploaded_by_user,
        documents_released=documents_released,
        documents_for_admin=documents_for_admin,
        can_admin_view=can_admin_view,
        managed_users=managed_users,
        attendance_logs=attendance_logs,
        active_section=active_section,
    )


@bp.route("/profile/documents/<string:doc_type>")
@login_required
def profile_document(doc_type):
    current_user = g.current_user
    doc_id = request.args.get("doc_id", type=int)
    receipt_id = request.args.get("receipt_id", type=int)
    can_admin_view = current_user.role in ["admin", "manager"] or (
        current_user.user_type and current_user.user_type.can_view_staff_profiles
    )

    rel_path = None
    if doc_type == "staff_doc" and doc_id:
        doc = StaffDocument.query.get_or_404(doc_id)
        if doc.user_id != current_user.id and not can_admin_view:
            return redirect(url_for("main.profile", section="documents"))
        if not doc.released_by_admin and doc.user_id == current_user.id and not can_admin_view:
            flash("Document is not released yet by admin/manager.", "error")
            return redirect(url_for("main.profile", section="documents"))
        rel_path = doc.file_path
    elif doc_type == "salary_receipt" and receipt_id:
        receipt = SalaryReceipt.query.get_or_404(receipt_id)
        if receipt.user_id != current_user.id and not can_admin_view:
            return redirect(url_for("main.profile", section="salary"))
        rel_path = receipt.file_path
    elif doc_type in ["govt_id", "photo"]:
        profile = _ensure_profile(current_user)
        rel_path = profile.govt_id_file_path if doc_type == "govt_id" else profile.photo_file_path
    else:
        return redirect(url_for("main.profile", section="documents"))

    if not rel_path:
        flash("Document not available.", "error")
        return redirect(url_for("main.profile", section="documents"))
    full_path = os.path.join(current_app.config["UPLOADS_ROOT"], rel_path)
    if not os.path.exists(full_path):
        flash("Document file missing.", "error")
        return redirect(url_for("main.profile", section="documents"))
    with open(full_path, "rb") as f:
        data = f.read()
    return Response(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="{os.path.basename(full_path)}"',
            "Content-Type": "application/octet-stream",
        },
    )


@bp.route("/table")
def table_qr_page():
    slug = (_query_arg_case_insensitive("slug") or "").strip()
    is_preview = (request.args.get("preview") or "").strip() in ["1", "true", "yes"]
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first() if slug else None
    if not table:
        table = CafeTable.query.filter_by(active=True).order_by(CafeTable.id.asc()).first()
    if not table:
        return render_template("table_qr.html", table=None, menu_items=[], categories=[], item_frequency={})
    categories = _visible_categories_for_available_menu()
    category_name_by_id = {c.id: c.name for c in categories}
    frequency_subq = (
        db.session.query(
            CafeOrderItem.menu_item_id.label("menu_item_id"),
            db.func.coalesce(db.func.sum(CafeOrderItem.quantity), 0).label("order_qty"),
        )
        .group_by(CafeOrderItem.menu_item_id)
        .subquery()
    )
    ranked_query = (
        MenuItem.query.filter_by(available=True).outerjoin(frequency_subq, MenuItem.id == frequency_subq.c.menu_item_id)
        .options(joinedload(MenuItem.category), joinedload(MenuItem.subcategory))
        .add_columns(db.func.coalesce(frequency_subq.c.order_qty, 0).label("order_qty"))
        .order_by(db.desc(db.func.coalesce(frequency_subq.c.order_qty, 0)), MenuItem.name.asc())
    )
    ranked_rows = ranked_query.all()
    menu_items = [row[0] for row in ranked_rows]
    item_frequency = {row[0].id: int(row[1] or 0) for row in ranked_rows}
    item_size_map = {}
    item_category_names_map = {}
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
        item_category_names_map[item.id] = _get_item_category_names(item, category_name_by_id)

    table_orders = []
    if table:
        table_orders = (
            CafeOrder.query.options(joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item))
            .filter(CafeOrder.table_id == table.id, CafeOrder.status.notin_(["paid", "cancelled"]))
            .order_by(CafeOrder.created_at.desc())
            .all()
        )
    return render_template(
        "table_qr.html",
        table=table,
        is_preview=is_preview,
        menu_items=menu_items,
        table_orders=table_orders,
        categories=categories,
        item_frequency=item_frequency,
        item_size_map=item_size_map,
        item_category_names_map=item_category_names_map,
    )


@bp.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    return redirect(url_for("cafe.staff"))


@bp.route("/users/<int:user_id>/update", methods=["POST"])
@roles_required("admin")
def update_user(user_id):
    return redirect(url_for("cafe.staff"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_user(user_id):
    return redirect(url_for("cafe.staff"))
