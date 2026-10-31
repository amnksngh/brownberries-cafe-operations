# Performance reviews — local testing

Staff Management → **Performance Reviews** (`/cafe/staff/performance`).
Attendance rules, compensation and organisation assignments are not modified by
this workflow. Approval approves an assessment, not a promotion or salary change.

## Roles and process

1. An admin/owner configures the organisation and assigns the employee to an active
   position. Create a scorecard for one of its grades. Up to eight metrics have a
   name, weight, measurable target, evidence requirement and explicit scoring rubric.
   Weights total exactly 100%. Choose a progression-consideration threshold.
2. Create an employee review using a specific scorecard version, period and reviewer.
   The reviewer must be a different active manager/admin/owner. Grade selection is
   the grade evaluated; it does not assign or promote an employee into that grade.
3. The assigned reviewer records scores (0–100), evidence text/references and a
   development recommendation. Partial drafts may be saved. Submission requires
   every score and its evidence, and is permitted after the end date (IST).
4. A separate admin/owner verifies the assessment and approves it with a reason,
   or returns it to draft with feedback. Neither employee nor reviewer may approve.
   Approved reviews are locked. Every save/submit/decision is audited.

The weighted score is the sum of `score × weight / 100`, rounded to two decimals.
A score at or above the configured threshold means eligible for consideration,
not an automatic promotion. Rubrics should distinguish measurable results from
judgment, account for workload, and cite supporting records. Evidence is currently
text/reference based, not file upload or automatic ingestion of sales/KOT data.

Create a new scorecard version to revise targets. Existing reviews keep their
original criteria, grade, threshold and career destination snapshot. The same
employee/position/exact date range cannot be created twice, even with another grade.
Stale review versions cannot overwrite concurrent edits.

## Local acceptance checks

- Create two metrics weighted 60/40. Confirm 59/40 is rejected.
- Assign an employee and manager, then create a completed-period review.
- As that manager, save a partial draft; incomplete submission must fail.
- Score 90 and 80 with evidence: the weighted score must be 86.
- Submit, then use a separate admin/owner to return it with a reason.
- Revise and resubmit; approve and confirm it cannot be changed afterward.
- Verify a manager cannot access another review or create scorecards.
- Verify self-approval, duplicate periods and stale edits are rejected.
- Create a new scorecard version and confirm the old review is unchanged.
- Confirm employee salary, position, permissions and attendance remain unchanged.

Effective-dated promotion/compensation approvals and payroll integration remain
a separate next phase. No production deployment is included in this change.
