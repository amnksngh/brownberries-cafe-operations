# Manual attendance and payroll

Open **Staff → Manual Attendance & Payroll** (`/cafe/manual-payroll`).

1. An admin confirms employment dates. On exit, enter the actual last employed
   date, notice days served and management-release decision.
2. Review each employed calendar day using P/PL/UL/FH/SH/BL/SL. Existing attendance
   is shown as evidence, not automatically approved. Blank does not mean absent.
3. After the month or employment ends, confirm that month's monthly salary and
   finalize. Earlier months must be finalized first and all days reviewed.
4. Finalized months are locked. Backdated edits before finalization recompute
   the monthly and annual caps in chronological order.
5. Record actual payment only after paying. Cash/Card/UPI/Bank and cafe versus
   owner funding are supported. This creates one linked expense; do not enter it
   again manually. Existing asset deductions carry unpaid remainders forward.
6. Staff view only their own records; managers/owners can view all staff. Only
   admins may review, finalize or record payments. Download the combined CSV
   from the same screen; salary receipts must match finalized net pay.

PL/SL share 2 days for a full calendar month employed, 1 for a partial month with
15 or more calendar days employed, and 0 below 15. PL requires two days' notice;
SL requires notification by the absence day. FH/SH pays the worked half plus an
eligible, timely applied half from the same allowance. Eligible close-family BL
has a separate five-day cap, resetting January 1.

Unused allowance is cash-only, valued at the earning month's confirmed salary
divided by its calendar days. March payroll settles unallocated entitlement
through March. Exit settles it with 15 notice days served or management release;
otherwise it records forfeiture. Each month can be allocated only once. Payments
are never made automatically.

All admin-role accounts are excluded from the October 1 opening reset. Accounts,
credentials, historical attendance, leave transactions and receipt files remain.
Old credits, geofence/check-in/heartbeat and legacy leave writes are blocked.
Pre-October reports remain readable. Older Android apps may still show monitoring
controls: disable monitoring; server attendance writes are blocked while other
cafe operations remain available.

## Deploy and activate

Run the unit suite, `scripts/audit_manual_payroll.py --instance <source-instance>`
(disposable copy only), and the performance audit with `--manual-payroll`.
Fetch the verified commit on AWS and rerun checks there. Stop application writers,
take and verify a fresh backup, then run:

```
python scripts/activate_manual_payroll.py --instance /srv/brownberries/production-instance --admin-id <active-admin-id> --apply
```

Restart and verify the service, public ordering and manual payroll screen.
Activation and the audited reset commit atomically; retries are idempotent.
No attendance decisions or real payments are fabricated during deployment.

## Recovery and corrections

Keep the pre-cutover commit and verified backup. Do not blindly roll back to
legacy code after activation: it can recreate old credits and ignores reviewed
payroll. Prefer a forward fix. Full restoration is safe only while writers remain
stopped and no newer records have been accepted. Never restore stale data over
new orders. Finalized payroll is immutable through the portal; a discovered
mistake requires an audited correction, not a contradictory receipt or direct
database edit.
