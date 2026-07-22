import os
from pathlib import Path
from datetime import timedelta

from flask import Flask
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from .auth_helpers import (
    load_current_user,
    user_display_roles,
    user_has_any_role,
    user_has_permission,
    user_primary_role,
)
from .cafe import (
    _backfill_order_codes,
    _backfill_paid_timestamps,
    _ensure_protected_menu_categories,
    bp as cafe_bp,
)
from .deploy_config import load_deployment_config
from .extensions import db, socketio
from .library import bp as library_bp
from .main import bp as main_bp
from .mobile_attendance import bp as mobile_attendance_bp
from .models import (
    CafeFeedback,
    CafeFeedbackItem,
    InventoryCategory,
    InventoryExpenseLog,
    InventoryItem,
    InventoryVendor,
    StaffMobileSession,
    SubscriptionPlan,
    User,
    Workstation,
)


def _ensure_sqlite_schema_columns():
    # Lightweight forward-compatible schema patching for SQLite deployments without migrations.
    column_specs = {
        "menu_item": {
            "prep_station": "TEXT NOT NULL DEFAULT 'kitchen'",
            "category_ids_json": "TEXT",
            "has_size_variants": "BOOLEAN NOT NULL DEFAULT 0",
            "size_pricing_json": "TEXT",
            "short_description": "TEXT",
            "is_deleted": "BOOLEAN NOT NULL DEFAULT 0",
            "chef_user_id": "INTEGER",
        },
        "staff_profile": {
            "joining_date": "DATE",
            "dob": "DATE",
            "marital_status": "TEXT",
            "gender": "TEXT",
            "phone": "TEXT",
            "alternate_contact": "TEXT",
            "emergency_contact": "TEXT",
            "address": "TEXT",
            "salary_type": "TEXT",
            "salary_amount": "FLOAT",
            "probation_end_date": "DATE",
            "pan_number": "TEXT",
            "bank_account_name": "TEXT",
            "bank_account_number": "TEXT",
            "bank_ifsc": "TEXT",
            "bank_name": "TEXT",
            "govt_id_type": "TEXT",
            "govt_id_number": "TEXT",
            "govt_id_file_path": "TEXT",
            "photo_file_path": "TEXT",
            "shift_start_time": "TIME",
            "shift_end_time": "TIME",
            "archived": "BOOLEAN NOT NULL DEFAULT 0",
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
            "manager_override": "BOOLEAN NOT NULL DEFAULT 0",
            "check_in_lat": "FLOAT",
            "check_in_lng": "FLOAT",
            "check_in_distance_m": "FLOAT",
            "check_in_method": "TEXT",
            "check_out_lat": "FLOAT",
            "check_out_lng": "FLOAT",
            "check_out_distance_m": "FLOAT",
            "check_out_method": "TEXT",
            "last_heartbeat_at": "DATETIME",
            "last_heartbeat_lat": "FLOAT",
            "last_heartbeat_lng": "FLOAT",
            "last_heartbeat_distance_m": "FLOAT",
            "mobile_device_id": "TEXT",
            "auto_checkout_reason": "TEXT",
            "notes": "TEXT",
        },
        "user": {
            "user_type_id": "INTEGER",
            "roles_json": "TEXT",
        },
        "user_type": {
            "can_view_delivery_locations": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "staff_document": {
            "verification_status": "TEXT NOT NULL DEFAULT 'pending'",
            "verification_note": "TEXT",
        },
        "cafe_table": {
            "last_staff_call_at": "DATETIME",
            "service_charge_opt_out_requested": "BOOLEAN NOT NULL DEFAULT 0",
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
            "service_tax_amount": "FLOAT NOT NULL DEFAULT 0",
            "gst_amount": "FLOAT NOT NULL DEFAULT 0",
            "cst_amount": "FLOAT NOT NULL DEFAULT 0",
            "daily_sequence": "INTEGER",
            "display_code": "TEXT",
            "paid_at": "DATETIME",
            "payment_breakdown_json": "TEXT",
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
            "is_parcel": "BOOLEAN NOT NULL DEFAULT 0",
            "prep_status": "TEXT NOT NULL DEFAULT 'pending'",
        },
        "cafe_feedback": {
            "order_ids_json": "TEXT",
            "source": "TEXT NOT NULL DEFAULT 'online'",
            "service_rating": "INTEGER NOT NULL DEFAULT 3",
            "summary_text": "TEXT",
            "submitted_by_user_id": "INTEGER",
            "submitted_by_name": "TEXT",
            "submitted_at": "DATETIME",
        },
        "cafe_feedback_item": {
            "menu_item_id": "INTEGER",
            "order_item_id": "INTEGER",
            "item_name": "TEXT",
            "size_label": "TEXT",
            "is_parcel": "BOOLEAN NOT NULL DEFAULT 0",
            "rating": "INTEGER NOT NULL DEFAULT 3",
        },
        "inventory_item": {
            "item_code": "TEXT",
            "category_name": "TEXT",
            "subcategory_name": "TEXT",
            "average_daily_usage": "FLOAT NOT NULL DEFAULT 0",
            "purchase_price": "FLOAT NOT NULL DEFAULT 0",
            "selling_relation": "TEXT",
            "shelf_life_days": "INTEGER",
            "expiry_tracking": "BOOLEAN NOT NULL DEFAULT 0",
            "storage_location": "TEXT",
            "vendor_id": "INTEGER",
        },
        "inventory_recipe": {
            "prep_time_minutes": "INTEGER",
            "ingredients_note": "TEXT",
            "preparation_steps": "TEXT",
            "plating_notes": "TEXT",
            "quality_checks": "TEXT",
            "allergy_alerts": "TEXT",
            "training_notes": "TEXT",
            "sop_photo_url": "TEXT",
            "size_sop_json": "TEXT",
        },
        "inventory_expense_log": {
            "vendor_id": "INTEGER",
            "transaction_mode": "TEXT",
        },
        "job_opening": {
            "salary_display": "TEXT",
            "vacancies": "INTEGER NOT NULL DEFAULT 1",
            "priority": "TEXT NOT NULL DEFAULT 'Normal'",
            "experience_required": "TEXT",
            "education_required": "TEXT",
            "description": "TEXT",
            "responsibilities": "TEXT",
            "requirements": "TEXT",
            "benefits": "TEXT",
            "working_hours": "TEXT",
            "weekly_off": "TEXT",
            "perks": "TEXT",
            "growth_opportunities": "TEXT",
            "skills_json": "TEXT",
            "require_resume": "BOOLEAN NOT NULL DEFAULT 1",
            "require_cover_letter": "BOOLEAN NOT NULL DEFAULT 0",
            "require_photograph": "BOOLEAN NOT NULL DEFAULT 0",
            "require_aadhaar": "BOOLEAN NOT NULL DEFAULT 0",
            "require_driving_license": "BOOLEAN NOT NULL DEFAULT 0",
            "auto_close_days": "INTEGER",
            "max_applicants": "INTEGER",
            "career_slug": "TEXT",
            "meta_title": "TEXT",
            "meta_description": "TEXT",
            "published_at": "DATETIME",
            "archived_at": "DATETIME",
        },
        "job_application": {
            "application_code": "TEXT",
            "source": "TEXT",
            "gender": "TEXT",
            "dob": "DATE",
            "whatsapp": "TEXT",
            "address": "TEXT",
            "city": "TEXT",
            "state": "TEXT",
            "pincode": "TEXT",
            "highest_qualification": "TEXT",
            "school_college": "TEXT",
            "passing_year": "TEXT",
            "percentage": "TEXT",
            "currently_working": "BOOLEAN NOT NULL DEFAULT 0",
            "previous_employer": "TEXT",
            "current_salary": "TEXT",
            "expected_salary": "TEXT",
            "notice_period": "TEXT",
            "experience": "TEXT",
            "skills_json": "TEXT",
            "immediate_joining": "BOOLEAN NOT NULL DEFAULT 0",
            "available_from": "DATE",
            "cover_letter": "TEXT",
            "declaration_confirmed": "BOOLEAN NOT NULL DEFAULT 0",
            "resume_file_path": "TEXT",
            "photo_file_path": "TEXT",
            "aadhaar_file_path": "TEXT",
            "driving_license_file_path": "TEXT",
            "certificates_file_path": "TEXT",
            "admin_notes": "TEXT",
        },
        "job_application_timeline": {
            "changed_by_user_id": "INTEGER",
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
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_created ON cafe_order (created_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_delivery_created ON cafe_order (is_delivery, created_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_menu_item_chef_user_id ON menu_item (chef_user_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_staff_mobile_session_user_active ON staff_mobile_session (user_id, active)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_staff_mobile_session_device ON staff_mobile_session (device_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_item_order ON cafe_order_item (order_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_item_menu ON cafe_order_item (menu_item_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_order_item_prep_status ON cafe_order_item (prep_status)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_feedback_primary_order ON cafe_feedback (primary_order_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_feedback_table_submitted ON cafe_feedback (table_id, submitted_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_cafe_feedback_item_feedback_menu ON cafe_feedback_item (feedback_id, menu_item_id)")
    )
    db.session.execute(
        text("UPDATE cafe_order_item SET prep_status = 'pending' WHERE prep_status IS NULL OR prep_status = ''")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_menu_item_available_cat_sub_type ON menu_item (available, category_id, subcategory_id, item_type)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_staff_attendance_user_date ON staff_attendance (user_id, attendance_date)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_inventory_item_category_name ON inventory_item (category_name, name)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_inventory_daily_closing_item_date ON inventory_daily_closing (item_id, closing_date)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_inventory_expense_log_date_category ON inventory_expense_log (entry_date, category_id)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_job_opening_status_published ON job_opening (status, published_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_job_application_job_status_created ON job_application (job_id, status, created_at)")
    )
    db.session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_job_application_mobile ON job_application (mobile)")
    )
    db.session.execute(
        text("UPDATE user SET roles_json = json_array(lower(role)) WHERE (roles_json IS NULL OR trim(roles_json) = '') AND role IS NOT NULL AND trim(role) != ''")
    )
    db.session.commit()


def _normalize_inventory_categories():
    alias_map = {
        "dairy & refrigerated": "Dairy",
        "dry grocery": "Groceries",
        "coffee & beverage": "Coffee & Beverages",
        "beverage supplies": "Coffee & Beverages",
        "packaging & utility": "Utility",
        "cleaning & hygiene": "Cleaning Products",
        "machinery & equipment": "Maintenance & Equipment",
    }
    all_categories = InventoryCategory.query.order_by(InventoryCategory.id.asc()).all()
    category_by_name = {
        (category.name or "").strip().lower(): category
        for category in all_categories
        if (category.name or "").strip()
    }
    changed = False
    for alias_name, canonical_name in alias_map.items():
        alias = category_by_name.get(alias_name)
        canonical = category_by_name.get(canonical_name.lower())
        if not alias or not canonical or alias.id == canonical.id:
            continue
        InventoryExpenseLog.query.filter_by(category_id=alias.id).update({"category_id": canonical.id})
        InventoryItem.query.filter(
            db.func.lower(db.func.trim(InventoryItem.category_name)) == alias_name
        ).update({"category_name": canonical.name}, synchronize_session=False)
        if alias.active:
            alias.active = False
            changed = True
    if changed:
        db.session.commit()


def _ensure_other_inventory_vendor():
    vendor = InventoryVendor.query.filter(db.func.lower(InventoryVendor.name) == "other").first()
    if not vendor:
        vendor = InventoryVendor(name="Other", active=True)
        db.session.add(vendor)
        db.session.commit()
    elif not vendor.active:
        vendor.active = True
        db.session.commit()
    return vendor


def _ensure_default_workstations():
    defaults = [
        ("kitchen", "Kitchen"),
        ("barista", "Barista Counter"),
    ]
    changed = False
    for index, (slug, name) in enumerate(defaults, start=1):
        workstation = Workstation.query.filter_by(slug=slug).first()
        if not workstation:
            workstation = Workstation(
                slug=slug,
                name=name,
                active=True,
                display_order=index,
            )
            db.session.add(workstation)
            changed = True
            continue
        if not workstation.name:
            workstation.name = name
            changed = True
        if workstation.display_order != index:
            workstation.display_order = index
            changed = True
        if not workstation.active:
            workstation.active = True
            changed = True
    if changed:
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
    (uploads_root / "recruitment").mkdir(parents=True, exist_ok=True)
    (uploads_root / "salary_receipts").mkdir(parents=True, exist_ok=True)
    app.config["UPLOADS_ROOT"] = str(uploads_root)

    db.init_app(app)
    socketio.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_sqlite_schema_columns()
        _ensure_protected_menu_categories()
        _backfill_order_codes()
        _backfill_paid_timestamps()
        _ensure_default_workstations()
        default_inventory_categories = [
            ("Groceries", "package", "#b08968"),
            ("Dairy", "milk", "#6ea8fe"),
            ("Fresh Produce", "leaf", "#6cab7a"),
            ("Coffee & Beverages", "cup-soda", "#8b5e3c"),
            ("Bakery & Confectionery", "cake-slice", "#d49a89"),
            ("Utility", "bolt", "#d6a75b"),
            ("Packaging & Utility", "box", "#d0a85c"),
            ("Cleaning Products", "sparkles", "#9aa6b2"),
            ("Kitchen Supplies", "utensils", "#a17755"),
            ("Gas & Fuel", "flame", "#de7c4a"),
            ("Water & Ice", "droplets", "#6aa9c8"),
            ("Maintenance & Equipment", "cog", "#7c7f8a"),
            ("Interior & Consumables", "armchair", "#b2a39b"),
        ]
        for name, icon, color in default_inventory_categories:
            if not InventoryCategory.query.filter(db.func.lower(InventoryCategory.name) == name.lower()).first():
                db.session.add(InventoryCategory(name=name, icon=icon, color=color, active=True))
        db.session.commit()
        _normalize_inventory_categories()
        _ensure_other_inventory_vendor()
    app.before_request(load_current_user)
    @app.context_processor
    def inject_role_helpers():
        return {
            "user_has_any_role": user_has_any_role,
            "user_has_permission": user_has_permission,
            "user_display_roles": user_display_roles,
            "user_primary_role": user_primary_role,
        }

    app.register_blueprint(main_bp)
    app.register_blueprint(cafe_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(mobile_attendance_bp)

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
