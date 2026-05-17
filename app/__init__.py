import os
from pathlib import Path
from datetime import timedelta

from flask import Flask
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from .auth_helpers import load_current_user
from .cafe import bp as cafe_bp
from .deploy_config import load_deployment_config
from .extensions import db, socketio
from .library import bp as library_bp
from .main import bp as main_bp
from .models import SubscriptionPlan, User


def _ensure_sqlite_schema_columns():
    # Lightweight forward-compatible schema patching for SQLite deployments without migrations.
    column_specs = {
        "menu_item": {
            "prep_station": "TEXT NOT NULL DEFAULT 'kitchen'",
            "category_ids_json": "TEXT",
            "has_size_variants": "BOOLEAN NOT NULL DEFAULT 0",
            "size_pricing_json": "TEXT",
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
            "govt_id_image_path": "TEXT",
        },
        "book": {
            "genre": "TEXT",
        },
        "staff_attendance": {
            "check_in_at": "DATETIME",
            "check_out_at": "DATETIME",
        },
        "user": {
            "user_type_id": "INTEGER",
        },
        "user_type": {
            "can_view_delivery_locations": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "cafe_order": {
            "is_delivery": "BOOLEAN NOT NULL DEFAULT 0",
            "delivery_customer_name": "TEXT",
            "delivery_customer_mobile": "TEXT",
            "delivery_address": "TEXT",
            "delivery_lat": "FLOAT",
            "delivery_lng": "FLOAT",
            "delivery_map_url": "TEXT",
            "packaging_charge": "FLOAT NOT NULL DEFAULT 0",
            "delivery_distance_km": "FLOAT NOT NULL DEFAULT 0",
            "delivery_charge": "FLOAT NOT NULL DEFAULT 0",
            "daily_sequence": "INTEGER",
            "display_code": "TEXT",
        },
        "customer": {
            "default_map_url": "TEXT",
        },
        "table_booking": {
            "people_count": "INTEGER NOT NULL DEFAULT 2",
        },
        "library_loan": {
            "due_reminder_sent_on": "DATE",
        },
        "cafe_order_item": {
            "size_label": "TEXT",
            "approval_status": "TEXT NOT NULL DEFAULT 'pending'",
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

    # Performance indexes for high-frequency queries.
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_table_status_created ON cafe_order (table_id, status, created_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_status_created ON cafe_order (status, created_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_item_order ON cafe_order_item (order_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_item_menu ON cafe_order_item (menu_item_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_menu_item_available_cat_sub_type ON menu_item (available, category_id, subcategory_id, item_type)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_staff_attendance_user_date ON staff_attendance (user_id, attendance_date)")
    )
    db.session.commit()


def create_app():
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )
    deploy_cfg = load_deployment_config(app.instance_path)
    app.config.update(
        SECRET_KEY="change-this-in-production",
        SQLALCHEMY_DATABASE_URI="sqlite:///brownberries.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_REFRESH_EACH_REQUEST=True,
        PUBLIC_BASE_URL=(
            deploy_cfg.get("PUBLIC_BASE_URL", "").strip()
            or "https://brownberriescafe.com"
        ),
    )
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    uploads_root = Path(app.instance_path) / "uploads"
    (uploads_root / "staff_ids").mkdir(parents=True, exist_ok=True)
    (uploads_root / "staff_docs").mkdir(parents=True, exist_ok=True)
    (uploads_root / "staff_photos").mkdir(parents=True, exist_ok=True)
    (uploads_root / "library_cards").mkdir(parents=True, exist_ok=True)
    (uploads_root / "library_docs").mkdir(parents=True, exist_ok=True)
    (uploads_root / "salary_receipts").mkdir(parents=True, exist_ok=True)
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


@socketio.on("connect", namespace="/table")
def _table_namespace_connect():
    return True


@socketio.on("connect", namespace="/kitchen")
def _kitchen_namespace_connect():
    return True
