# Manual attendance policy — staged implementation, NOT ACTIVE

User-confirmed effective date: 2026-10-01. Exclude every account with any Admin role from the opening leave-balance reset. Preserve accounts, credentials, historical attendance, payroll documents and leave transactions.

## Confirmed rules

- P: full present day; UL: unpaid day.
- PL: minimum two calendar days' advance notification.
- SL: unexpected sickness notified to management; advance notice not required.
- PL and SL share two paid leave days per calendar month; further leave unpaid.
- FH/SH: half worked; the other half uses 0.5 of the monthly allowance only when applied for with the required notice; otherwise unpaid.
- BL: eligible close-family bereavement, five paid days each Jan–Dec year; separate from monthly allowance.
- Unused monthly allowance is banked for cash settlement only, not usable as leave in later months.
- Value each month's unused days using that month's salary divided by its actual calendar-day count. Retain salary snapshots; do not revalue historical entitlement using current salary.
- Pay accumulated entitlement with March salary for continuing employees; on exit pay if 15 days' notice completed or management released the employee; otherwise record forfeiture.

## Implemented foundation

- Pure Decimal-based day/entitlement/exit calculations with regression tests.
- Explicit transactional opening-reset function plus per-person audit record; excludes all admin-role accounts, includes archived staff, skips service accounts and already-reset people.
- No startup activation and no exposed reset button. NO STAFF BALANCES HAVE BEEN RESET.

## Required before activation

1. Replace current manual entry controls with P/PL/UL/FH/SH/BL/SL plus notification date, half-day application and bereavement confirmation. Retain pre-policy records under legacy logic.
2. Persist dated manual attendance decisions/audit history and salary snapshots. Recompute allowance consumption in chronological order when a backdated entry changes.
3. Integrate monthly earned cash ledger and idempotent March/exit settlements with all payroll summaries and exports; do not count unpaid or unsettled days as paid salary.
4. Disable old accrual, leave-debit/approval, check-in/out, geofence and heartbeat writers for migrated staff. Hide obsolete setup/self-service controls without deleting history or disabling unrelated Android functionality.
5. Adapt staff/admin dashboards, calendar labels and exports to use the same calculation. Remove old shift-minute/late-mark penalties from post-policy manual payroll.
6. Preview local reset counts and excluded admin accounts, back up local test data, then activate reset AND replacement calculation paths transactionally. Never reset while legacy maintenance can recreate old balances.
7. Test restart/retry, annual rollover, partial days, backdated edits, salary changes, role exclusions, March settlement and departure eligibility against a copied database.
8. Obtain approval for AWS rollout and take a current verified AWS backup. Never copy the local test database over live orders or staff records.

Open implementation assumption to show in UI: a monthly entitlement is not finalized into payable cash until attendance for that month is complete. Joining/leaving-month proration is not yet specified; do not silently invent a prorating rule.
