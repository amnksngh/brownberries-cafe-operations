# Statistics: Payments Collected

The top of Statistics shows Total Collected, Cash, Card and UPI with rupee
amounts and percentages for the selected settlement-date period. Other and
Not recorded appear only when applicable. The same figures are included in
the Excel Summary sheet and statistics summary API.

- QR/UPI are combined; Card Tap/Card Dip/credit/debit card are combined.
- Only paid orders included by the existing statistics filters are counted.
- Recorded order totals include service charge and billed charges. This may
  differ from the existing Total Sales metric, which uses item sales plus
  packaging/delivery rather than the full collected receipt amount.
- Cash uses net payment, not tendered notes before change.
- The same settlement breakdown may be stored on several orders. Amounts are
  attributed proportionally to each order, not counted in full for every order.
- Category/workstation filters use proportional attribution and say so visibly;
  the system does not claim to know which dish a particular bank payment covered.
- Missing or malformed split details remain Not recorded, never assumed UPI.
- Amount allocation uses decimal arithmetic and reconciles in paise. No extra
  database queries, payment writes, schema changes or historical data updates.
- Settlement clears the server's statistics cache; refresh Statistics after
  recording a payment to see the newly calculated figures.

Validation: 53 unit tests pass. Snapshot route/JavaScript audit passes. Payment
totals and their component methods reconcile to recorded eligible paid orders
for today and last month (including periods with more than the UI's 400-order
list limit). Roll back this feature using the pre-release Git tag, retaining
the current database to preserve newer transactions.
