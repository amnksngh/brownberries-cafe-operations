from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .auth_helpers import login_required, roles_required
from .extensions import db
from .models import (
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    LibraryLoan,
    MenuCategory,
    MenuItem,
    MenuSubcategory,
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
