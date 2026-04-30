from functools import wraps

from flask import g, redirect, request, session, url_for

from .models import User


def load_current_user():
    user_id = session.get("user_id")
    g.current_user = User.query.get(user_id) if user_id else None


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
            if g.current_user.role not in roles:
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator
