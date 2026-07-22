"""Shared staff account retirement logic.

Staff accounts can be referenced by historical orders, inventory actions,
documents, and attendance records. Those references must not be deleted just
to remove a login, so accounts with history are archived instead.
"""

from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (
    CafeFeedback,
    CafeOrder,
    InventoryExpenseLog,
    InventoryToPurchase,
    JobApplicationTimeline,
    MenuItem,
    SalaryReceipt,
    StaffAttendance,
    StaffDocument,
    StaffLeaveRequest,
    StaffMobileSession,
    StaffProfile,
)


def _has_staff_history(user_id: int) -> bool:
    references = (
        db.session.query(CafeOrder.id).filter(CafeOrder.ordered_by_user_id == user_id).first(),
        db.session.query(CafeFeedback.id).filter(CafeFeedback.submitted_by_user_id == user_id).first(),
        db.session.query(MenuItem.id).filter(MenuItem.chef_user_id == user_id).first(),
        db.session.query(InventoryExpenseLog.id).filter(InventoryExpenseLog.created_by_user_id == user_id).first(),
        db.session.query(InventoryToPurchase.id).filter(
            db.or_(
                InventoryToPurchase.created_by_user_id == user_id,
                InventoryToPurchase.completed_by_user_id == user_id,
                InventoryToPurchase.closed_by_user_id == user_id,
            )
        ).first(),
        db.session.query(JobApplicationTimeline.id).filter(JobApplicationTimeline.changed_by_user_id == user_id).first(),
        db.session.query(StaffDocument.id).filter(
            db.or_(
                StaffDocument.user_id == user_id,
                StaffDocument.uploaded_by_user_id == user_id,
            )
        ).first(),
        db.session.query(SalaryReceipt.id).filter(
            db.or_(
                SalaryReceipt.user_id == user_id,
                SalaryReceipt.uploaded_by_user_id == user_id,
            )
        ).first(),
        db.session.query(StaffAttendance.id).filter(StaffAttendance.user_id == user_id).first(),
        db.session.query(StaffLeaveRequest.id).filter(StaffLeaveRequest.user_id == user_id).first(),
        db.session.query(StaffMobileSession.id).filter(StaffMobileSession.user_id == user_id).first(),
    )
    return any(references)


def retire_staff_account(user):
    """Delete an unused account or archive one that has historical links.

    Returns ``"deleted"`` for a true hard delete and ``"archived"`` when
    retaining history is required. The integrity-error fallback protects older
    Windows databases that may contain a reference not yet represented here.
    """
    if _has_staff_history(user.id):
        profile = user.staff_profile
        if not profile:
            profile = StaffProfile(user_id=user.id)
            db.session.add(profile)
        profile.archived = True
        user.active = False
        db.session.commit()
        return "archived"

    profile = user.staff_profile
    if profile:
        db.session.delete(profile)
    db.session.delete(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        profile = user.staff_profile or StaffProfile.query.filter_by(user_id=user.id).first()
        if not profile:
            profile = StaffProfile(user_id=user.id)
            db.session.add(profile)
        profile.archived = True
        user.active = False
        db.session.commit()
        return "archived"
    return "deleted"
