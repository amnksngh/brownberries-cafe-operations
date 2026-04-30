from datetime import date, datetime

from .extensions import db


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class User(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), nullable=False, default="staff")
    active = db.Column(db.Boolean, default=True, nullable=False)


class CafeTable(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    seating_capacity = db.Column(db.Integer, nullable=False, default=2)
    metadata_note = db.Column(db.String(255), nullable=True)
    qr_slug = db.Column(db.String(120), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)


class MenuCategory(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)


class MenuSubcategory(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("menu_category.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    category = db.relationship("MenuCategory", backref="subcategories")


class MenuType(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)


class MenuItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("menu_category.id"), nullable=False)
    subcategory_id = db.Column(
        db.Integer, db.ForeignKey("menu_subcategory.id"), nullable=True
    )
    item_type = db.Column(db.String(80), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    image_url = db.Column(db.String(255), nullable=True)
    description = db.Column(db.String(500), nullable=True)
    calories = db.Column(db.Integer, nullable=True)
    price = db.Column(db.Float, nullable=False, default=0)
    available = db.Column(db.Boolean, default=True, nullable=False)
    category = db.relationship("MenuCategory", backref="items")
    subcategory = db.relationship("MenuSubcategory", backref="items")


class CafeOrder(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    table_id = db.Column(db.Integer, db.ForeignKey("cafe_table.id"), nullable=False)
    ordered_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(40), default="open", nullable=False)
    payment_type = db.Column(db.String(40), nullable=True)
    payment_reference = db.Column(db.String(120), nullable=True)
    total_amount = db.Column(db.Float, default=0, nullable=False)
    table = db.relationship("CafeTable", backref="orders")
    ordered_by = db.relationship("User", backref="orders")


class CafeOrderItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("cafe_order.id"), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey("menu_item.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0)
    order = db.relationship("CafeOrder", backref="order_items")
    menu_item = db.relationship("MenuItem")


class InventoryItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    area = db.Column(db.String(40), nullable=False)  # barista/kitchen/cafe
    name = db.Column(db.String(120), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    current_amount = db.Column(db.Float, nullable=False, default=0)
    required_amount = db.Column(db.Float, nullable=False, default=0)
    reorder_level = db.Column(db.Float, nullable=False, default=0)
    note = db.Column(db.String(255), nullable=True)


class StaffProfile(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    joining_date = db.Column(db.Date, nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    alternate_contact = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    pan_number = db.Column(db.String(20), nullable=True)
    bank_account_name = db.Column(db.String(120), nullable=True)
    bank_account_number = db.Column(db.String(40), nullable=True)
    bank_ifsc = db.Column(db.String(20), nullable=True)
    bank_name = db.Column(db.String(120), nullable=True)
    archived = db.Column(db.Boolean, default=False, nullable=False)
    user = db.relationship("User", backref=db.backref("staff_profile", uselist=False))


class StaffAttendance(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    attendance_date = db.Column(db.Date, nullable=False, default=date.today)
    status = db.Column(db.String(20), nullable=False, default="present")
    notes = db.Column(db.String(255), nullable=True)
    user = db.relationship("User", backref="attendance_logs")


class StaffLeaveRequest(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    leave_type = db.Column(db.String(40), nullable=False, default="casual")
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    admin_remarks = db.Column(db.String(255), nullable=True)
    user = db.relationship("User", backref="leave_requests")


class SubscriptionPlan(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), nullable=False, unique=True)  # monthly/quarterly/yearly
    duration_days = db.Column(db.Integer, nullable=False)
    weekly_reissue_fee_per_book = db.Column(db.Float, nullable=False, default=10)
    late_fee_per_day = db.Column(db.Float, nullable=False, default=5)
    active = db.Column(db.Boolean, default=True, nullable=False)


class LibraryMember(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    subscription_plan_id = db.Column(
        db.Integer, db.ForeignKey("subscription_plan.id"), nullable=True
    )
    subscription_start_date = db.Column(db.Date, nullable=True)
    subscription_end_date = db.Column(db.Date, nullable=True)
    card_number = db.Column(db.String(50), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    subscription_plan = db.relationship("SubscriptionPlan", backref="members")


class Book(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    author = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(80), nullable=True)
    shelf_no = db.Column(db.String(40), nullable=False)
    total_copies = db.Column(db.Integer, nullable=False, default=1)
    available_copies = db.Column(db.Integer, nullable=False, default=1)


class LibraryLoan(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("library_member.id"), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey("book.id"), nullable=False)
    issue_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date, nullable=False)
    return_date = db.Column(db.Date, nullable=True)
    reissue_count = db.Column(db.Integer, nullable=False, default=0)
    weekly_fee_per_book = db.Column(db.Float, nullable=False, default=10)
    late_fee_per_day = db.Column(db.Float, nullable=False, default=5)
    damage_fee = db.Column(db.Float, nullable=False, default=0)
    lost_fee = db.Column(db.Float, nullable=False, default=0)
    total_charge = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(40), nullable=False, default="issued")
    member = db.relationship("LibraryMember", backref="loans")
    book = db.relationship("Book", backref="loans")


class LibraryPayment(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("library_member.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_type = db.Column(db.String(40), nullable=False)
    reference = db.Column(db.String(120), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    member = db.relationship("LibraryMember", backref="payments")
