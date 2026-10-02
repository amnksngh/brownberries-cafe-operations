"""Read-only payment attribution for filtered, settled sales (no database queries)."""
import json
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

METHODS = (("cash", "Cash"), ("card", "Card"), ("upi", "UPI"),
           ("other", "Other"), ("unknown", "Not recorded"))


def _number(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _method(value):
    value = " ".join(str(value or "").lower().split())
    if value == "cash":
        return "cash"
    if value in {"card", "card tap", "card dip", "credit card", "debit card"}:
        return "card"
    if value in {"upi", "qr", "upi/qr", "qr/upi"}:
        return "upi"
    if value in {"", "-", "split", "split payment", "unknown", "none"}:
        return "unknown"
    return "other"


def _weights(order):
    try:
        entries = json.loads(order.payment_breakdown_json or "[]")
        if not isinstance(entries, list):
            raise ValueError("Payment rows must be a list")
        result = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid payment row")
            amount = _number(entry.get("amount"))
            if amount is None:
                raise ValueError("Invalid payment amount")
            if amount:
                method = _method(entry.get("method"))
                result[method] = result.get(method, Decimal(0)) + amount
        if result:
            return result
    except (TypeError, ValueError):
        pass
    # Never guess Cash/UPI when an old split has no usable breakdown.
    return {_method(order.payment_type): Decimal(1)}


def summarize_payments(filtered_orders, *, allocated=False):
    """Attribute each order's collected total using its settlement's tender mix.

    A shared split breakdown is a settlement total, NOT an amount to add once
    per order. Group identical settlement mixes and apportion their selected
    total once, retaining exact paise with the largest-remainder method.
    """
    groups = {}
    for row in filtered_orders:
        order = row["order"]
        if order.status != "paid":
            continue
        total = _number(order.total_amount) or Decimal(0)
        ratio = min(_number(row.get("ratio", 1)) or Decimal(0), Decimal(1))
        cents = int((total * ratio * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        weights = _weights(order)
        signature = tuple(sorted(weights.items()))
        group_key = (order.settlement_group_id or ("order", order.id), signature)
        groups[group_key] = groups.get(group_key, 0) + cents

    totals = {key: 0 for key, _ in METHODS}
    for (_group, signature), cents in groups.items():
        weight_total = sum(weight for _method_key, weight in signature)
        shares = [(method, Decimal(cents) * weight / weight_total) for method, weight in signature]
        floors = {method: int(amount.to_integral_value(rounding=ROUND_FLOOR)) for method, amount in shares}
        remainder = cents - sum(floors.values())
        ranked = sorted(shares, key=lambda pair: (-(pair[1] - floors[pair[0]]), pair[0]))
        for method, _amount in ranked[:remainder]:
            floors[method] += 1
        for method, amount in floors.items():
            totals[method] += amount
    total = sum(totals.values())
    return {
        "total_collected": total / 100,
        "allocated": allocated,
        "rows": [{"key": key, "label": label, "amount": totals[key] / 100,
                  "percentage": round(totals[key] * 100 / total, 2) if total else 0.0}
                 for key, label in METHODS if key in {"cash", "card", "upi"} or totals[key]],
    }
