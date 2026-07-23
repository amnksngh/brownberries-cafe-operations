"""Attendance rule-book defaults and publication helpers."""

from .extensions import db
from .models import AttendanceRuleBook


DEFAULT_ATTENDANCE_RULEBOOK = """BROWNBERRIES CAFE
STAFF ATTENDANCE AND LEAVE RULE BOOK

Effective from the date shown in the app

1. Attendance and punctuality
• Staff must check in only after reaching the cafe premises and must check out before leaving.
• Check-in uses the cafe attendance QR flow and the phone's location permission. Do not share your account or submit attendance for another person unless you are an authorized manager or admin.
• Staff should follow the shift timing assigned in their profile. Late arrival, early exit, missed check-in, or missed check-out may be reviewed by a manager.
• If the network is unavailable, inform the manager as soon as possible. Admin/manager corrections are recorded as overrides.

2. Attendance statuses
• Present All Day: worked the assigned shift.
• First Half / Second Half: worked one half of the assigned day.
• Earned Leave: approved paid leave deducted from the earned balance.
• Urgent Leave: approved unpaid leave, subject to the policy limits below.
• Company Holiday and Weekly Off: paid non-working days when configured by admin.
• Absent: no approved leave and no valid attendance for the day.

3. Earned Leave
• Earned leave starts at zero for a new staff member.
• The default credit is 1 day on the 15th and 1 day at month-end. Admin may change the credit in Leave Settings.
• Unused earned leave carries forward. The app records every credit and deduction in the leave ledger.
• Earned leave can be requested for a maximum of 7 continuous calendar days by default. The configured limit is shown in Profile.
• Earned leave must have sufficient balance and must be approved before it becomes leave in attendance.

4. Urgent Leave
• Urgent leave is unpaid and is allocated after probation according to the configured annual allowance.
• By default, the allowance is 12 days per year and no more than 3 days may be used in a calendar month. Admin may change these values.
• Urgent leave must be approved. Approved urgent leave is recorded as unpaid attendance.

5. Applying for leave
• Submit the request from Profile → Attendance with the leave type, dates, shift portions, reason, and supporting document when useful.
• A request is not approved until an authorized admin or manager decides it. The employee can cancel a pending request.
• The system checks balance, maximum duration, weekly offs, company holidays, overlap, role coverage, and configured cooldown rules.
• Employees must inform their manager for emergencies even when a request is submitted in the app.

6. Approval and records
• Leave requests may be Pending, Approved, Rejected, Request Changes, or Cancelled.
• Approval changes the attendance calendar and, for balance-backed leave, records a debit in the leave ledger.
• Admin and managers may correct attendance with an auditable override. Staff must not edit their own historical attendance.
• Payroll uses the recorded attendance: earned leave, holidays, and weekly offs are paid; urgent leave and absence are unpaid; half days count as half a day.

7. Workplace responsibility
• Do not misuse the attendance, leave, location, or supporting-document features.
• Raise corrections promptly with the manager. False entries may be investigated under cafe policy.
• The latest version in Profile is the controlling version. Ask management if anything is unclear.
"""


def ensure_rulebook_default() -> AttendanceRuleBook:
    current = (
        AttendanceRuleBook.query.filter_by(active=True)
        .order_by(AttendanceRuleBook.version.desc())
        .first()
    )
    if current:
        return current

    latest = AttendanceRuleBook.query.order_by(AttendanceRuleBook.version.desc()).first()
    if latest:
        latest.active = True
        db.session.commit()
        return latest

    rulebook = AttendanceRuleBook(
        version=1,
        title="Staff Attendance Rule Book",
        content_text=DEFAULT_ATTENDANCE_RULEBOOK,
        active=True,
    )
    db.session.add(rulebook)
    db.session.commit()
    return rulebook


def next_rulebook_version() -> int:
    latest = AttendanceRuleBook.query.order_by(AttendanceRuleBook.version.desc()).first()
    return (latest.version if latest else 0) + 1
