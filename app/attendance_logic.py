import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace


ATTENDANCE_STATUS_LABELS = {
    "present_all_day": "Present All Day",
    "first_half": "Half Day",
    "second_half": "Half Day",
    "short_attendance": "Short Attendance",
    "pending_correction": "Pending Correction",
    "on_leave": "On Leave",
    "earned_leave": "Earned Leave",
    "urgent_leave": "Urgent Leave",
    "half_day_leave": "Half Day Leave",
    "half_day_earned_leave": "Half Day Earned Leave",
    "half_day_urgent_leave": "Half Day Urgent Leave",
    "sick_leave": "Sick Leave",
    "weekly_off": "Weekly Off",
    "company_holiday": "Company Holiday",
    "late_entry": "Late Entry",
    "early_exit": "Early Exit",
    "missed_checkout": "Missed Checkout",
    "absent": "Absent",
}

ATTENDANCE_STATUS_OPTIONS = [
    ("", "Auto Calculate"),
    ("present_all_day", "Present All Day"),
    ("first_half", "Half Day"),
    ("short_attendance", "Short Attendance"),
    ("pending_correction", "Pending Correction"),
    ("on_leave", "On Leave"),
    ("earned_leave", "Earned Leave"),
    ("urgent_leave", "Urgent Leave / Absent"),
    ("half_day_leave", "Half Day Leave"),
    ("sick_leave", "Sick Leave"),
    ("weekly_off", "Weekly Off"),
    ("company_holiday", "Company Holiday"),
    ("absent", "Absent"),
]

SELF_LEAVE_TYPE_OPTIONS = [
    ("earned", "Earned Leave"),
    ("urgent", "Urgent Leave"),
]

SHIFT_START = time(9, 0)
SHIFT_END = time(18, 0)
SHIFT_REQUIRED_MINUTES = 510
LATE_GRACE_MINUTES = 10
EARLY_EXIT_GRACE_MINUTES = 10
PRESENT_MINUTES = math.ceil(SHIFT_REQUIRED_MINUTES * 0.85)
HALF_DAY_MINUTES = math.ceil(SHIFT_REQUIRED_MINUTES * 0.50)
SHORT_ATTENDANCE_MINUTES = math.ceil(SHIFT_REQUIRED_MINUTES * 0.25)
MANUAL_ONLY_STATUSES = {"on_leave", "sick_leave", "weekly_off", "absent"}


def shift_window_for_profile(profile) -> tuple[time, time]:
    start = getattr(profile, "shift_start_time", None) or SHIFT_START
    end = getattr(profile, "shift_end_time", None) or SHIFT_END
    return start, end


def shift_window_for_row(row) -> tuple[time, time]:
    profile = getattr(getattr(row, "user", None), "staff_profile", None)
    return shift_window_for_profile(profile)


def shift_required_minutes_for_row(row) -> int:
    shift_start, shift_end = shift_window_for_row(row)
    start_minutes = (shift_start.hour * 60) + shift_start.minute
    end_minutes = (shift_end.hour * 60) + shift_end.minute
    span_minutes = max(60, end_minutes - start_minutes)
    default_span = max(60, ((SHIFT_END.hour * 60) + SHIFT_END.minute) - ((SHIFT_START.hour * 60) + SHIFT_START.minute))
    ratio = SHIFT_REQUIRED_MINUTES / default_span
    return max(30, math.ceil(span_minutes * ratio))


def attendance_status_label(status: str | None) -> str:
    if not status:
        return "-"
    return ATTENDANCE_STATUS_LABELS.get(status, status.replace("_", " ").title())


def attendance_datetime_for(date_value: date, value: str | None):
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    parsed = datetime.strptime(raw, "%H:%M")
    return datetime.combine(date_value, parsed.time())


def worked_minutes_for_row(row, *, now: datetime | None = None) -> int:
    override = getattr(row, "_worked_minutes_override", None) if row else None
    if override is not None:
        return max(0, int(override))
    if not row or not row.check_in_at:
        return 0
    end = row.check_out_at or now
    if not end:
        return 0
    return max(0, int((end - row.check_in_at).total_seconds() // 60))


def worked_hours_for_row(row) -> float:
    return round(worked_minutes_for_row(row) / 60, 2)


def calculate_status_from_worked_minutes(row, worked_minutes: int, *, has_open_session: bool = False) -> str:
    """Classify a whole day from the sum of all its attendance sessions."""
    if not row:
        return "absent"
    if has_open_session:
        return "pending_correction"
    required_minutes = shift_required_minutes_for_row(row)
    present_minutes = math.ceil(required_minutes * 0.85)
    half_day_minutes = math.ceil(required_minutes * 0.50)
    short_attendance_minutes = math.ceil(required_minutes * 0.25)
    if worked_minutes >= present_minutes:
        return "present_all_day"
    if worked_minutes >= half_day_minutes:
        return "first_half"
    if worked_minutes >= short_attendance_minutes:
        return "short_attendance"
    return "absent"


def aggregate_attendance_rows(rows: list, *, now: datetime | None = None) -> list:
    """Return one report row per user/day without discarding split sessions.

    Geofence attendance intentionally stores each check-in/check-out interval.
    The web and mobile reports need a daily view, so this creates a lightweight
    report object with the first check-in, final check-out, total minutes, and
    the original ``session_rows`` for timeline rendering.
    """
    now = now or datetime.now()
    groups = defaultdict(list)
    for row in rows or []:
        if row and row.attendance_date is not None:
            groups[(row.user_id, row.attendance_date)].append(row)

    aggregated = []
    for _, sessions in sorted(groups.items(), key=lambda item: item[0][1], reverse=True):
        sessions.sort(key=lambda row: (row.check_in_at or datetime.min, row.id or 0))
        first = sessions[0]
        checkins = [row.check_in_at for row in sessions if row.check_in_at]
        checkouts = [row.check_out_at for row in sessions if row.check_out_at]
        has_open_session = any(row.check_in_at and not row.check_out_at for row in sessions)
        total_minutes = sum(worked_minutes_for_row(row, now=now) for row in sessions)
        manual_rows = [row for row in sessions if getattr(row, "manager_override", False)]
        status_row = manual_rows[-1] if manual_rows else first
        status = (
            status_row.status
            if manual_rows and status_row.status
            else calculate_status_from_worked_minutes(
                status_row,
                total_minutes,
                has_open_session=has_open_session,
            )
        )
        last = sessions[-1]
        reasons = []
        for row in sessions:
            reason = (getattr(row, "auto_checkout_reason", None) or "").strip()
            if reason and reason not in reasons:
                reasons.append(reason)
        aggregated.append(
            SimpleNamespace(
                id=last.id,
                user_id=last.user_id,
                user=getattr(first, "user", None),
                attendance_date=last.attendance_date,
                status=status,
                check_in_at=min(checkins) if checkins else None,
                check_out_at=None if has_open_session else (max(checkouts) if checkouts else None),
                manager_override=bool(manual_rows),
                check_in_lat=next((row.check_in_lat for row in sessions if row.check_in_lat is not None), None),
                check_in_lng=next((row.check_in_lng for row in sessions if row.check_in_lng is not None), None),
                check_in_distance_m=next((row.check_in_distance_m for row in sessions if row.check_in_distance_m is not None), None),
                check_in_method=next((row.check_in_method for row in sessions if row.check_in_method), None),
                check_out_lat=getattr(last, "check_out_lat", None),
                check_out_lng=getattr(last, "check_out_lng", None),
                check_out_distance_m=getattr(last, "check_out_distance_m", None),
                check_out_method=next((row.check_out_method for row in reversed(sessions) if row.check_out_method), None),
                last_heartbeat_at=max(
                    (row.last_heartbeat_at for row in sessions if row.last_heartbeat_at),
                    default=None,
                ),
                last_heartbeat_lat=getattr(last, "last_heartbeat_lat", None),
                last_heartbeat_lng=getattr(last, "last_heartbeat_lng", None),
                last_heartbeat_distance_m=getattr(last, "last_heartbeat_distance_m", None),
                mobile_device_id=next((row.mobile_device_id for row in reversed(sessions) if row.mobile_device_id), None),
                auto_checkout_reason=", ".join(reasons) or None,
                notes=next((row.notes for row in reversed(sessions) if row.notes), None),
                _worked_minutes_override=total_minutes,
                session_count=len(sessions),
                session_rows=sessions,
            )
        )
    return aggregated


def late_minutes_for_row(row, grace_minutes: int = LATE_GRACE_MINUTES) -> int:
    if not row or not row.check_in_at:
        return 0
    shift_start, _ = shift_window_for_row(row)
    shift_start_dt = datetime.combine(row.attendance_date, shift_start)
    delta = int((row.check_in_at - shift_start_dt).total_seconds() // 60)
    return max(0, delta - max(0, int(grace_minutes)))


def early_exit_minutes_for_row(row, grace_minutes: int = EARLY_EXIT_GRACE_MINUTES) -> int:
    if not row or not row.check_out_at:
        return 0
    _, shift_end = shift_window_for_row(row)
    shift_end_dt = datetime.combine(row.attendance_date, shift_end)
    delta = int((shift_end_dt - row.check_out_at).total_seconds() // 60)
    return max(0, delta - max(0, int(grace_minutes)))


def calculate_status_from_times(row, leniency_minutes: int = LATE_GRACE_MINUTES) -> str:
    if not row:
        return "absent"
    if row.check_in_at and not row.check_out_at:
        return "pending_correction"
    worked_minutes = worked_minutes_for_row(row)
    # A small admin-configured grace window prevents a few minutes of GPS or
    # clock drift from turning an otherwise complete shift into a half day.
    shift_start, shift_end = shift_window_for_row(row)
    effective_start = row.check_in_at
    effective_end = row.check_out_at
    grace = timedelta(minutes=max(0, int(leniency_minutes)))
    if effective_start and effective_start <= datetime.combine(row.attendance_date, shift_start) + grace:
        effective_start = datetime.combine(row.attendance_date, shift_start)
    if effective_end and effective_end >= datetime.combine(row.attendance_date, shift_end) - grace:
        effective_end = datetime.combine(row.attendance_date, shift_end)
    if effective_start and effective_end:
        worked_minutes = max(0, int((effective_end - effective_start).total_seconds() // 60))
    required_minutes = shift_required_minutes_for_row(row)
    present_minutes = math.ceil(required_minutes * 0.85)
    half_day_minutes = math.ceil(required_minutes * 0.50)
    short_attendance_minutes = math.ceil(required_minutes * 0.25)
    if worked_minutes >= present_minutes:
        return "present_all_day"
    if worked_minutes >= half_day_minutes:
        return "first_half"
    if worked_minutes >= short_attendance_minutes:
        return "short_attendance"
    return "absent"


def refresh_attendance_row(
    row,
    manual_status: str | None = None,
    *,
    leniency_minutes: int = LATE_GRACE_MINUTES,
    force_recalculate: bool = False,
) -> str:
    chosen_manual_status = (manual_status or "").strip()
    if chosen_manual_status:
        row.status = chosen_manual_status
        return row.status
    if getattr(row, "manager_override", False) and not force_recalculate:
        return row.status
    if not row.check_in_at and not row.check_out_at:
        row.status = row.status or "absent"
        return row.status
    row.status = calculate_status_from_times(row, leniency_minutes=leniency_minutes)
    return row.status


def attendance_flags_for_row(
    row,
    *,
    leniency_minutes: int = LATE_GRACE_MINUTES,
) -> list[str]:
    if not row:
        return []
    flags = []
    late_minutes = late_minutes_for_row(row, grace_minutes=leniency_minutes)
    early_exit_minutes = early_exit_minutes_for_row(row, grace_minutes=leniency_minutes)
    worked_minutes = worked_minutes_for_row(row)
    if late_minutes > 0:
        flags.append(f"Late by {late_minutes} min")
    if early_exit_minutes > 0:
        flags.append(f"Early exit by {early_exit_minutes} min")
    if row.check_in_at and not row.check_out_at:
        flags.append("Pending checkout")
    required_minutes = shift_required_minutes_for_row(row)
    present_minutes = math.ceil(required_minutes * 0.85)
    half_day_minutes = math.ceil(required_minutes * 0.50)
    short_attendance_minutes = math.ceil(required_minutes * 0.25)
    if worked_minutes >= present_minutes:
        flags.append("Full shift")
    elif worked_minutes >= half_day_minutes:
        flags.append("Half shift")
    elif worked_minutes >= short_attendance_minutes:
        flags.append("Short attendance")
    if getattr(row, "manager_override", False):
        flags.append("Admin override")
    return flags


def attendance_pay_fraction(status: str | None) -> float:
    if status in [
        "present_all_day",
        "weekly_off",
        "late_entry",
        "early_exit",
        "on_leave",
        "earned_leave",
        "company_holiday",
    ]:
        return 1.0
    if status in ["first_half", "second_half", "half_day_earned_leave", "half_day_leave"]:
        return 0.5
    if status == "short_attendance":
        return 0.25
    return 0.0


def late_penalty_days(late_marks: int) -> float:
    if late_marks <= 3:
        return 0.0
    return math.ceil((late_marks - 3) / 3) * 0.5


def build_attendance_summary(attendance_logs: list, *, leniency_minutes: int = LATE_GRACE_MINUTES):
    summary = {
        "present_days": 0,
        "half_days": 0,
        "short_days": 0,
        "leave_days": 0,
        "earned_leave_days": 0,
        "urgent_leave_days": 0,
        "company_holiday_days": 0,
        "sick_days": 0,
        "weekly_off_days": 0,
        "late_marks": 0,
        "early_exits": 0,
        "pending_corrections": 0,
        "worked_hours": 0.0,
        "late_penalty_days": 0.0,
    }
    for row in attendance_logs:
        status = row.status or ""
        if status in ["present_all_day", "late_entry", "early_exit"]:
            summary["present_days"] += 1
        elif status in ["first_half", "second_half"]:
            summary["half_days"] += 1
        elif status == "short_attendance":
            summary["short_days"] += 1
        elif status == "on_leave":
            summary["leave_days"] += 1
        elif status == "earned_leave":
            summary["leave_days"] += 1
            summary["earned_leave_days"] += 1
        elif status in ["half_day_earned_leave", "half_day_leave"]:
            summary["leave_days"] += 0.5
            summary["half_days"] += 1
            summary["earned_leave_days"] += 0.5
        elif status == "urgent_leave":
            summary["urgent_leave_days"] += 1
        elif status == "half_day_urgent_leave":
            summary["urgent_leave_days"] += 0.5
            summary["half_days"] += 1
        elif status == "company_holiday":
            summary["company_holiday_days"] += 1
        elif status == "sick_leave":
            summary["sick_days"] += 1
        elif status == "weekly_off":
            summary["weekly_off_days"] += 1
        elif status in ["pending_correction", "missed_checkout"]:
            summary["pending_corrections"] += 1
        if late_minutes_for_row(row, grace_minutes=leniency_minutes) > 0:
            summary["late_marks"] += 1
        if early_exit_minutes_for_row(row, grace_minutes=leniency_minutes) > 0:
            summary["early_exits"] += 1
        if row.check_in_at and not row.check_out_at and status not in ["pending_correction", "missed_checkout"]:
            summary["pending_corrections"] += 1
        summary["worked_hours"] = round(summary["worked_hours"] + worked_hours_for_row(row), 2)
    summary["late_penalty_days"] = late_penalty_days(summary["late_marks"])
    return summary
