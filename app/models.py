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
    user_type_id = db.Column(db.Integer, db.ForeignKey("user_type.id"), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    user_type = db.relationship("UserType", backref="users")


class UserType(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    can_access_cafe = db.Column(db.Boolean, default=False, nullable=False)
    can_access_library = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_staff = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_menu = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_orders = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_kitchen = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_inventory = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_cashier = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_stats = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_library_members = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_library_books = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_library_loans = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_library_payments = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_library_plans = db.Column(db.Boolean, default=False, nullable=False)
    can_view_staff_profiles = db.Column(db.Boolean, default=False, nullable=False)
    can_upload_salary = db.Column(db.Boolean, default=False, nullable=False)
    can_view_delivery_locations = db.Column(db.Boolean, default=False, nullable=False)


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
    category_ids_json = db.Column(db.String(500), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    image_url = db.Column(db.String(255), nullable=True)
    short_description = db.Column(db.String(140), nullable=True)
    description = db.Column(db.String(500), nullable=True)
    calories = db.Column(db.Integer, nullable=True)
    price = db.Column(db.Float, nullable=False, default=0)
    has_size_variants = db.Column(db.Boolean, default=False, nullable=False)
    size_pricing_json = db.Column(db.String(1000), nullable=True)
    prep_station = db.Column(db.String(40), nullable=False, default="kitchen")
    available = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    category = db.relationship("MenuCategory", backref="items")
    subcategory = db.relationship("MenuSubcategory", backref="items")


class CafeOrder(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    table_id = db.Column(db.Integer, db.ForeignKey("cafe_table.id"), nullable=False)
    ordered_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(40), default="open", nullable=False)
    paid_at = db.Column(db.DateTime, nullable=True)
    payment_type = db.Column(db.String(40), nullable=True)
    payment_reference = db.Column(db.String(120), nullable=True)
    payment_breakdown_json = db.Column(db.Text, nullable=True)
    is_delivery = db.Column(db.Boolean, default=False, nullable=False)
    delivery_customer_name = db.Column(db.String(120), nullable=True)
    delivery_customer_mobile = db.Column(db.String(20), nullable=True)
    delivery_address = db.Column(db.String(255), nullable=True)
    delivery_lat = db.Column(db.Float, nullable=True)
    delivery_lng = db.Column(db.Float, nullable=True)
    delivery_map_url = db.Column(db.String(500), nullable=True)
    packaging_charge = db.Column(db.Float, default=0, nullable=False)
    delivery_distance_km = db.Column(db.Float, default=0, nullable=False)
    delivery_charge = db.Column(db.Float, default=0, nullable=False)
    daily_sequence = db.Column(db.Integer, nullable=True)
    display_code = db.Column(db.String(30), nullable=True)
    total_amount = db.Column(db.Float, default=0, nullable=False)
    table = db.relationship("CafeTable", backref="orders")
    ordered_by = db.relationship("User", backref="orders")


class CafeOrderItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("cafe_order.id"), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey("menu_item.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0)
    size_label = db.Column(db.String(80), nullable=True)
    is_parcel = db.Column(db.Boolean, nullable=False, default=False)
    approval_status = db.Column(db.String(20), nullable=False, default="pending")
    order = db.relationship("CafeOrder", backref="order_items")
    menu_item = db.relationship("MenuItem")


class InventoryItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_code = db.Column(db.String(40), unique=True, nullable=True)
    area = db.Column(db.String(40), nullable=False)  # barista/kitchen/cafe
    name = db.Column(db.String(120), nullable=False)
    category_name = db.Column(db.String(80), nullable=True)
    subcategory_name = db.Column(db.String(80), nullable=True)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    current_amount = db.Column(db.Float, nullable=False, default=0)
    required_amount = db.Column(db.Float, nullable=False, default=0)
    reorder_level = db.Column(db.Float, nullable=False, default=0)
    average_daily_usage = db.Column(db.Float, nullable=False, default=0)
    purchase_price = db.Column(db.Float, nullable=False, default=0)
    selling_relation = db.Column(db.String(120), nullable=True)
    shelf_life_days = db.Column(db.Integer, nullable=True)
    expiry_tracking = db.Column(db.Boolean, default=False, nullable=False)
    storage_location = db.Column(db.String(120), nullable=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("inventory_vendor.id"), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    vendor = db.relationship("InventoryVendor", backref="items")


class InventoryCategory(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    icon = db.Column(db.String(40), nullable=True)
    color = db.Column(db.String(20), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)


class InventoryVendor(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    vendor_category = db.Column(db.String(80), nullable=True)
    contact_person = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    gst_number = db.Column(db.String(40), nullable=True)
    payment_terms = db.Column(db.String(120), nullable=True)
    outstanding_balance = db.Column(db.Float, nullable=False, default=0)
    average_rate_note = db.Column(db.String(255), nullable=True)
    note = db.Column(db.String(500), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)


class InventoryPurchase(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_date = db.Column(db.Date, nullable=False, default=date.today)
    vendor_id = db.Column(db.Integer, db.ForeignKey("inventory_vendor.id"), nullable=True)
    invoice_number = db.Column(db.String(80), nullable=True)
    invoice_file_path = db.Column(db.String(255), nullable=True)
    subtotal = db.Column(db.Float, nullable=False, default=0)
    tax_amount = db.Column(db.Float, nullable=False, default=0)
    total_amount = db.Column(db.Float, nullable=False, default=0)
    payment_status = db.Column(db.String(20), nullable=False, default="pending")
    note = db.Column(db.String(255), nullable=True)
    vendor = db.relationship("InventoryVendor", backref="purchases")


class InventoryPurchaseLine(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("inventory_purchase.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit_price = db.Column(db.Float, nullable=False, default=0)
    line_total = db.Column(db.Float, nullable=False, default=0)
    purchase = db.relationship("InventoryPurchase", backref="lines")
    item = db.relationship("InventoryItem", backref="purchase_lines")


class InventoryDailyClosing(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    closing_date = db.Column(db.Date, nullable=False, default=date.today)
    item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"), nullable=False)
    opening_stock = db.Column(db.Float, nullable=False, default=0)
    closing_stock = db.Column(db.Float, nullable=False, default=0)
    consumed_amount = db.Column(db.Float, nullable=False, default=0)
    variance_amount = db.Column(db.Float, nullable=False, default=0)
    note = db.Column(db.String(255), nullable=True)
    item = db.relationship("InventoryItem", backref="daily_closing_rows")


class InventoryRecipe(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey("menu_item.id"), nullable=False, unique=True)
    yield_qty = db.Column(db.Float, nullable=False, default=1)
    yield_unit = db.Column(db.String(20), nullable=True)
    prep_time_minutes = db.Column(db.Integer, nullable=True)
    ingredients_note = db.Column(db.Text, nullable=True)
    preparation_steps = db.Column(db.Text, nullable=True)
    plating_notes = db.Column(db.Text, nullable=True)
    quality_checks = db.Column(db.Text, nullable=True)
    allergy_alerts = db.Column(db.Text, nullable=True)
    training_notes = db.Column(db.Text, nullable=True)
    sop_photo_url = db.Column(db.String(500), nullable=True)
    size_sop_json = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    menu_item = db.relationship("MenuItem", backref=db.backref("inventory_recipe", uselist=False))


class InventoryRecipeItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("inventory_recipe.id"), nullable=False)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"), nullable=False)
    qty_per_menu = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    recipe = db.relationship("InventoryRecipe", backref="ingredients")
    inventory_item = db.relationship("InventoryItem", backref="recipe_links")


class InventoryWastage(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wastage_date = db.Column(db.Date, nullable=False, default=date.today)
    item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    reason = db.Column(db.String(255), nullable=True)
    item = db.relationship("InventoryItem", backref="wastage_rows")


class StaffProfile(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    joining_date = db.Column(db.Date, nullable=True)
    dob = db.Column(db.Date, nullable=True)
    marital_status = db.Column(db.String(40), nullable=True)
    gender = db.Column(db.String(20), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    alternate_contact = db.Column(db.String(20), nullable=True)
    emergency_contact = db.Column(db.String(120), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    salary_type = db.Column(db.String(40), nullable=True)
    salary_amount = db.Column(db.Float, nullable=True)
    probation_end_date = db.Column(db.Date, nullable=True)
    pan_number = db.Column(db.String(20), nullable=True)
    bank_account_name = db.Column(db.String(120), nullable=True)
    bank_account_number = db.Column(db.String(40), nullable=True)
    bank_ifsc = db.Column(db.String(20), nullable=True)
    bank_name = db.Column(db.String(120), nullable=True)
    govt_id_type = db.Column(db.String(40), nullable=True)
    govt_id_number = db.Column(db.String(80), nullable=True)
    govt_id_file_path = db.Column(db.String(255), nullable=True)
    photo_file_path = db.Column(db.String(255), nullable=True)
    archived = db.Column(db.Boolean, default=False, nullable=False)
    user = db.relationship("User", backref=db.backref("staff_profile", uselist=False))


class StaffAttendance(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    attendance_date = db.Column(db.Date, nullable=False, default=date.today)
    status = db.Column(db.String(20), nullable=False, default="present")
    check_in_at = db.Column(db.DateTime, nullable=True)
    check_out_at = db.Column(db.DateTime, nullable=True)
    manager_override = db.Column(db.Boolean, default=False, nullable=False)
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


class Customer(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    mobile = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    default_address = db.Column(db.String(255), nullable=True)
    default_lat = db.Column(db.Float, nullable=True)
    default_lng = db.Column(db.Float, nullable=True)
    default_map_url = db.Column(db.String(500), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)


class StaffDocument(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    doc_type = db.Column(db.String(80), nullable=False)
    doc_number = db.Column(db.String(120), nullable=True)
    file_path = db.Column(db.String(255), nullable=False)
    released_by_admin = db.Column(db.Boolean, default=False, nullable=False)
    verification_status = db.Column(db.String(20), nullable=False, default="pending")
    verification_note = db.Column(db.String(255), nullable=True)
    user = db.relationship("User", foreign_keys=[user_id], backref="documents")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_user_id])


class SalaryReceipt(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    salary_month = db.Column(db.Integer, nullable=False)
    salary_year = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Float, nullable=True)
    file_path = db.Column(db.String(255), nullable=False)
    note = db.Column(db.String(255), nullable=True)
    user = db.relationship("User", foreign_keys=[user_id], backref="salary_receipts")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_user_id])


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
    member_code = db.Column(db.String(30), unique=True, nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    subscription_plan_id = db.Column(
        db.Integer, db.ForeignKey("subscription_plan.id"), nullable=True
    )
    subscription_start_date = db.Column(db.Date, nullable=True)
    subscription_end_date = db.Column(db.Date, nullable=True)
    card_number = db.Column(db.String(50), nullable=True)
    govt_id_image_path = db.Column(db.String(255), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    subscription_plan = db.relationship("SubscriptionPlan", backref="members")


class LibraryAuthor(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    active = db.Column(db.Boolean, default=True, nullable=False)


class Book(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    author = db.Column(db.String(120), nullable=False)
    genre = db.Column(db.String(80), nullable=True)
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
    due_reminder_sent_on = db.Column(db.Date, nullable=True)
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


class TableBooking(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    people_count = db.Column(db.Integer, nullable=False, default=2)
    booking_date = db.Column(db.Date, nullable=False)
    start_hour = db.Column(db.Integer, nullable=False)  # 8..21
    end_hour = db.Column(db.Integer, nullable=False)  # 9..22
    note = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="booked")
