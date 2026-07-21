import json
import os
import calendar
import math
import re
from datetime import date, datetime, time
from io import BytesIO
from uuid import uuid4
from zoneinfo import ZoneInfo

import qrcode
from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .attendance_logic import (
    ATTENDANCE_STATUS_LABELS,
    ATTENDANCE_STATUS_OPTIONS,
    SELF_LEAVE_TYPE_OPTIONS,
    attendance_datetime_for,
    attendance_flags_for_row,
    attendance_pay_fraction,
    attendance_status_label,
    build_attendance_summary,
    late_penalty_days,
    refresh_attendance_row,
    worked_hours_for_row,
)
from .auth_helpers import login_required, roles_required, user_has_any_role, user_has_permission
from .extensions import socketio
from .extensions import db
from .deploy_config import load_deployment_config
from .models import (
    CafeFeedback,
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    LibraryLoan,
    LibraryPayment,
    JobApplication,
    JobApplicationTimeline,
    JobOpening,
    MenuCategory,
    MenuItem,
    Customer,
    StaffAttendance,
    StaffDocument,
    StaffLeaveRequest,
    StaffProfile,
    SalaryReceipt,
    TableBooking,
    UserType,
    User,
)

bp = Blueprint("main", __name__)
PROTECTED_ADMIN_EMAIL = "admin@brownberries.local"
PROTECTED_MENU_CATEGORY_NAMES = {"other", "utility"}
IST_TZ = ZoneInfo("Asia/Kolkata")
UTC_TZ = ZoneInfo("UTC")
DEFAULT_ATTENDANCE_CAFE_LAT = 25.207989477704068
DEFAULT_ATTENDANCE_CAFE_LNG = 80.87374457551877
DEFAULT_ATTENDANCE_RADIUS_METERS = 120.0
STAFF_CALL_COOLDOWN_SECONDS = 5 * 60
JOB_APPLICATION_STATUSES = [
    "applied",
    "resume_screening",
    "phone_call",
    "interview_scheduled",
    "interview_completed",
    "selected",
    "offer_sent",
    "accepted",
    "joined",
    "rejected",
]
JOB_EMPLOYMENT_TYPES = ["Full Time", "Part Time", "Intern", "Contract"]
JOB_LOCATION_TYPES = ["Brownberries Cafe", "Remote", "Hybrid"]
JOB_PRIORITY_TYPES = ["Urgent", "Normal", "Future Hiring"]
JOB_EDUCATION_TYPES = ["Any", "10th", "12th", "Graduate"]
JOB_EXPERIENCE_TYPES = ["0 Years", "1 Year", "2 Years", "3+ Years"]


def _staff_call_cooldown_remaining_seconds(table: CafeTable | None) -> int:
    if not table or not table.last_staff_call_at:
        return 0
    elapsed = (datetime.utcnow() - table.last_staff_call_at).total_seconds()
    remaining = STAFF_CALL_COOLDOWN_SECONDS - int(elapsed)
    return max(0, remaining)
RECRUITMENT_SKILL_OPTIONS = [
    "Coffee", "Espresso", "Latte Art", "POS", "Communication", "Customer Service",
    "Cleaning", "South Indian", "North Indian", "Chinese", "Pizza", "Pasta",
    "Cash Handling", "Milk Steaming", "Inventory", "Delivery", "Book Handling",
]
PUBLIC_JOB_DEPARTMENTS = [
    "Barista", "Kitchen", "Service", "Cashier", "Library", "Inventory", "Housekeeping", "Delivery",
]


def _is_cafe_admin(user: User) -> bool:
    return user.email == PROTECTED_ADMIN_EMAIL or (
        user_has_any_role(user, "admin") and user.full_name.strip().lower() == "cafe admin"
    )


def _slugify_text(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or f"job-{uuid4().hex[:8]}"


def _json_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _set_json_list(items: list[str]) -> str:
    cleaned: list[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned)


@bp.route("/healthz")
def healthz():
    try:
        db.session.execute(db.select(User.id).limit(1)).scalar()
        return jsonify(
            {
                "ok": True,
                "service": "brownberries-cafe-operations",
                "timestamp_ist": datetime.now(IST_TZ).isoformat(),
            }
        )
    except Exception as exc:
        current_app.logger.exception("Health check failed")
        return (
            jsonify(
                {
                    "ok": False,
                    "service": "brownberries-cafe-operations",
                    "error": str(exc),
                }
            ),
            500,
        )


def _job_skills(job: JobOpening) -> list[str]:
    return _json_list(job.skills_json)


def _application_skills(application: JobApplication) -> list[str]:
    return _json_list(application.skills_json)


def _job_salary_text(job: JobOpening) -> str:
    if (job.salary_display or "").strip():
        return job.salary_display.strip()
    if job.salary_min is not None and job.salary_max is not None:
        return f"₹{int(job.salary_min):,} - ₹{int(job.salary_max):,}"
    if job.salary_min is not None:
        return f"From ₹{int(job.salary_min):,}"
    if job.salary_max is not None:
        return f"Up to ₹{int(job.salary_max):,}"
    return "Salary discussed at interview"


def _job_is_publicly_open(job: JobOpening) -> bool:
    if not job or job.status != "published":
        return False
    now = datetime.utcnow()
    if job.archived_at:
        return False
    if job.auto_close_days and job.published_at:
        days_live = (now - job.published_at).days
        if days_live >= max(1, int(job.auto_close_days)):
            return False
    if job.max_applicants:
        if "applications" in job.__dict__:
            active_count = sum(1 for app in (job.applications or []) if app.status != "rejected")
        else:
            active_count = JobApplication.query.filter(
                JobApplication.job_id == job.id,
                JobApplication.status != "rejected",
            ).count()
        if active_count >= max(1, int(job.max_applicants)):
            return False
    return True


def _job_status_label(status: str) -> str:
    return str(status or "").replace("_", " ").title()


def _job_application_code(application_id: int, created_at: datetime | None = None) -> str:
    ref_dt = created_at or datetime.utcnow()
    return f"BBC-{ref_dt.year}-{int(application_id):06d}"


def _append_application_timeline(application: JobApplication, status: str, note: str | None = None, changed_by: User | None = None):
    db.session.add(
        JobApplicationTimeline(
            application_id=application.id,
            status=status,
            note=(note or "").strip() or None,
            changed_by_user_id=changed_by.id if changed_by else None,
        )
    )


def _job_card_payload(job: JobOpening) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "department": job.department,
        "employment_type": job.employment_type,
        "location_type": job.location_type,
        "salary_text": _job_salary_text(job),
        "vacancies": job.vacancies,
        "priority": job.priority,
        "experience_required": job.experience_required or "Flexible",
        "education_required": job.education_required or "Flexible",
        "skills": _job_skills(job),
        "career_slug": job.career_slug,
        "status": job.status,
        "published_at": job.published_at,
        "is_open": _job_is_publicly_open(job),
        "application_count": len(job.applications),
    }


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


def _build_salary_summary(profile, attendance_logs: list[StaffAttendance], ref_date: date | None = None):
    ref_date = ref_date or date.today()
    days_in_month = calendar.monthrange(ref_date.year, ref_date.month)[1]
    monthly_salary = float(profile.salary_amount or 0)
    per_day_salary = round((monthly_salary / days_in_month), 2) if monthly_salary and days_in_month else 0.0
    payable_days = 0.0
    unpaid_days = 0.0
    for row in attendance_logs:
        fraction = attendance_pay_fraction(row.status)
        payable_days += fraction
        unpaid_days += max(0.0, 1.0 - fraction)
    estimated_pay = round(per_day_salary * payable_days, 2)
    return {
        "salary_type": profile.salary_type or "monthly",
        "salary_amount": monthly_salary,
        "days_in_month": days_in_month,
        "per_day_salary": per_day_salary,
        "payable_days": round(payable_days, 2),
        "unpaid_days": round(unpaid_days, 2),
        "estimated_pay": estimated_pay,
        "payment_day": 6,
    }


def _leave_notice_message(start_date: date, end_date: date):
    leave_days = (end_date - start_date).days + 1
    today = date.today()
    notice_days = max(0, (start_date - today).days)
    if leave_days > 7 and notice_days < 30:
        return "Leaves longer than 7 days should be requested at least 1 month in advance."


def _current_ist_day_bounds_utc_naive():
    today_ist = datetime.now(IST_TZ).date()
    start_local = datetime.combine(today_ist, time.min).replace(tzinfo=IST_TZ)
    end_local = datetime.combine(today_ist, time.max).replace(tzinfo=IST_TZ)
    return (
        start_local.astimezone(UTC_TZ).replace(tzinfo=None),
        end_local.astimezone(UTC_TZ).replace(tzinfo=None),
    )
    if leave_days > 1 and notice_days < 7:
        return "Leaves longer than 1 day should ideally be requested at least 7 days in advance."
    return ""


def _service_charge_rate_for_public():
    cfg = load_deployment_config(current_app.instance_path)
    try:
        value = float(cfg.get("SERVICE_CHARGE_RATE") or cfg.get("SERVICE_TAX_RATE") or 5.0)
    except (TypeError, ValueError):
        value = 5.0
    return round(min(max(value or 5.0, 5.0), 10.0), 2)


def _table_feedback_prompt(table: CafeTable | None) -> dict | None:
    if not table:
        return None
    today_start, today_end = _current_ist_day_bounds_utc_naive()
    latest_paid_order = (
        CafeOrder.query.options(joinedload(CafeOrder.table))
        .filter(
            CafeOrder.table_id == table.id,
            CafeOrder.status == "paid",
            CafeOrder.paid_at.is_not(None),
            CafeOrder.paid_at >= today_start,
            CafeOrder.paid_at <= today_end,
        )
        .order_by(CafeOrder.paid_at.desc(), CafeOrder.id.desc())
        .first()
    )
    if not latest_paid_order:
        return None
    primary_order = (
        CafeOrder.query.filter(
            CafeOrder.table_id == latest_paid_order.table_id,
            CafeOrder.status == "paid",
            CafeOrder.paid_at == latest_paid_order.paid_at,
        )
        .order_by(CafeOrder.created_at.asc(), CafeOrder.id.asc())
        .first()
    ) or latest_paid_order
    feedback = CafeFeedback.query.filter_by(primary_order_id=primary_order.id).order_by(CafeFeedback.id.desc()).first()
    settlement_total = (
        db.session.query(db.func.coalesce(db.func.sum(CafeOrder.total_amount), 0))
        .filter(
            CafeOrder.table_id == latest_paid_order.table_id,
            CafeOrder.status == "paid",
            CafeOrder.paid_at == latest_paid_order.paid_at,
        )
        .scalar()
        or 0
    )
    return {
        "primary_order_id": primary_order.id,
        "settlement_total": round(float(settlement_total or 0), 2),
        "feedback_exists": bool(feedback),
        "feedback_source": (feedback.source if feedback else ""),
        "feedback_editable": (not feedback) or (feedback.source != "online"),
        "paid_at": latest_paid_order.paid_at.astimezone(UTC_TZ).isoformat() if getattr(latest_paid_order.paid_at, "tzinfo", None) else (latest_paid_order.paid_at.isoformat() if latest_paid_order.paid_at else ""),
        "feedback_url": url_for("cafe.public_settlement_feedback", order_id=primary_order.id),
    }


def _public_menu_category_ids(item: MenuItem, category_name_by_id: dict[int, str]) -> list[int]:
    visible_ids: list[int] = []
    seen: set[int] = set()
    for cid in _menu_item_category_ids(item):
        cname = (category_name_by_id.get(cid) or "").strip().lower()
        if not cname or cname in PROTECTED_MENU_CATEGORY_NAMES or cid in seen:
            continue
        visible_ids.append(cid)
        seen.add(cid)
    return visible_ids


def _get_item_category_names(item: MenuItem, category_name_by_id: dict[int, str], include_protected: bool = True) -> list[str]:
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
                    if cname and (not include_protected) and cname.strip().lower() in PROTECTED_MENU_CATEGORY_NAMES:
                        continue
                    if cname and cname.lower() not in names_seen:
                        names.append(cname)
                        names_seen.add(cname.lower())
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if item.category and item.category.name:
        if include_protected or item.category.name.strip().lower() not in PROTECTED_MENU_CATEGORY_NAMES:
            if item.category.name.lower() not in names_seen:
                names.append(item.category.name)
                names_seen.add(item.category.name.lower())
    return names


def _is_public_menu_item(item: MenuItem, category_name_by_id: dict[int, str]) -> bool:
    return len(_public_menu_category_ids(item, category_name_by_id)) > 0


def _attendance_settings():
    cfg = load_deployment_config(current_app.instance_path)
    try:
        cafe_lat = float(cfg.get("ATTENDANCE_CAFE_LAT") or DEFAULT_ATTENDANCE_CAFE_LAT)
    except (TypeError, ValueError):
        cafe_lat = DEFAULT_ATTENDANCE_CAFE_LAT
    try:
        cafe_lng = float(cfg.get("ATTENDANCE_CAFE_LNG") or DEFAULT_ATTENDANCE_CAFE_LNG)
    except (TypeError, ValueError):
        cafe_lng = DEFAULT_ATTENDANCE_CAFE_LNG
    try:
        radius_m = float(cfg.get("ATTENDANCE_RADIUS_METERS") or DEFAULT_ATTENDANCE_RADIUS_METERS)
    except (TypeError, ValueError):
        radius_m = DEFAULT_ATTENDANCE_RADIUS_METERS
    return {
        "cafe_lat": cafe_lat,
        "cafe_lng": cafe_lng,
        "radius_m": max(20.0, radius_m),
    }


def _attendance_haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6371000.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_m * c


def _attendance_distance_from_cafe(lat: float | None, lng: float | None) -> float | None:
    if lat is None or lng is None:
        return None
    settings = _attendance_settings()
    return _attendance_haversine_m(settings["cafe_lat"], settings["cafe_lng"], float(lat), float(lng))


def _active_attendance_session_for_user(user_id: int):
    return (
        StaffAttendance.query.filter(
            StaffAttendance.user_id == user_id,
            StaffAttendance.check_in_at.is_not(None),
            StaffAttendance.check_out_at.is_(None),
        )
        .order_by(StaffAttendance.attendance_date.desc(), StaffAttendance.check_in_at.desc())
        .first()
    )


def _attendance_elapsed_text(row) -> str:
    if not row or not row.check_in_at:
        return "-"
    end_time = row.check_out_at or datetime.now()
    seconds = max(0, int((end_time - row.check_in_at).total_seconds()))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours:02d}h {minutes:02d}m"


def _attendance_source_label(row) -> str:
    method = ((getattr(row, "check_in_method", "") or "").strip().lower())
    if method == "qr_geofence":
        return "QR Check-In"
    if method == "admin_override":
        return "Admin Override"
    if method == "profile_manual":
        return "Manual"
    return "Manual"


def _visible_categories_for_available_menu() -> list[MenuCategory]:
    all_categories = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    category_name_by_id = {c.id: c.name for c in all_categories}
    categories = [c for c in all_categories if (c.name or "").strip().lower() not in PROTECTED_MENU_CATEGORY_NAMES]
    available_items = MenuItem.query.filter_by(available=True, is_deleted=False).all()
    used_category_ids: set[int] = set()
    for item in available_items:
        for cid in _public_menu_category_ids(item, category_name_by_id):
            used_category_ids.add(cid)
    return [
        c
        for c in categories
        if c.id in used_category_ids
    ]


@bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = (request.args.get("next") or request.form.get("next") or "").strip()
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email, active=True).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid credentials.", "error")
            return render_template("login.html", next_url=next_url)
        session.permanent = True
        session["user_id"] = user.id
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("main.dashboard"))
    return render_template("login.html", next_url=next_url)


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
    open_jobs = [
        _job_card_payload(job)
        for job in JobOpening.query.options(joinedload(JobOpening.applications)).filter_by(status="published")
        .order_by(JobOpening.priority.asc(), JobOpening.published_at.desc(), JobOpening.created_at.desc())
        .limit(6)
        .all()
        if _job_is_publicly_open(job)
    ]
    return render_template(
        "public_home.html",
        map_url=map_url,
        review_url=review_url,
        slots=slots,
        upcoming_bookings=upcoming,
        open_jobs=open_jobs,
        public_notice_text=notice_text,
        public_notice_enabled=notice_enabled and bool(notice_text),
        hide_staff_nav=True,
    )


@bp.route("/careers")
def careers():
    q = (request.args.get("q") or "").strip()
    department = (request.args.get("department") or "").strip()
    employment_type = (request.args.get("employment_type") or "").strip()
    location_type = (request.args.get("location_type") or "").strip()
    sort_by = (request.args.get("sort") or "newest").strip().lower()
    query = JobOpening.query.options(joinedload(JobOpening.applications)).filter_by(status="published").order_by(JobOpening.published_at.desc(), JobOpening.created_at.desc())
    if q:
        q_like = f"%{q}%"
        query = query.filter(
            db.or_(
                JobOpening.title.ilike(q_like),
                JobOpening.department.ilike(q_like),
                JobOpening.description.ilike(q_like),
                JobOpening.skills_json.ilike(q_like),
            )
        )
    if department:
        query = query.filter(JobOpening.department == department)
    if employment_type:
        query = query.filter(JobOpening.employment_type == employment_type)
    if location_type:
        query = query.filter(JobOpening.location_type == location_type)
    jobs = [job for job in query.all() if _job_is_publicly_open(job)]
    if sort_by == "highest_salary":
        jobs.sort(key=lambda job: float(job.salary_max or job.salary_min or 0), reverse=True)
    elif sort_by == "popular":
        jobs.sort(key=lambda job: len(job.applications), reverse=True)
    return render_template(
        "careers.html",
        jobs=[_job_card_payload(job) for job in jobs],
        filters={
            "q": q,
            "department": department,
            "employment_type": employment_type,
            "location_type": location_type,
            "sort": sort_by,
        },
        departments=PUBLIC_JOB_DEPARTMENTS,
        employment_types=JOB_EMPLOYMENT_TYPES,
        location_types=JOB_LOCATION_TYPES,
        hide_staff_nav=True,
    )


@bp.route("/careers/<string:career_slug>", methods=["GET", "POST"])
def career_detail(career_slug: str):
    job = JobOpening.query.filter_by(career_slug=career_slug).first_or_404()
    if not _job_is_publicly_open(job):
        flash("This job is not currently accepting applications.", "error")
        return redirect(url_for("main.careers"))
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        mobile = (request.form.get("mobile") or "").strip()
        if not full_name or not mobile:
            flash("Full name and mobile number are required.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))
        resume_file = request.files.get("resume_file")
        photo_file = request.files.get("photo_file")
        aadhaar_file = request.files.get("aadhaar_file")
        driving_license_file = request.files.get("driving_license_file")
        certificates_file = request.files.get("certificates_file")
        if job.require_resume and (not resume_file or not resume_file.filename):
            flash("Resume is required for this role.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))
        if job.require_photograph and (not photo_file or not photo_file.filename):
            flash("Photograph is required for this role.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))
        if job.require_aadhaar and (not aadhaar_file or not aadhaar_file.filename):
            flash("Aadhaar document is required for this role.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))
        if job.require_driving_license and (not driving_license_file or not driving_license_file.filename):
            flash("Driving license is required for this role.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))
        if job.require_cover_letter and not (request.form.get("cover_letter") or "").strip():
            flash("Cover letter is required for this role.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))
        if not request.form.get("declaration_confirmed"):
            flash("Please confirm that the information provided is correct.", "error")
            return redirect(url_for("main.career_detail", career_slug=career_slug))

        selected_skills = []
        for skill in request.form.getlist("skills"):
            text = (skill or "").strip()
            if text and text not in selected_skills:
                selected_skills.append(text)
        other_skills = [s.strip() for s in (request.form.get("other_skills") or "").split(",") if s.strip()]
        for skill in other_skills:
            if skill not in selected_skills:
                selected_skills.append(skill)

        application = JobApplication(
            job_id=job.id,
            application_code="PENDING",
            status="applied",
            source="Public Website",
            full_name=full_name,
            gender=(request.form.get("gender") or "").strip() or None,
            dob=date.fromisoformat(request.form["dob"]) if request.form.get("dob") else None,
            mobile=mobile,
            whatsapp=(request.form.get("whatsapp") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            address=(request.form.get("address") or "").strip() or None,
            city=(request.form.get("city") or "").strip() or None,
            state=(request.form.get("state") or "").strip() or None,
            pincode=(request.form.get("pincode") or "").strip() or None,
            highest_qualification=(request.form.get("highest_qualification") or "").strip() or None,
            school_college=(request.form.get("school_college") or "").strip() or None,
            passing_year=(request.form.get("passing_year") or "").strip() or None,
            percentage=(request.form.get("percentage") or "").strip() or None,
            currently_working=True if request.form.get("currently_working") else False,
            previous_employer=(request.form.get("previous_employer") or "").strip() or None,
            current_salary=(request.form.get("current_salary") or "").strip() or None,
            expected_salary=(request.form.get("expected_salary") or "").strip() or None,
            notice_period=(request.form.get("notice_period") or "").strip() or None,
            experience=(request.form.get("experience") or "").strip() or None,
            skills_json=_set_json_list(selected_skills),
            immediate_joining=True if request.form.get("immediate_joining") else False,
            available_from=date.fromisoformat(request.form["available_from"]) if request.form.get("available_from") else None,
            cover_letter=(request.form.get("cover_letter") or "").strip() or None,
            declaration_confirmed=True if request.form.get("declaration_confirmed") else False,
        )
        db.session.add(application)
        db.session.flush()
        application.application_code = _job_application_code(application.id, application.created_at)
        if resume_file and resume_file.filename:
            application.resume_file_path = _save_profile_file(resume_file, "recruitment", f"resume-{application.id}")
        if photo_file and photo_file.filename:
            application.photo_file_path = _save_profile_file(photo_file, "recruitment", f"photo-{application.id}")
        if aadhaar_file and aadhaar_file.filename:
            application.aadhaar_file_path = _save_profile_file(aadhaar_file, "recruitment", f"aadhaar-{application.id}")
        if driving_license_file and driving_license_file.filename:
            application.driving_license_file_path = _save_profile_file(driving_license_file, "recruitment", f"license-{application.id}")
        if certificates_file and certificates_file.filename:
            application.certificates_file_path = _save_profile_file(certificates_file, "recruitment", f"certificates-{application.id}")
        _append_application_timeline(application, "applied", "Application submitted from public careers page.")
        db.session.commit()
        return redirect(url_for("main.career_thank_you", application_code=application.application_code))

    return render_template(
        "career_detail.html",
        job=job,
        job_card=_job_card_payload(job),
        job_skills=_job_skills(job),
        skill_options=RECRUITMENT_SKILL_OPTIONS,
        hide_staff_nav=True,
    )


@bp.route("/careers/application/<string:application_code>")
def career_thank_you(application_code: str):
    application = JobApplication.query.filter_by(application_code=application_code).first_or_404()
    return render_template(
        "career_thank_you.html",
        application=application,
        status_label=_job_status_label(application.status),
        hide_staff_nav=True,
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


@bp.route("/recruitment", methods=["GET"])
@roles_required("admin", "manager")
def recruitment():
    section = (request.args.get("section") or "jobs").strip().lower()
    if section not in {"jobs", "applications"}:
        section = "jobs"
    edit_job_id = request.args.get("edit_job_id", type=int)
    jobs = JobOpening.query.options(joinedload(JobOpening.applications)).order_by(
        db.case((JobOpening.status == "published", 0), else_=1),
        JobOpening.created_at.desc(),
    ).all()
    applications = JobApplication.query.options(
        joinedload(JobApplication.job),
        joinedload(JobApplication.timeline_entries),
    ).order_by(JobApplication.created_at.desc()).all()
    application_cards = []
    for application in applications:
        application_cards.append(
            {
                "application": application,
                "skills": _application_skills(application),
                "files": {
                    "resume": bool(application.resume_file_path),
                    "photo": bool(application.photo_file_path),
                    "aadhaar": bool(application.aadhaar_file_path),
                    "license": bool(application.driving_license_file_path),
                    "certificates": bool(application.certificates_file_path),
                },
            }
        )
    editing_job = JobOpening.query.get(edit_job_id) if edit_job_id else None
    today = datetime.now(IST_TZ).date()
    stats = {
        "open_jobs": sum(1 for job in jobs if _job_is_publicly_open(job)),
        "applicants_today": sum(1 for app in applications if (app.created_at or datetime.utcnow()).date() == today),
        "interviews_today": sum(1 for app in applications if app.status in {"interview_scheduled", "interview_completed"}),
        "offers_pending": sum(1 for app in applications if app.status == "offer_sent"),
        "joining_today": sum(1 for app in applications if app.status == "joined"),
        "rejected": sum(1 for app in applications if app.status == "rejected"),
    }
    return render_template(
        "recruitment.html",
        section=section,
        jobs=jobs,
        applications=applications,
        application_cards=application_cards,
        job_cards=[_job_card_payload(job) for job in jobs],
        editing_job=editing_job,
        editing_job_skills=_job_skills(editing_job) if editing_job else [],
        stats=stats,
        employment_types=JOB_EMPLOYMENT_TYPES,
        location_types=JOB_LOCATION_TYPES,
        priority_types=JOB_PRIORITY_TYPES,
        education_types=JOB_EDUCATION_TYPES,
        experience_types=JOB_EXPERIENCE_TYPES,
        departments=PUBLIC_JOB_DEPARTMENTS,
        skill_options=RECRUITMENT_SKILL_OPTIONS,
        status_options=JOB_APPLICATION_STATUSES,
        status_label=_job_status_label,
    )


@bp.route("/recruitment/jobs/save", methods=["POST"])
@roles_required("admin", "manager")
def save_recruitment_job():
    job_id = request.form.get("job_id", type=int)
    title = (request.form.get("title") or "").strip()
    department = (request.form.get("department") or "").strip()
    if not title or not department:
        flash("Job title and department are required.", "error")
        target = url_for("main.recruitment", section="jobs")
        if job_id:
            target = url_for("main.recruitment", section="jobs", edit_job_id=job_id)
        return redirect(target)
    job = JobOpening.query.get(job_id) if job_id else JobOpening(career_slug="")
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("main.recruitment", section="jobs"))
    skill_values = []
    for skill in request.form.getlist("skills"):
        if (skill or "").strip():
            skill_values.append(skill.strip())
    for extra in (request.form.get("extra_skills") or "").split(","):
        if extra.strip():
            skill_values.append(extra.strip())
    salary_min_raw = (request.form.get("salary_min") or "").strip()
    salary_max_raw = (request.form.get("salary_max") or "").strip()
    try:
        salary_min = float(salary_min_raw) if salary_min_raw else None
        salary_max = float(salary_max_raw) if salary_max_raw else None
    except ValueError:
        flash("Salary values must be valid numbers.", "error")
        target = url_for("main.recruitment", section="jobs")
        if job_id:
            target = url_for("main.recruitment", section="jobs", edit_job_id=job_id)
        return redirect(target)
    slug_source = (request.form.get("career_slug") or title).strip()
    career_slug = _slugify_text(slug_source)
    existing_slug = JobOpening.query.filter(JobOpening.career_slug == career_slug, JobOpening.id != (job.id or 0)).first()
    if existing_slug:
        career_slug = f"{career_slug}-{uuid4().hex[:4]}"
    job.title = title
    job.department = department
    job.employment_type = (request.form.get("employment_type") or "Full Time").strip()
    job.location_type = (request.form.get("location_type") or "Brownberries Cafe").strip()
    job.salary_min = salary_min
    job.salary_max = salary_max
    job.salary_display = (request.form.get("salary_display") or "").strip() or None
    job.vacancies = max(1, request.form.get("vacancies", type=int) or 1)
    job.priority = (request.form.get("priority") or "Normal").strip()
    job.experience_required = (request.form.get("experience_required") or "").strip() or None
    job.education_required = (request.form.get("education_required") or "").strip() or None
    job.description = (request.form.get("description") or "").strip() or None
    job.responsibilities = (request.form.get("responsibilities") or "").strip() or None
    job.requirements = (request.form.get("requirements") or "").strip() or None
    job.benefits = (request.form.get("benefits") or "").strip() or None
    job.working_hours = (request.form.get("working_hours") or "").strip() or None
    job.weekly_off = (request.form.get("weekly_off") or "").strip() or None
    job.perks = (request.form.get("perks") or "").strip() or None
    job.growth_opportunities = (request.form.get("growth_opportunities") or "").strip() or None
    job.skills_json = _set_json_list(skill_values)
    job.require_resume = True if request.form.get("require_resume") else False
    job.require_cover_letter = True if request.form.get("require_cover_letter") else False
    job.require_photograph = True if request.form.get("require_photograph") else False
    job.require_aadhaar = True if request.form.get("require_aadhaar") else False
    job.require_driving_license = True if request.form.get("require_driving_license") else False
    job.auto_close_days = request.form.get("auto_close_days", type=int) or None
    job.max_applicants = request.form.get("max_applicants", type=int) or None
    job.status = (request.form.get("status") or "draft").strip().lower()
    job.career_slug = career_slug
    job.meta_title = (request.form.get("meta_title") or "").strip() or None
    job.meta_description = (request.form.get("meta_description") or "").strip() or None
    if job.status == "published" and not job.published_at:
        job.published_at = datetime.utcnow()
    if job.status != "published":
        job.archived_at = datetime.utcnow() if job.status == "archived" else None
    db.session.add(job)
    db.session.commit()
    flash("Job listing saved.", "success")
    return redirect(url_for("main.recruitment", section="jobs"))


@bp.route("/recruitment/jobs/<int:job_id>/action", methods=["POST"])
@roles_required("admin", "manager")
def recruitment_job_action(job_id: int):
    job = JobOpening.query.get_or_404(job_id)
    action = (request.form.get("action") or "").strip().lower()
    if action == "publish":
        job.status = "published"
        job.archived_at = None
        job.published_at = job.published_at or datetime.utcnow()
        flash("Job published.", "success")
    elif action == "hide":
        job.status = "hidden"
        flash("Job hidden from public listings.", "success")
    elif action == "archive":
        job.status = "archived"
        job.archived_at = datetime.utcnow()
        flash("Job archived.", "success")
    elif action == "duplicate":
        copy = JobOpening(
            title=f"{job.title} Copy",
            department=job.department,
            employment_type=job.employment_type,
            location_type=job.location_type,
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            salary_display=job.salary_display,
            vacancies=job.vacancies,
            priority=job.priority,
            experience_required=job.experience_required,
            education_required=job.education_required,
            description=job.description,
            responsibilities=job.responsibilities,
            requirements=job.requirements,
            benefits=job.benefits,
            working_hours=job.working_hours,
            weekly_off=job.weekly_off,
            perks=job.perks,
            growth_opportunities=job.growth_opportunities,
            skills_json=job.skills_json,
            require_resume=job.require_resume,
            require_cover_letter=job.require_cover_letter,
            require_photograph=job.require_photograph,
            require_aadhaar=job.require_aadhaar,
            require_driving_license=job.require_driving_license,
            auto_close_days=job.auto_close_days,
            max_applicants=job.max_applicants,
            status="draft",
            career_slug=_slugify_text(f"{job.title}-copy-{uuid4().hex[:4]}"),
            meta_title=job.meta_title,
            meta_description=job.meta_description,
        )
        db.session.add(copy)
        flash("Job duplicated as draft.", "success")
    elif action == "delete":
        if job.applications:
            flash("This job already has applications, so it was archived instead of deleted.", "error")
            job.status = "archived"
            job.archived_at = datetime.utcnow()
        else:
            db.session.delete(job)
            flash("Job deleted.", "success")
    db.session.commit()
    return redirect(url_for("main.recruitment", section="jobs"))


@bp.route("/recruitment/applications/<int:application_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def recruitment_application_update(application_id: int):
    application = JobApplication.query.options(joinedload(JobApplication.job)).get_or_404(application_id)
    status = (request.form.get("status") or application.status).strip().lower()
    note = (request.form.get("admin_notes") or "").strip()
    if status not in JOB_APPLICATION_STATUSES:
        status = application.status
    status_changed = status != application.status
    application.status = status
    application.admin_notes = note or None
    if status_changed:
        _append_application_timeline(application, status, note, g.current_user)
    elif note:
        _append_application_timeline(application, application.status, f"Note updated: {note}", g.current_user)
    db.session.commit()
    flash("Application updated.", "success")
    return redirect(url_for("main.recruitment", section="applications"))


@bp.route("/recruitment/applications/<int:application_id>/file/<string:file_kind>")
@roles_required("admin", "manager")
def recruitment_application_file(application_id: int, file_kind: str):
    application = JobApplication.query.get_or_404(application_id)
    mapping = {
        "resume": application.resume_file_path,
        "photo": application.photo_file_path,
        "aadhaar": application.aadhaar_file_path,
        "license": application.driving_license_file_path,
        "certificates": application.certificates_file_path,
    }
    rel_path = mapping.get(file_kind)
    if not rel_path:
        flash("Requested file was not found.", "error")
        return redirect(url_for("main.recruitment", section="applications"))
    full_path = os.path.join(current_app.config["UPLOADS_ROOT"], rel_path)
    if not os.path.exists(full_path):
        flash("Requested file is missing from storage.", "error")
        return redirect(url_for("main.recruitment", section="applications"))
    return send_file(full_path, as_attachment=True, download_name=os.path.basename(full_path))


@bp.route("/dashboard")
@login_required
def dashboard():
    today_start, today_end = _current_ist_day_bounds_utc_naive()
    active_loans = LibraryLoan.query.filter_by(status="issued").count()
    due_tomorrow = LibraryLoan.query.filter(
        LibraryLoan.status == "issued",
        LibraryLoan.due_date == db.func.date(db.func.datetime("now", "+1 day")),
    ).count()
    open_orders = CafeOrder.query.filter(
        CafeOrder.status.notin_(["paid", "cancelled"]),
        CafeOrder.created_at >= today_start,
        CafeOrder.created_at <= today_end,
    ).count()
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
            menu_items = MenuItem.query.filter(
                MenuItem.id.in_(item_ids),
                MenuItem.available.is_(True),
                MenuItem.is_deleted.is_(False),
            ).all() if item_ids else []
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
    menu_query = _apply_menu_category_filter(MenuItem.query.filter_by(available=True, is_deleted=False), category_id)
    if item_type:
        menu_query = menu_query.filter(MenuItem.item_type == item_type)
    menu_items = (
        menu_query.options(joinedload(MenuItem.category), joinedload(MenuItem.subcategory))
        .order_by(MenuItem.name.asc())
        .all()
    )
    all_category_rows = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    all_category_name_by_id = {c.id: c.name for c in all_category_rows}
    menu_items = [item for item in menu_items if _is_public_menu_item(item, all_category_name_by_id)]
    categories = _visible_categories_for_available_menu()
    category_name_by_id = {c.id: c.name for c in categories}
    item_category_names_map = {
        item.id: _get_item_category_names(item, category_name_by_id, include_protected=False) for item in menu_items
    }
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
    can_admin_view = user_has_any_role(current_user, "admin", "manager") or user_has_permission(current_user, "can_view_staff_profiles")
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
            profile.emergency_contact = request.form.get("emergency_contact", "").strip() or None
            profile.address = request.form.get("address", "").strip() or None
            if can_admin_view:
                profile.joining_date = date.fromisoformat(request.form["joining_date"]) if request.form.get("joining_date") else profile.joining_date
                profile.salary_type = request.form.get("salary_type", "").strip() or None
                salary_amount_raw = request.form.get("salary_amount", "").strip()
                if salary_amount_raw:
                    try:
                        profile.salary_amount = float(salary_amount_raw)
                    except ValueError:
                        flash("Salary amount must be a valid number.", "error")
                        return redirect(url_for("main.profile", section="personal", user_id=user.id if can_admin_view else None))
                profile.probation_end_date = date.fromisoformat(request.form["probation_end_date"]) if request.form.get("probation_end_date") else None
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
            if not can_admin_view:
                return redirect(url_for("main.profile"))
            selected_date = date.fromisoformat(request.form["attendance_date"]) if request.form.get("attendance_date") else date.today()
            status = (request.form.get("status") or "").strip()
            valid_statuses = {value for value, _ in ATTENDANCE_STATUS_OPTIONS if value}
            if status and status not in valid_statuses:
                status = ""
            notes = request.form.get("notes", "").strip() or None
            check_in_time = attendance_datetime_for(selected_date, request.form.get("check_in_time"))
            check_out_time = attendance_datetime_for(selected_date, request.form.get("check_out_time"))
            if check_in_time and check_out_time and check_out_time < check_in_time:
                flash("Check-out time cannot be before check-in time.", "error")
                return redirect(url_for("main.profile", section="attendance", user_id=user.id if can_admin_view else None))
            row = StaffAttendance.query.filter_by(user_id=user.id, attendance_date=selected_date).first()
            if not row:
                row = StaffAttendance(user_id=user.id, attendance_date=selected_date)
                db.session.add(row)
            row.check_in_at = check_in_time
            row.check_out_at = check_out_time
            row.notes = notes
            row.manager_override = True
            refresh_attendance_row(row, manual_status=status)
            db.session.commit()
            flash("Attendance override saved.", "success")
            return redirect(url_for("main.profile", section="attendance", user_id=user.id if can_admin_view else None))

        if action == "check_in":
            if current_user.id != user.id:
                return redirect(url_for("main.profile"))
            flash("Please use the staff attendance QR to check in from the cafe premises.", "error")
            return redirect(url_for("main.profile", section="attendance"))

        if action == "check_out":
            if current_user.id != user.id:
                return redirect(url_for("main.profile"))
            row = _active_attendance_session_for_user(user.id)
            if not row or not row.check_in_at:
                flash("No active check-in session found.", "error")
                return redirect(url_for("main.profile", section="attendance"))
            row.check_out_at = datetime.now()
            row.check_out_method = "profile"
            refresh_attendance_row(row)
            db.session.commit()
            flash("Check-out recorded.", "success")
            return redirect(url_for("main.profile", section="attendance"))

        if action == "leave_request":
            if current_user.id != user.id:
                return redirect(url_for("main.profile"))
            start_date = date.fromisoformat(request.form["start_date"])
            end_date = date.fromisoformat(request.form["end_date"])
            if end_date < start_date:
                flash("Leave end date cannot be before start date.", "error")
                return redirect(url_for("main.profile", section="attendance"))
            notice_warning = _leave_notice_message(start_date, end_date)
            db.session.add(
                StaffLeaveRequest(
                    user_id=user.id,
                    leave_type=request.form.get("leave_type", "casual").strip() or "casual",
                    start_date=start_date,
                    end_date=end_date,
                    reason=request.form.get("reason", "").strip() or None,
                    status="pending",
                    admin_remarks=notice_warning or None,
                )
            )
            db.session.commit()
            if notice_warning:
                flash(f"Leave request submitted. Notice warning: {notice_warning}", "error")
            else:
                flash("Leave request submitted.", "success")
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
                    released_by_admin=True if user_has_any_role(current_user, "admin", "manager") else False,
                    verification_status="verified" if user_has_any_role(current_user, "admin", "manager") else "pending",
                    verification_note="Uploaded by admin/manager" if user_has_any_role(current_user, "admin", "manager") else None,
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
            verification_status = (request.form.get("verification_status") or "pending").strip().lower()
            if verification_status not in ["pending", "verified", "rejected"]:
                verification_status = "pending"
            doc.verification_status = verification_status
            doc.verification_note = request.form.get("verification_note", "").strip() or None
            db.session.commit()
            flash("Document verification status updated.", "success")
            return redirect(url_for("main.profile", section="documents", user_id=user.id))

        if action == "upload_salary_receipt":
            if not (user_has_any_role(current_user, "admin", "manager") or user_has_permission(current_user, "can_upload_salary")):
                return redirect(url_for("main.profile"))
            receipt_file = request.files.get("receipt_file")
            target_id = int(request.form["target_user_id"])
            target_user = User.query.get_or_404(target_id)
            if not receipt_file or not receipt_file.filename:
                flash("Please select salary receipt file.", "error")
                return redirect(url_for("main.profile", section="salary", user_id=target_user.id))
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
            return redirect(url_for("main.profile", section="salary", user_id=target_user.id))

    summary_month = request.args.get("month", type=int) or date.today().month
    summary_year = request.args.get("year", type=int) or date.today().year
    if summary_month < 1 or summary_month > 12:
        summary_month = date.today().month
    if summary_year < 2000 or summary_year > 2100:
        summary_year = date.today().year
    month_end_day = calendar.monthrange(summary_year, summary_month)[1]
    month_start = date(summary_year, summary_month, 1)
    month_end = date(summary_year, summary_month, month_end_day)
    attendance_logs = (
        StaffAttendance.query.filter_by(user_id=user.id)
        .order_by(StaffAttendance.attendance_date.desc())
        .limit(60)
        .all()
    )
    month_attendance_logs = (
        StaffAttendance.query.filter(
            StaffAttendance.user_id == user.id,
            StaffAttendance.attendance_date >= month_start,
            StaffAttendance.attendance_date <= month_end,
        )
        .order_by(StaffAttendance.attendance_date.desc())
        .all()
    )
    attendance_rows_to_refresh = {row.id: row for row in attendance_logs + month_attendance_logs if row.id}
    attendance_changed = False
    for row in attendance_rows_to_refresh.values():
        before = row.status
        refresh_attendance_row(row, manual_status=before if (row.manager_override and not (row.check_in_at or row.check_out_at)) else None)
        if row.status != before:
            attendance_changed = True
    if attendance_changed:
        db.session.commit()
    attendance_summary = build_attendance_summary(month_attendance_logs)
    salary_summary = _build_salary_summary(profile, month_attendance_logs, month_start)
    receipts = (
        CafeOrder.query.filter_by(ordered_by_user_id=user.id, status="paid")
        .order_by(CafeOrder.created_at.desc())
        .limit(40)
        .all()
    )
    library_payments = (
        LibraryPayment.query.order_by(LibraryPayment.created_at.desc()).limit(20).all()
        if user_has_any_role(user, "admin", "manager", "librarian")
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
    leave_logs = (
        StaffLeaveRequest.query.filter_by(user_id=user.id)
        .order_by(StaffLeaveRequest.created_at.desc())
        .limit(40)
        .all()
    )
    today_attendance = next((row for row in attendance_logs if row.attendance_date == date.today()), None)
    active_attendance_session = _active_attendance_session_for_user(user.id)
    next_attendance_action = "completed"
    if active_attendance_session and active_attendance_session.check_in_at and not active_attendance_session.check_out_at:
        next_attendance_action = "check_out"
    elif not today_attendance or not today_attendance.check_in_at:
        next_attendance_action = "check_in"
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
        month_attendance_logs=month_attendance_logs,
        attendance_summary=attendance_summary,
        salary_summary=salary_summary,
        leave_logs=leave_logs,
        attendance_status_options=ATTENDANCE_STATUS_OPTIONS,
        leave_type_options=SELF_LEAVE_TYPE_OPTIONS,
        attendance_status_label=attendance_status_label,
        attendance_flags_for_row=attendance_flags_for_row,
        worked_hours_for_row=worked_hours_for_row,
        today_attendance=today_attendance,
        active_attendance_session=active_attendance_session,
        attendance_elapsed_text=_attendance_elapsed_text,
        attendance_source_label=_attendance_source_label,
        next_attendance_action=next_attendance_action,
        late_penalty_days=late_penalty_days,
        active_section=active_section,
        summary_month=summary_month,
        summary_year=summary_year,
    )


@bp.route("/staff/attendance/check-in", methods=["GET", "POST"])
@login_required
def staff_attendance_check_in():
    user = g.current_user
    existing_active = _active_attendance_session_for_user(user.id)
    settings = _attendance_settings()
    if request.method == "POST":
        if existing_active and existing_active.check_in_at and not existing_active.check_out_at:
            flash("You already have an active check-in session. Please check out from your profile.", "error")
            return redirect(url_for("main.profile", section="attendance"))
        today = date.today()
        completed_today = StaffAttendance.query.filter_by(user_id=user.id, attendance_date=today).first()
        if completed_today and completed_today.check_in_at and completed_today.check_out_at:
            flash("Today's attendance is already completed. Ask admin if a correction is needed.", "error")
            return redirect(url_for("main.profile", section="attendance"))
        try:
            check_in_lat = float((request.form.get("check_in_lat") or "").strip())
            check_in_lng = float((request.form.get("check_in_lng") or "").strip())
        except (TypeError, ValueError):
            flash("Location could not be captured. Please allow location access and try again.", "error")
            return redirect(url_for("main.staff_attendance_check_in"))
        distance_m = _attendance_distance_from_cafe(check_in_lat, check_in_lng)
        if distance_m is None or distance_m > settings["radius_m"]:
            flash(
                f"You need to be inside the cafe geofence to check in. Current distance: {distance_m:.0f} m."
                if distance_m is not None
                else "You need to be inside the cafe geofence to check in.",
                "error",
            )
            return redirect(url_for("main.staff_attendance_check_in"))
        row = completed_today
        if not row:
            row = StaffAttendance(user_id=user.id, attendance_date=today)
            db.session.add(row)
        row.check_in_at = datetime.now()
        row.check_out_at = None
        row.manager_override = False
        row.check_in_lat = check_in_lat
        row.check_in_lng = check_in_lng
        row.check_in_distance_m = round(distance_m, 2)
        row.check_in_method = "qr_geofence"
        row.check_out_method = None
        refresh_attendance_row(row)
        db.session.commit()
        flash("Check-in recorded from cafe premises.", "success")
        return redirect(url_for("main.profile", section="attendance"))
    return render_template(
        "staff_attendance_qr.html",
        active_attendance_session=existing_active,
        attendance_elapsed_text=_attendance_elapsed_text,
        cafe_lat=settings["cafe_lat"],
        cafe_lng=settings["cafe_lng"],
        attendance_radius_m=settings["radius_m"],
    )


@bp.route("/profile/documents/<string:doc_type>")
@login_required
def profile_document(doc_type):
    current_user = g.current_user
    doc_id = request.args.get("doc_id", type=int)
    receipt_id = request.args.get("receipt_id", type=int)
    can_admin_view = user_has_any_role(current_user, "admin", "manager") or user_has_permission(current_user, "can_view_staff_profiles")

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
        return render_template(
            "table_qr.html",
            table=None,
            menu_items=[],
            categories=[],
            item_frequency={},
            hide_staff_nav=True,
        )
    categories = _visible_categories_for_available_menu()
    all_category_rows = MenuCategory.query.order_by(MenuCategory.name.asc()).all()
    all_category_name_by_id = {c.id: c.name for c in all_category_rows}
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
        MenuItem.query.filter_by(available=True, is_deleted=False).outerjoin(frequency_subq, MenuItem.id == frequency_subq.c.menu_item_id)
        .options(joinedload(MenuItem.category), joinedload(MenuItem.subcategory))
        .add_columns(db.func.coalesce(frequency_subq.c.order_qty, 0).label("order_qty"))
        .order_by(db.desc(db.func.coalesce(frequency_subq.c.order_qty, 0)), MenuItem.name.asc())
    )
    ranked_rows = ranked_query.all()
    menu_items = [row[0] for row in ranked_rows if _is_public_menu_item(row[0], all_category_name_by_id)]
    item_frequency = {
        row[0].id: int(row[1] or 0)
        for row in ranked_rows
        if _is_public_menu_item(row[0], all_category_name_by_id)
    }
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
        item_category_names_map[item.id] = _get_item_category_names(item, category_name_by_id, include_protected=False)

    table_orders = []
    staff_call_cooldown_remaining = 0
    if table:
        today_start, today_end = _current_ist_day_bounds_utc_naive()
        table_orders = (
            CafeOrder.query.options(joinedload(CafeOrder.order_items).joinedload(CafeOrderItem.menu_item))
            .filter(
                CafeOrder.table_id == table.id,
                CafeOrder.status.notin_(["paid", "cancelled"]),
                CafeOrder.created_at >= today_start,
                CafeOrder.created_at <= today_end,
            )
            .order_by(CafeOrder.created_at.desc())
            .all()
        )
        staff_call_cooldown_remaining = _staff_call_cooldown_remaining_seconds(table)
    qr_success_toast = session.pop("qr_success_toast", None)
    service_charge_rate = _service_charge_rate_for_public()
    feedback_prompt = _table_feedback_prompt(table) if table else None
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
        staff_call_cooldown_remaining=staff_call_cooldown_remaining,
        service_charge_rate=service_charge_rate,
        service_charge_enabled=not bool(getattr(table, "service_charge_opt_out_requested", False)),
        feedback_prompt=feedback_prompt,
        qr_success_toast=qr_success_toast,
        hide_staff_nav=True,
    )


@bp.route("/table/service-charge-preference", methods=["POST"])
def table_service_charge_preference():
    slug = (request.form.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        return jsonify({"ok": False, "message": "Table not found."}), 404
    include_service_charge = request.form.get("apply_service_charge") == "1"
    table.service_charge_opt_out_requested = not include_service_charge
    db.session.commit()
    return jsonify({"ok": True, "apply_service_charge": include_service_charge})


@bp.route("/table/feedback-status")
def table_feedback_status():
    slug = (request.args.get("slug") or "").strip()
    table = CafeTable.query.filter_by(qr_slug=slug, active=True).first()
    if not table:
        return jsonify({"ok": False, "message": "Table not found."}), 404
    return jsonify({"ok": True, "feedback_prompt": _table_feedback_prompt(table)})


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
    user = User.query.get_or_404(user_id)
    if _is_cafe_admin(user):
        flash("Cafe Admin cannot be deleted.", "error")
        return redirect(url_for("cafe.staff"))
    if user.staff_profile:
        db.session.delete(user.staff_profile)
    StaffAttendance.query.filter_by(user_id=user.id).delete()
    StaffLeaveRequest.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash("Staff member deleted.", "success")
    return redirect(url_for("cafe.staff"))
