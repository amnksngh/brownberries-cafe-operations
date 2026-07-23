"""Database-backed leave policy and attendance integration helpers.

The existing StaffLeaveRequest table remains the application record. This
module adds policy, balance, and ledger behavior around it so older requests
remain readable and every new balance change is auditable.
"""

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .extensions import db
from .models import (
    CompanyHoliday,
    LeaveBalance,
    LeavePolicy,
    LeaveTransaction,
    LeaveType,
    RoleLeaveRule,
    StaffAttendance,
    StaffLeaveRequest,
    User,
    WeeklyOffConfig,
)


IST = ZoneInfo("Asia/Kolkata")
VALID_SHIFTS = {"first_half", "second_half"}
SUPPORTED_LEAVE_TYPES = {"earned", "urgent"}


def business_today() -> date:
    return datetime.now(IST).date()


def leave_policy() -> LeavePolicy:
    policy = LeavePolicy.query.get(1)
    if not policy:
        policy = LeavePolicy(id=1)
        db.session.add(policy)
        db.session.flush()
    return policy


def weekly_off_config() -> WeeklyOffConfig:
    config = WeeklyOffConfig.query.get(1)
    if not config:
        config = WeeklyOffConfig(id=1)
        db.session.add(config)
        db.session.flush()
    return config


def ensure_leave_defaults() -> None:
    for code, name, paid in (
        ("earned", "Earned Leave", True),
        ("urgent", "Urgent Leave", False),
    ):
        leave_type = LeaveType.query.filter_by(code=code).first()
        if not leave_type:
            db.session.add(LeaveType(code=code, name=name, paid=paid, uses_balance=True, active=True))
    leave_policy()
    weekly_off_config()
    role_names = set()
    for user in User.query.all():
        role_names.update(user.assigned_roles())
    for role_name in sorted(role_names):
        if not role_name:
            continue
        if not RoleLeaveRule.query.filter_by(role_name=role_name).first():
            db.session.add(RoleLeaveRule(role_name=role_name, enabled=True, max_concurrent_on_leave=1, cooldown_days=1))
    db.session.commit()


def _eligible_users():
    return (
        User.query.filter(User.active.is_(True))
        .order_by(User.full_name.asc())
        .all()
    )


def ensure_leave_balance(user: User) -> LeaveBalance:
    balance = LeaveBalance.query.filter_by(user_id=user.id).first()
    if not balance:
        balance = LeaveBalance(user_id=user.id, earned_balance=0, urgent_balance=0)
        db.session.add(balance)
        db.session.flush()
        db.session.add(
            LeaveTransaction(
                user_id=user.id,
                leave_type="earned",
                amount=0,
                transaction_type="opening_balance",
                period_key=f"opening:{business_today().isoformat()}",
                note="Initial leave balance",
            )
        )
    return balance


def _month_dates(start: date, end: date):
    cursor = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while cursor <= final:
        yield cursor
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


def _credit_once(user: User, balance: LeaveBalance, leave_type: str, amount: float, period_key: str, note: str):
    if amount <= 0 or LeaveTransaction.query.filter_by(user_id=user.id, period_key=period_key).first():
        return False
    if leave_type == "earned":
        balance.earned_balance = round(float(balance.earned_balance or 0) + amount, 2)
    else:
        balance.urgent_balance = round(float(balance.urgent_balance or 0) + amount, 2)
    db.session.add(
        LeaveTransaction(
            user_id=user.id,
            leave_type=leave_type,
            amount=amount,
            transaction_type="credit",
            period_key=period_key,
            note=note,
        )
    )
    return True


def run_leave_maintenance(as_of: date | None = None) -> None:
    """Run idempotent leave credits and annual urgent reset.

    This runs at application startup and is also exposed as a CLI command. A
    period key makes retries safe after a restart or a temporary outage.
    """
    as_of = as_of or business_today()
    policy = leave_policy()
    weekly_off_config()
    changed = False
    for user in _eligible_users():
        profile = getattr(user, "staff_profile", None)
        if not profile or profile.archived:
            continue
        balance = ensure_leave_balance(user)
        joining_date = profile.joining_date or as_of
        created_date = balance.created_at.date() if balance.created_at else as_of
        credit_start = max(joining_date, created_date)
        for month in _month_dates(credit_start, as_of):
            month_end = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
            if credit_start <= date(month.year, month.month, 15) <= as_of:
                changed = _credit_once(
                    user,
                    balance,
                    "earned",
                    float(policy.monthly_earned_credit or 0),
                    f"earned:{month.year:04d}-{month.month:02d}-15",
                    "Monthly earned leave credit",
                ) or changed
            if credit_start <= month_end <= as_of:
                changed = _credit_once(
                    user,
                    balance,
                    "earned",
                    float(policy.month_end_earned_credit or 0),
                    f"earned:{month.year:04d}-{month.month:02d}-end",
                    "End-of-month earned leave credit",
                ) or changed

        if balance.urgent_year != as_of.year:
            probation_complete = not policy.urgent_requires_probation or (
                profile.probation_end_date is not None and profile.probation_end_date <= as_of
            )
            if probation_complete:
                previous = float(balance.urgent_balance or 0)
                target = float(policy.urgent_leaves_per_year or 0)
                balance.urgent_balance = target
                balance.urgent_year = as_of.year
                db.session.add(
                    LeaveTransaction(
                        user_id=user.id,
                        leave_type="urgent",
                        amount=round(target - previous, 2),
                        transaction_type="annual_reset",
                        period_key=f"urgent:{as_of.year}",
                        note="Annual urgent leave allocation after probation",
                    )
                )
                changed = True
    if changed:
        db.session.commit()
    else:
        db.session.commit()


def calculate_leave_duration(start_date: date, end_date: date, from_shift: str, to_shift: str) -> float:
    start_index = 0 if from_shift == "first_half" else 1
    end_index = 0 if to_shift == "first_half" else 1
    half_units = ((end_date - start_date).days * 2) + end_index - start_index + 1
    return round(max(0.5, half_units / 2), 2)


def _request_bounds(leave: StaffLeaveRequest):
    return _date_shift_bounds(leave.start_date, leave.end_date, leave.from_shift, leave.to_shift)


def _date_shift_bounds(start_date: date, end_date: date, from_shift: str, to_shift: str):
    start_index = 0 if from_shift == "first_half" else 1
    end_index = 0 if to_shift == "first_half" else 1
    return (start_date.toordinal() * 2 + start_index, end_date.toordinal() * 2 + end_index)


def _bounds_overlap(left, right) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _roles_overlap(user_a: User, user_b: User) -> bool:
    return bool(set(user_a.assigned_roles()) & set(user_b.assigned_roles()))


def _holiday_map(start_date: date, end_date: date):
    return {
        holiday.holiday_date: holiday.name
        for holiday in CompanyHoliday.query.filter(
            CompanyHoliday.active.is_(True),
            CompanyHoliday.holiday_date >= start_date,
            CompanyHoliday.holiday_date <= end_date,
        ).all()
    }


def _is_weekly_off(day: date, config: WeeklyOffConfig) -> bool:
    return bool(config.enabled and day.weekday() == int(config.weekday))


def _approved_requests(exclude_user_id: int | None = None):
    query = StaffLeaveRequest.query.filter_by(status="approved")
    if exclude_user_id:
        query = query.filter(StaffLeaveRequest.user_id != exclude_user_id)
    return query.all()


def validate_leave_request(
    user: User,
    leave_type: str,
    start_date: date,
    end_date: date,
    from_shift: str = "first_half",
    to_shift: str = "second_half",
    exclude_request_id: int | None = None,
):
    errors: list[str] = []
    leave_type = (leave_type or "").strip().lower()
    from_shift = (from_shift or "first_half").strip().lower()
    to_shift = (to_shift or "second_half").strip().lower()
    if leave_type not in SUPPORTED_LEAVE_TYPES:
        errors.append("Select Earned Leave or Urgent Leave.")
    if from_shift not in VALID_SHIFTS or to_shift not in VALID_SHIFTS:
        errors.append("Select valid first-half or second-half shifts.")
    if end_date < start_date:
        errors.append("Leave end date cannot be before the start date.")
        return errors, 0.0
    duration = calculate_leave_duration(start_date, end_date, from_shift, to_shift)
    policy = leave_policy()
    if (end_date - start_date).days + 1 > int(policy.max_continuous_days or 7):
        errors.append(f"Leave cannot exceed {policy.max_continuous_days} consecutive calendar days.")
    config = weekly_off_config()
    holiday_map = _holiday_map(start_date, end_date)
    blocked_special_days = []
    cursor = start_date
    while cursor <= end_date:
        if cursor in holiday_map:
            blocked_special_days.append(f"{cursor}: {holiday_map[cursor]}")
        elif _is_weekly_off(cursor, config):
            blocked_special_days.append(f"{cursor}: {config.label}")
        cursor += timedelta(days=1)
    if blocked_special_days:
        errors.append("Leave cannot be requested on company holidays or weekly offs: " + ", ".join(blocked_special_days))

    balance = ensure_leave_balance(user)
    if leave_type == "earned" and float(balance.earned_balance or 0) + 1e-9 < duration:
        errors.append(f"Earned Leave balance is only {balance.earned_balance:.1f} day(s).")
    if leave_type == "urgent":
        if policy.urgent_requires_probation:
            profile = getattr(user, "staff_profile", None)
            if not profile or not profile.probation_end_date or profile.probation_end_date > start_date:
                errors.append("Urgent Leave is available only after probation is completed.")
        if float(balance.urgent_balance or 0) + 1e-9 < duration:
            errors.append(f"Urgent Leave balance is only {balance.urgent_balance:.1f} day(s).")
        month_totals = {}
        for existing in StaffLeaveRequest.query.filter(
            StaffLeaveRequest.user_id == user.id,
            StaffLeaveRequest.leave_type == "urgent",
            StaffLeaveRequest.status.in_(["pending", "approved"]),
        ).all():
            if exclude_request_id and existing.id == exclude_request_id:
                continue
            month_totals[existing.start_date.strftime("%Y-%m")] = month_totals.get(existing.start_date.strftime("%Y-%m"), 0) + float(existing.duration_days or 0)
        month_totals[start_date.strftime("%Y-%m")] = month_totals.get(start_date.strftime("%Y-%m"), 0) + duration
        if any(total > float(policy.max_monthly_urgent_leaves or 3) + 1e-9 for total in month_totals.values()):
            errors.append(f"Urgent Leave is limited to {policy.max_monthly_urgent_leaves:g} day(s) per calendar month.")

    candidate_bounds = _date_shift_bounds(start_date, end_date, from_shift, to_shift)
    approved = [row for row in _approved_requests(exclude_user_id=user.id) if not (exclude_request_id and row.id == exclude_request_id)]
    for existing in approved:
        if _bounds_overlap(candidate_bounds, _request_bounds(existing)):
            errors.append("You already have approved leave overlapping these dates.")
            break

    # No more than the configured number of distinct employees may be absent
    # on any date of the requested period.
    for day_offset in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=day_offset)
        day_bounds = _date_shift_bounds(day, day, "first_half", "second_half")
        absent_users = {
            row.user_id
            for row in approved
            if _bounds_overlap(day_bounds, _request_bounds(row))
        }
        if len(absent_users) >= int(policy.max_company_leaves or 2):
            errors.append(f"A maximum of {policy.max_company_leaves} employees can be on leave at the same time.")
            break

    role_rules = {rule.role_name: rule for rule in RoleLeaveRule.query.filter_by(enabled=True).all()}
    shared_roles = set(user.assigned_roles())
    for role in shared_roles:
        rule = role_rules.get(role)
        if not rule:
            continue
        matching = [
            row for row in approved
            if row.user and row.user_id != user.id and row.user.has_role(role)
        ]
        if not matching:
            continue
        cooldown = int(rule.cooldown_days if rule.cooldown_days is not None else policy.role_cooldown_days or 0)
        max_concurrent = max(1, int(rule.max_concurrent_on_leave or 1))
        for day_offset in range((end_date - start_date).days + 1):
            day = start_date + timedelta(days=day_offset)
            day_bounds = _date_shift_bounds(day, day, "first_half", "second_half")
            role_absent_users = {
                row.user_id
                for row in matching
                if _bounds_overlap(
                    day_bounds,
                    _date_shift_bounds(
                        row.start_date,
                        row.end_date + timedelta(days=cooldown),
                        row.from_shift,
                        "second_half",
                    ),
                )
            }
            if len(role_absent_users) >= max_concurrent:
                errors.append(
                    f"Role restriction: at most {max_concurrent} {role.replace('_', ' ')} staff member(s) can be away, including the {cooldown}-day cooldown."
                )
                break
        if errors and errors[-1].startswith("Role restriction:"):
            break
    return list(dict.fromkeys(errors)), duration


def leave_availability(user: User, start_date: date, end_date: date):
    availability = {}
    cursor = start_date
    while cursor <= end_date:
        errors, _ = validate_leave_request(user, "earned", cursor, cursor, "first_half", "first_half")
        availability[cursor.isoformat()] = {
            "available": not errors,
            "reason": errors[0] if errors else "Leave can be requested",
        }
        cursor += timedelta(days=1)
    return availability


def _attendance_status_for_day(leave: StaffLeaveRequest, day: date) -> str:
    covers_first = day > leave.start_date or leave.from_shift == "first_half"
    covers_second = day < leave.end_date or leave.to_shift == "second_half"
    if covers_first and covers_second:
        if leave.leave_type == "earned":
            return "earned_leave"
        if leave.leave_type == "urgent":
            return "urgent_leave"
        return "on_leave"
    if leave.leave_type == "earned":
        return "half_day_earned_leave"
    if leave.leave_type == "urgent":
        return "half_day_urgent_leave"
    return "half_day_leave"


def _sync_leave_attendance(leave: StaffLeaveRequest):
    cursor = leave.start_date
    while cursor <= leave.end_date:
        row = StaffAttendance.query.filter_by(user_id=leave.user_id, attendance_date=cursor).first()
        if not row:
            row = StaffAttendance(user_id=leave.user_id, attendance_date=cursor)
            db.session.add(row)
        row.check_in_at = None
        row.check_out_at = None
        row.status = _attendance_status_for_day(leave, cursor)
        row.manager_override = True
        row.notes = f"Leave request #{leave.id}"
        cursor += timedelta(days=1)


def _remove_leave_attendance(leave: StaffLeaveRequest):
    rows = StaffAttendance.query.filter(
        StaffAttendance.user_id == leave.user_id,
        StaffAttendance.attendance_date >= leave.start_date,
        StaffAttendance.attendance_date <= leave.end_date,
        StaffAttendance.notes.like(f"Leave request #{leave.id}%"),
    ).all()
    for row in rows:
        db.session.delete(row)


def apply_leave_decision(leave: StaffLeaveRequest, decision: str, actor: User):
    decision = (decision or "pending").strip().lower()
    if decision not in {"pending", "approved", "rejected", "requested_changes", "cancelled"}:
        return False, ["Invalid leave decision."]
    old_status = leave.status
    if decision == "approved":
        if leave.leave_type in SUPPORTED_LEAVE_TYPES:
            errors, duration = validate_leave_request(
                leave.user,
                leave.leave_type,
                leave.start_date,
                leave.end_date,
                leave.from_shift,
                leave.to_shift,
                exclude_request_id=leave.id,
            )
            if errors:
                return False, errors
        else:
            # Preserve approvals from the pre-policy leave form without
            # charging them against the new earned/urgent balances.
            duration = float(leave.duration_days or calculate_leave_duration(
                leave.start_date, leave.end_date, leave.from_shift, leave.to_shift
            ))
        balance = ensure_leave_balance(leave.user)
        if old_status != "approved":
            if leave.leave_type == "earned":
                balance.earned_balance = round(float(balance.earned_balance or 0) - duration, 2)
            elif leave.leave_type == "urgent":
                balance.urgent_balance = round(float(balance.urgent_balance or 0) - duration, 2)
            if leave.leave_type in SUPPORTED_LEAVE_TYPES:
                db.session.add(
                    LeaveTransaction(
                        user_id=leave.user_id,
                        leave_request_id=leave.id,
                        leave_type=leave.leave_type,
                        amount=-duration,
                        transaction_type="debit",
                        note="Approved leave",
                        created_by_user_id=actor.id,
                    )
                )
        leave.duration_days = duration
        leave.debited_amount = duration
        leave.status = "approved"
        leave.approved_at = datetime.utcnow()
        leave.decided_by_user_id = actor.id
        _sync_leave_attendance(leave)
    elif old_status == "approved" and decision != "approved":
        balance = ensure_leave_balance(leave.user)
        amount = float(leave.debited_amount or leave.duration_days or 0)
        if leave.leave_type == "earned":
            balance.earned_balance = round(float(balance.earned_balance or 0) + amount, 2)
        elif leave.leave_type == "urgent":
            balance.urgent_balance = round(float(balance.urgent_balance or 0) + amount, 2)
        db.session.add(
            LeaveTransaction(
                user_id=leave.user_id,
                leave_request_id=leave.id,
                leave_type=leave.leave_type,
                amount=amount,
                transaction_type="refund",
                note=f"Leave status changed to {decision}",
                created_by_user_id=actor.id,
            )
        )
        leave.debited_amount = 0
        _remove_leave_attendance(leave)
        leave.status = decision
        leave.decided_by_user_id = actor.id
    else:
        leave.status = decision
        leave.decided_by_user_id = actor.id
    return True, []


def leave_dashboard_context(user: User, month: int | None = None, year: int | None = None):
    run_leave_maintenance()
    today = business_today()
    month = month or today.month
    year = year or today.year
    month = month if 1 <= month <= 12 else today.month
    year = year if 2000 <= year <= 2100 else today.year
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    balance = ensure_leave_balance(user)
    holidays = CompanyHoliday.query.filter(
        CompanyHoliday.active.is_(True), CompanyHoliday.holiday_date >= today, CompanyHoliday.holiday_date <= today + timedelta(days=90)
    ).order_by(CompanyHoliday.holiday_date.asc()).all()
    weekly = weekly_off_config()
    return {
        "leave_balance": balance,
        "leave_policy": leave_policy(),
        "leave_availability": leave_availability(user, month_start, month_end),
        "leave_holidays": holidays,
        "weekly_off_config": weekly,
        "leave_month": month,
        "leave_year": year,
        "leave_month_rows": calendar.Calendar(firstweekday=0).monthdayscalendar(year, month),
    }
