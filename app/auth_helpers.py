from functools import wraps

from flask import g, redirect, request, session, url_for

from .models import User

ENDPOINT_PERMISSIONS = {
    "cafe.home": "can_access_cafe",
    "cafe.staff": "can_manage_staff",
    "cafe.user_types": "can_manage_staff",
    "cafe.menu": "can_manage_menu",
    "cafe.add_category": "can_manage_menu",
    "cafe.add_type": "can_manage_menu",
    "cafe.update_category": "can_manage_menu",
    "cafe.update_type": "can_manage_menu",
    "cafe.delete_category": "can_manage_menu",
    "cafe.delete_type": "can_manage_menu",
    "cafe.update_menu_item": "can_manage_menu",
    "cafe.delete_menu_item": "can_manage_menu",
    "cafe.update_menu_availability": "can_manage_menu",
    "cafe.orders": "can_manage_orders",
    "cafe.kitchen_display": "can_manage_kitchen",
    "cafe.barista_display": "can_manage_kitchen",
    "cafe.inventory": "can_manage_inventory",
    "cafe.cashier": "can_manage_cashier",
    "cafe.mark_order_paid": "can_manage_cashier",
    "cafe.clear_table_orders": "can_manage_cashier",
    "cafe.approve_order": "can_manage_orders",
    "cafe.bookings": "can_manage_cashier",
    "cafe.delivery_locations": "can_view_delivery_locations",
    "cafe.stats": "can_manage_stats",
    "cafe.export_stats": "can_manage_stats",
    "library.home": "can_access_library",
    "library.members": "can_manage_library_members",
    "library.member_documents": "can_manage_library_members",
    "library.authors": "can_manage_library_books",
    "library.books": "can_manage_library_books",
    "library.loans": "can_manage_library_loans",
    "library.update_due_date": "can_manage_library_loans",
    "library.reissue": "can_manage_library_loans",
    "library.return_book": "can_manage_library_loans",
    "library.payments": "can_manage_library_payments",
    "library.plans": "can_manage_library_plans",
}


def load_current_user():
    user_id = session.get("user_id")
    g.current_user = User.query.get(user_id) if user_id else None
    if g.current_user and not g.current_user.active:
        session.clear()
        g.current_user = None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.current_user:
                return redirect(url_for("main.login", next=request.path))
            if not g.current_user.active:
                session.clear()
                return redirect(url_for("main.login", next=request.path))
            if g.current_user.role in roles:
                return view(*args, **kwargs)
            user_type = getattr(g.current_user, "user_type", None)
            perm_name = ENDPOINT_PERMISSIONS.get(request.endpoint or "")
            if user_type and perm_name and getattr(user_type, perm_name, False):
                return view(*args, **kwargs)
            return redirect(url_for("main.dashboard"))

        return wrapped

    return decorator
