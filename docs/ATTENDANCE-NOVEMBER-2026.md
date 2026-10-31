# November 2026 attendance policy — implementation in progress

This document records the confirmed business policy, not a claim of statutory
compliance. No production activation or balance reset is part of this change.

## Confirmed rules

- Effective November 1, 2026. Prior completed month's net sales after discounts
  and refunds, excluding taxes/service charges, set the next month's allowance:
  below INR 800,000: 2; from 800,000 to below 1,200,000: 3; otherwise 4 days.
- Cumulative paid ordinary/sick leave limits: 2-day band unlocks on days 1/16;
  3-day band on 1/11/21; 4-day band on 1/8/15/22.
- Sickness bypasses advance notice and blocked dates, but not these paid-use
  limits. Extra sickness is unpaid without a pooled-leave penalty.
- Bereavement remains separate: five paid days per calendar year, exempt from
  early-month caps, concurrent-leave restrictions and blocked dates.
- Unused allowance is cash-settlement only. Preserve earning-month salary and
  calendar-day divisor. March/eligible exit settlements must not double-pay lots.
- Unauthorized nonsick absence may receive an explicit admin-reviewed pool
  penalty. Half-day absence means 0.5 day. Consume oldest unsettled days first;
  uncovered debt offsets future accrual. Never change already-paid settlements.
- Admins can revoke penalties in special circumstances with a reason and audit.
- Admins/owners manage No Leave Days with reasons. Existing approved requests
  survive. Cancel only overlapping dates of pending ordinary requests, preserving
  the original application and unaffected dates.
- New hires joining November 1 or later have 90 calendar days of probation;
  existing employees are excluded. No discretionary paid allowance or accrual
  during probation. Retain separately recorded sickness/bereavement and applicable
  statutory entitlements. Confirmation is explicit; extensions require a reason.
  Standard allowance begins the first full month after confirmation.

## Implemented locally

`app/attendance_policy_november.py` provides explicit, opt-in calculations for
sales bands, calendar caps, probation, unused cash, per-date cancellation plans,
and FIFO penalty reservations with remaining debt. Existing callers continue to
use the existing policy. Tests exercise thresholds and date boundaries.

## Required before activation

Approved cutover treatment (October 31): retain legacy accumulated days as an
opening balance awaiting admin valuation. Do not reset balances, guess historical
salary values, or apply automatic penalties to historical absences. The last
read-only production check found 65 days across 14 nonzero accounts; recapture
actual balances at cutover rather than hard-coding these earlier totals.
`leave_opening.capture_opening` now provides an explicit idempotent snapshot
operation. It is not called by startup or deployment. Integration into the
reviewed cutover transaction and the admin valuation UI remains pending.

1. Persist completed-month net-sales snapshots using the existing sales/refund
   accounting source; surface provisional/unavailable months without guessing.
2. Add probation confirmation/extension fields and audited admin controls.
3. Add date-level leave cancellations and admin/owner blocked-date management;
   connect employee and on-behalf application/approval paths and calendar views.
4. Add immutable penalty/reservation/revocation events with transactional locking,
   admin permissions, reasons, duplicate protection and source-month values.
5. Connect previews, finalization, exports, March/exit settlements and payroll UI
   to the new policy. Preserve existing payroll/settlement snapshots.
6. Verify legal/statutory entitlement handling before enabling payroll deductions.
7. Test integration, authorization, concurrent updates and rollback on a copy of
   the database. Do not call legacy `activate()` to switch policy: that operation
   also resets opening balances. No reset has been requested here.

The current pure calculation functions do not authorize or persist penalties,
cancel actual requests, or enable the policy on AWS.
