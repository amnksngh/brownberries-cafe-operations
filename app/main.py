import os
from datetime import date, datetime
from uuid import uuid4

from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .auth_helpers import login_required, roles_required
from .extensions import db
from .models import (
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    LibraryLoan,
    LibraryPayment,
    MenuCategory,
    MenuItem,
    MenuSubcategory,
    StaffAttendance,
    StaffDocument,
    StaffProfile,
    SalaryReceipt,
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


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email, active=True).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid credentials.", "error")
            return render_template("login.html")
        session["user_id"] = user.id
        return redirect(url_for("main.dashboard"))
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/")
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
    if not slug:
        return render_template("table_qr.html", table=None, menu_items=[])
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    category_id = request.args.get("category_id", type=int)
    subcategory_id = request.args.get("subcategory_id", type=int)
    item_type = (request.args.get("item_type") or "").strip()
    page = max(1, request.args.get("page", type=int) or 1)
    page_size = 10

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

    menu_query = MenuItem.query.filter_by(available=True)
    if category_id:
        menu_query = menu_query.filter(MenuItem.category_id == category_id)
    if subcategory_id:
        menu_query = menu_query.filter(MenuItem.subcategory_id == subcategory_id)
    if item_type:
        menu_query = menu_query.filter(MenuItem.item_type == item_type)

    items = menu_query.all()
    frequency_rows = (
        db.session.query(
            CafeOrderItem.menu_item_id,
            db.func.coalesce(db.func.sum(CafeOrderItem.quantity), 0).label("order_qty"),
        )
        .group_by(CafeOrderItem.menu_item_id)
        .all()
    )
    item_frequency = {row.menu_item_id: int(row.order_qty) for row in frequency_rows}

    # Default behavior: if no filter chosen, show top bought items first.
    menu_items_sorted = sorted(
        items,
        key=lambda item: (-item_frequency.get(item.id, 0), item.name.lower()),
    )

    total_items = len(menu_items_sorted)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    end = start + page_size
    menu_items = menu_items_sorted[start:end]

    table_orders = []
    if table:
        table_orders = (
            CafeOrder.query.filter(CafeOrder.table_id == table.id, CafeOrder.status != "paid")
            .order_by(CafeOrder.created_at.desc())
            .all()
        )
    return render_template(
        "table_qr.html",
        table=table,
        menu_items=menu_items,
        table_orders=table_orders,
        categories=categories,
        subcategories=subcategories,
        item_types=item_types,
        selected_category_id=category_id,
        selected_subcategory_id=subcategory_id,
        selected_item_type=item_type,
        page=page,
        total_pages=total_pages,
        item_frequency=item_frequency,
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
