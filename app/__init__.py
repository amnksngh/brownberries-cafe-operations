import os
from pathlib import Path

from flask import Flask
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from .auth_helpers import load_current_user
from .cafe import bp as cafe_bp
from .extensions import db, socketio
from .library import bp as library_bp
from .main import bp as main_bp
from .models import SubscriptionPlan, User


def _ensure_sqlite_schema_columns():
    # Lightweight forward-compatible schema patching for SQLite deployments without migrations.
    column_specs = {
        "menu_item": {
            "prep_station": "TEXT NOT NULL DEFAULT 'kitchen'",
        },
        "staff_profile": {
            "dob": "DATE",
            "marital_status": "TEXT",
            "gender": "TEXT",
            "govt_id_type": "TEXT",
            "govt_id_number": "TEXT",
            "govt_id_file_path": "TEXT",
            "photo_file_path": "TEXT",
        },
        "library_member": {
            "member_code": "TEXT",
        },
        "book": {
            "genre": "TEXT",
        },
    }
    for table_name, cols in column_specs.items():
        existing = {
            row[1]
            for row in db.session.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        }
        for col, spec in cols.items():
            if col not in existing:
                db.session.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {col} {spec}")
                )
    db.session.commit()


def create_app():
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.update(
        SECRET_KEY="change-this-in-production",
        SQLALCHEMY_DATABASE_URI="sqlite:///brownberries.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL", "").strip()
        or "https://transactions-computers-blvd-directions.trycloudflare.com",
    )
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    uploads_root = Path(app.instance_path) / "uploads"
    (uploads_root / "staff_ids").mkdir(parents=True, exist_ok=True)
    (uploads_root / "staff_docs").mkdir(parents=True, exist_ok=True)
    (uploads_root / "staff_photos").mkdir(parents=True, exist_ok=True)
    (uploads_root / "library_cards").mkdir(parents=True, exist_ok=True)
    app.config["UPLOADS_ROOT"] = str(uploads_root)

    db.init_app(app)
    socketio.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_sqlite_schema_columns()
    app.before_request(load_current_user)
    app.register_blueprint(main_bp)
    app.register_blueprint(cafe_bp)
    app.register_blueprint(library_bp)

    @app.cli.command("init-db")
    def init_db():
        db.create_all()
        if not User.query.filter_by(email="admin@brownberries.local").first():
            db.session.add(
                User(
                    full_name="Cafe Admin",
                    email="admin@brownberries.local",
                    password_hash=generate_password_hash("admin123"),
                    role="admin",
                    active=True,
                )
            )
        defaults = [
            ("Monthly", 30, 10, 5),
            ("Quarterly", 90, 10, 5),
            ("Yearly", 365, 10, 5),
        ]
        for name, days, weekly, late in defaults:
            if not SubscriptionPlan.query.filter_by(name=name).first():
                db.session.add(
                    SubscriptionPlan(
                        name=name,
                        duration_days=days,
                        weekly_reissue_fee_per_book=weekly,
                        late_fee_per_day=late,
                        active=True,
                    )
                )
        db.session.commit()
        print("Database initialized. Default admin: admin@brownberries.local / admin123")

    return app
