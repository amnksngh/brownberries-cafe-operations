# Daily Cash Control

Status: implemented on the development branch for local review. This work does not deploy itself, reset a drawer, or alter existing financial data.

Open **Cashier Desk → Daily Cash Control**, or **Cash Counter → Daily Cash Control**. The original sales settlement and cash ledger remain in place. A signed-in Cashier-authorized user, admin, manager, or owner can use the controls. Kiosk users must sign in to record manual cash movements; the new control page never accepts anonymous writes.

## Daily process

1. **Opening:** before new transactions, count the money left from the previous day. Save an Opening count. This is an immutable snapshot, not a deposit. The difference against the ledger is shown separately. Existing opening cash must not be counted as today's sales.
2. **Refill:** record the actual notes/coins added using Morning / operating float. Record source/person in the reference. A refill increases the drawer, not revenue. If counting after a refill, do not add that same refill twice.
3. **Sales:** continue settling tables from Cashier Desk. Record Cash/Card/UPI split, tender and change in the existing settlement form. Cash sales are net of change; tender/change have separate KPIs. Shared table settlements count only once.
4. **Expenses:** choose Cafe expense paid from drawer and outgoing denominations. If already booked, link its expense ID or paid inventory purchase ID (Cash / Cafe Operations / exact amount). Otherwise select a category to create one expense automatically. Never create a second expense for an existing inventory purchase. A linked record can fund only one withdrawal. Before linking an older expense, check that an earlier manual withdrawal has not already paid it: historical unlinked records cannot safely be inferred.
5. **Owner handover / end withdrawal:** record outgoing denominations and recipient/reference. These are transfers, not cafe expenses and not reductions in sales.
6. **Change denominations:** choose Exchange and enter incoming and outgoing notes/coins of equal total value. Both sides are saved in one transaction; drawer value and sales do not change.
7. **Spot check:** count at any time and save a Spot count. This does not change the drawer ledger. Differences remain evidence for investigation.
8. **Closing:** after the last sale and final withdrawals, count the cash actually remaining. Close the open session. Keep the printed/PDF report if desired. The next day starts from the physical carried-forward cash, not zero unless it is actually empty. A forgotten prior-day session must be closed before opening another; its actual closure timestamp remains recorded.

## The accounting model

**Expected closing cash = counted opening cash + ledger movements since opening.**

Movements include gross customer cash received minus change, refills, expense withdrawals, handovers, exchanges (net zero), and approved corrections. Closing minus expected gives the session overage (+) or shortage (−). The report also shows the difference against the full ledger, so an opening discrepancy is not hidden. Counts do not automatically overwrite denominations, create sales, write off losses, or accuse an employee.

Cash/Card/UPI sales are based on settled orders and settlement time in IST. Drawer flows are based on movement time. An approved correction to an older sale changes that sale's payment attribution, while its compensating drawer entries are dated when approved. Saved closing reports are frozen; the current daily report can therefore differ after a later correction. Overnight session movement totals follow actual opening/closing time; the sales summary in a closing report is the selected IST calendar day, not an inferred night-shift business day.

The current drawer totals at the top are live even when viewing an older report date. Original tender/change KPIs preserve original entries; corrections are listed separately. Recorded denominations are not proof of physical possession.

## Wrong payment method

1. Select the recent settlement (up to the latest 200 orders), not just one item/order out of a shared payment.
2. Enter the correct complete Cash/Card/UPI split; the sum must equal the original total.
3. Enter actual original cash tender and change; their difference must equal corrected cash. Use zero denominations for a wholly electronic payment. Card/UPI corrections require a transaction reference.
4. Submit a reason. Nothing financial changes while pending.
5. A **different admin or owner** checks the bank/card receipt and physical cash, then approves or rejects with a reason. Self-approval is blocked, including for admins. A cafe with only one admin should have the cashier submit requests for that admin to review.
6. Approval updates all orders in the settlement without changing totals, taxes, payment dates, or order items. Original payment details remain in the request audit. Cash entries are corrected through appended reversals/replacements, never deletion. Statistics use the corrected breakdown and their cache is invalidated.

Historical splits without a settlement identifier require separate review; the system does not guess which orders belong together. If a correction leaves impossible recorded denominations because later withdrawals relied on the wrong original entry, physically count and investigate the intervening entries. The system does not silently manufacture balancing cash.

## Safeguards

- CSRF protection, server-side authorization, required reasons and actor/time records.
- Serialized SQLite cash writes, duplicate-submission keys and stale-page detection for controls.
- No ordinary deletion of settlement cash, controlled movements, or entries included in a closed period.
- Already-paid orders cannot be re-marked paid to bypass payment correction approval.
- Manual cash entries cannot be future-dated or backdated into closed periods.
- Equal-value exchanges and expense/withdrawal links commit atomically.
- No automatic payroll deductions, loss write-offs, bank confirmation, or allegation of theft. Managers must compare payment-provider statements and receipts with the recorded totals.

## Acceptance test (local copy only)

1. Log in as cashier. Open with actual carried-forward cash; confirm no ledger deposit is created.
2. Refill ₹1,000; confirm drawer rises but sales remain unchanged.
3. Settle a ₹150 order using a ₹200 note and ₹50 change; confirm Cash sales ₹150, tender ₹200, change ₹50.
4. Exchange a ₹100 note for two ₹50 notes; confirm zero total-value change. Try unequal values and insufficient notes: both must be rejected.
5. Pay a ₹100 expense, creating its expense log; confirm exactly one Cash expense in Inventory/Statistics. Repeat using an existing expense/purchase ID, and confirm duplicate linking is rejected.
6. Hand over cash to owner, then withdraw at end of day. Confirm these do not become expenses or reduce sales.
7. Request Cash→UPI and UPI→Cash corrections. Test approval by a different admin, denial for cashier and self-approval, rejection, stale settlement, and split-settlement totals. Check statistics and cash ledger.
8. Close with expected cash, then test a separate day's shortage/overage. Confirm opening/closing denominations and the original closing report survive later corrections.
9. Double-click/replay a control submission: only one record is saved. Try another tab after a cash movement: stale values must be rejected.
10. Test at 390px mobile and desktop; use Print / Save PDF for a dated report.

Automated checks: `python -m unittest discover -s tests`; optional Playwright fixture test `node scripts/check_cash_desk_ui.cjs` with `PYTHON` and `PLAYWRIGHT_MODULE` set to installed runtimes. The UI fixture uses a temporary database only.
