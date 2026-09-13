from __future__ import annotations


from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def reported_loss_split(total_loss_value: float) -> dict[str, float]:
    """Split a reported breakage equally while keeping the paise total exact."""
    total = max(Decimal("0.00"), _money(total_loss_value))
    staff = (total * Decimal("0.50")).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    return {
        "staff_charge": float(staff),
        "cafe_share": float(total - staff),
    }


def shared_loss_allocations(
    total_loss_value: float, staff_user_ids: list[int] | tuple[int, ...]
) -> list[dict[str, float | int]]:
    """Allocate an unreported loss equally and exactly across eligible staff."""
    user_ids = sorted({int(value) for value in staff_user_ids if int(value) > 0})
    total_paise = int(max(Decimal("0.00"), _money(total_loss_value)) * 100)
    if not user_ids or total_paise <= 0:
        return []
    base_paise, remainder = divmod(total_paise, len(user_ids))
    allocations = []
    for index, user_id in enumerate(user_ids):
        charge_paise = base_paise + (1 if index < remainder else 0)
        allocations.append(
            {
                "user_id": user_id,
                "charge_amount": charge_paise / 100,
                "share_percent": round(100 / len(user_ids), 4),
            }
        )
    return allocations


def weighted_average_unit_price(
    existing_quantity: int | float,
    existing_unit_price: int | float,
    added_quantity: int | float,
    added_unit_price: int | float,
) -> float:
    existing_quantity = max(0, int(existing_quantity or 0))
    added_quantity = max(0, int(added_quantity or 0))
    total_quantity = existing_quantity + added_quantity
    if total_quantity <= 0:
        return 0.0
    total_value = (
        existing_quantity * max(0.0, float(existing_unit_price or 0))
        + added_quantity * max(0.0, float(added_unit_price or 0))
    )
    return round(total_value / total_quantity, 2)


def reusable_count_change(
    quantity_before: int | float,
    current_quantity: int | float,
    unit_price: int | float,
) -> dict:
    before = max(0, int(quantity_before or 0))
    current = max(0, int(current_quantity or 0))
    lost = max(0, before - current)
    recovered = max(0, current - before)
    return {
        "quantity_before": before,
        "current_quantity": current,
        "lost_quantity": lost,
        "recovered_quantity": recovered,
        "loss_value": round(lost * max(0.0, float(unit_price or 0)), 2),
    }


def reusable_stock_composition(
    purchased_quantity: int | float,
    current_quantity: int | float,
) -> dict:
    purchased = max(0, int(purchased_quantity or 0))
    current = min(purchased, max(0, int(current_quantity or 0))) if purchased else 0
    lost = max(0, purchased - current)
    if purchased <= 0:
        current_percent = 0.0
        lost_percent = 0.0
    else:
        current_percent = round((current / purchased) * 100, 2)
        lost_percent = round(100 - current_percent, 2)
    return {
        "purchased": purchased,
        "current": current,
        "lost": lost,
        "current_percent": current_percent,
        "lost_percent": lost_percent,
    }
