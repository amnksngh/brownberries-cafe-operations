# Organisation and career structure — local configuration release

Open Staff Management → Organisation & Career Structure, or
`/cafe/staff/organisation`, signed in as an admin or owner.

## First setup

1. Choose **Install starter structure with G1, G2, G3**. This is explicit and
   cannot overwrite an existing structure. No employees are assigned by default.
2. Review the reporting tree. Executive Chef, DCDP, Commis II and Commis III are
   disabled initially. Kitchen Steward Team is a separate branch.
3. Expand a position to change its department, title, station/group, supervisor,
   career destination, assigned employees and responsibilities.
4. Configure its three editable grades. Monthly INR ranges start blank, not zero.
   Record measurable targets, weights, evidence, review periods and advancement
   requirements in the criteria field. No scores or raises are calculated yet.
5. Enter a reason and save. Every save retains a full revision snapshot and actor.

New departments are created by entering a new department name on a position.
Multiple root positions and cross-department reporting are supported. An active
position with no assigned employees is shown as vacant. Multiple employees can
occupy one position, and an employee may cover multiple positions.

Reporting and career paths are independent. For example, Commis I reports to CDP
but progresses toward the currently disabled DCDP position. Enabling DCDP does
not automatically reassign reporting lines. Disabling an occupied position or a
supervisor of active positions is rejected until assignments/reports are moved.

## Test checklist

- Install templates once and check that a second installation cannot overwrite.
- Create a new position and department; link an existing station/group.
- Change its supervisor and verify the tree. Try a circular reporting path;
  it must be rejected. Repeat with career progression.
- Assign an employee and edit G1/G2/G3 names/ranges; confirm salary and login
  roles remain unchanged. Reversed or invalid monetary ranges must be rejected.
- Try disabling an occupied position, then unassign staff and try again.
- Open two tabs, save one, then save the stale tab; it must request a reload.
- Confirm ordinary staff/managers cannot view or edit this configuration.
- Review the change history and inspect the page on a narrow/mobile viewport.

## Deliberate boundaries

This release establishes the editable structure. Criteria are configuration text,
not a completed objective-review engine. Structured weighted reviews, evidence
submission, temporary assignment dates, promotion approvals and effective-dated
salary changes are subsequent work. Existing payroll, permissions, dish ownership
and workstation assignments are not changed. No production deployment is included.

Storage is additive: organisation configuration plus append-only full snapshots.
Do not remove these tables to roll back code; retain them for history/recovery.
