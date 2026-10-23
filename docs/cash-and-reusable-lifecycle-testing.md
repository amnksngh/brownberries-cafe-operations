# Cash reconciliation and reusable asset lifecycle (local preview)

These additions do not reset existing balances or migrate asset quantities automatically.
No production deployment is part of this change.

## Cash gap

Open `/cafe/cash-counter/control` as an admin or owner. In **Reconcile**:

1. Investigate incorrect payment methods using the existing payment-correction workflow first.
2. Count every actual note and coin; omitted/zero fields mean none physically present.
3. Review and enter the exact signed adjustment shown, provide a reason and confirm.
4. Submit once. The ledger and denominations will match the verified physical amount.
5. Check audit history. Original entries, sales and payments remain intact. Corrections
   are not sales or automatically booked expenses. Closing keeps shortage/overage evidence.

For a ledger of ₹750 with an empty physical drawer, the adjustment is **−750**.
Do not enter zeros merely to hide an unexplained balance; confirm the real cash first.

## Reusable assets

Inventory → Reusable Assets → **Stock Lifecycle**.

Existing items: select an item, enter how many currently usable units are in store.
The rest become in-use. Historical purchased-minus-usable stock becomes an opening
broken/lost balance, without creating fresh staff charges. No old purchases, counts,
loss events or payroll allocations are deleted.

New items: create the item in Stock Lifecycle, then record its purchased batch. Use the
existing item editor for images and area/workstation changes. The older creation form
is for an already-received opening balance, not an outstanding delivery.

### Suggested test using a disposable item

1. Create “Lifecycle Test Cups”. Record 20 purchased units at ₹100 each, with payment
   mode and cafe/owner funding. This records the purchase cost once; it does not also
   withdraw notes/coins from the cash drawer.
2. Mark the batch loaded. Expect 20 in transit, no usable units.
3. Receive 18 usable units, confirming final delivery. Expect store 18, broken/lost 2.
   Transit loss is ₹200 cafe-borne, with no staff allocations.
4. Dispatch 10: store 8, in-use 10.
5. Report 2 broken units against a staff member: store 8, in-use 8, broken/lost 4.
   Existing policy charges that employee ₹100 and cafe ₹100.
6. Mark 1 in-use unit worn out: store 8, in-use 7, broken/lost 4, unusable 1.
   This is cafe-borne and creates no staff charge.
7. Recover 1 against the reported-loss movement: store 8, in-use 8,
   broken/lost 3, unusable 1. Total remains 20. Review the original staff charge;
   recovery does not silently change settled salaries or automatically refund money.
8. Save a weekly physical count of store 8 / in-use 8. If counts differ, resolve the
   cause with dispatch/return/loss/wear/linked recovery before saving the verified count.
9. Buy another batch of 5: total 25, awaiting delivery 5, other buckets unchanged.
   Load and receive it separately; it must not create a second item listing.

### Tally and safeguards

`Total purchased = awaiting delivery + in transit + store + in use + broken/lost + unusable`.
After all deliveries are finalized, this reduces to the four stock/loss buckets.

- Each receipt finalizes one batch. Do not finalize a partial delivery while more units
  are expected; the unreceived balance is treated as uncompensated transit loss.
- Counts and movements are recorded now in IST; invoice purchase dates may be historical.
- Over-dispatch, over-receipt, repeated receipt and over-recovery are rejected.
- Recovery must link to the same item's original loss and cannot exceed unrecovered units.
- Store and in-use quantities cannot go negative.
- Ordinary staff can report their own breakage only; managers/inventory managers/admins/
  owners can operate stock. The existing permissions still control staff-charge settlement.
- Two open tabs: save one, then submit the stale second tab. It must ask for a reload.
- Repeated submissions with the same request key must not duplicate purchases or losses.
- The existing loss chart shows **gross recorded loss value**; recovered units are separate.
  Pending deliveries are excluded from loss totals.
- No automatic expense duplication is made for wear or transit loss: the purchase cost
  already exists. Loss reporting explains where purchased assets went.

## Automated checks

Run `python -W ignore -m unittest discover -s tests -q`.
With Playwright and the project Python available, run `node scripts/check_asset_lifecycle_ui.cjs`.
The UI check uses a temporary database and checks 390 px / 1280 px layouts, existing
inventory rendering, admin reconciliation visibility and movement-form action selection.
