"""Canonical calculations for stock health and daily inventory variance.

The functions in this module are deliberately independent from Flask and the
database.  Routes, dashboards, closing, and purchase suggestions all call the
same rules so the numbers cannot drift between screens.
"""

from __future__ import annotations


UNIT_ALIASES = {
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "gm": "g",
    "gms": "g",
    "gram": "g",
    "grams": "g",
    "l": "litre",
    "ltr": "litre",
    "litres": "litre",
    "liter": "litre",
    "liters": "litre",
    "millilitre": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "piece": "pcs",
    "pieces": "pcs",
    "pc": "pcs",
    "unit": "pcs",
    "units": "pcs",
}

UNIT_FACTORS = {
    "kg": ("mass", 1000.0),
    "g": ("mass", 1.0),
    "litre": ("volume", 1000.0),
    "ml": ("volume", 1.0),
    "pcs": ("count", 1.0),
}


def normalize_unit(value: str | None) -> str:
    unit = (value or "").strip().lower().replace(".", "")
    return UNIT_ALIASES.get(unit, unit)


def convert_quantity(
    quantity: float,
    source_unit: str | None,
    target_unit: str | None,
) -> float | None:
    """Convert common recipe units, returning None when conversion is unsafe."""
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    if source == target or not source or not target:
        return round(float(quantity or 0), 6) if source == target else None
    source_spec = UNIT_FACTORS.get(source)
    target_spec = UNIT_FACTORS.get(target)
    if not source_spec or not target_spec or source_spec[0] != target_spec[0]:
        return None
    base_quantity = float(quantity or 0) * source_spec[1]
    return round(base_quantity / target_spec[1], 6)


def stock_band(
    current_amount: float,
    adequate_amount: float,
    tolerance_percent: float = 15,
) -> dict:
    """Return the ± tolerance stock band and the amount needed to reach target."""
    current = max(0.0, float(current_amount or 0))
    adequate = max(0.0, float(adequate_amount or 0))
    tolerance = max(0.0, min(99.0, float(tolerance_percent or 0))) / 100.0
    if adequate <= 0:
        return {
            "status": "unconfigured",
            "current": round(current, 3),
            "adequate": 0.0,
            "lower": 0.0,
            "upper": 0.0,
            "suggested_purchase": 0.0,
            "excess": 0.0,
        }
    lower = round(adequate * (1.0 - tolerance), 6)
    upper = round(adequate * (1.0 + tolerance), 6)
    if current < lower:
        status = "deficit"
    elif current > upper:
        status = "overstock"
    else:
        status = "adequate"
    return {
        "status": status,
        "current": round(current, 3),
        "adequate": round(adequate, 3),
        "lower": round(lower, 3),
        "upper": round(upper, 3),
        "suggested_purchase": round(max(0.0, adequate - current), 3)
        if status == "deficit"
        else 0.0,
        "excess": round(max(0.0, current - upper), 3)
        if status == "overstock"
        else 0.0,
    }


def closing_metrics(
    *,
    opening_stock: float,
    inbound_amount: float,
    closing_stock: float,
    expected_consumption: float,
    explicit_wastage: float,
    unit_price: float = 0,
) -> dict:
    """Compare physical depletion with recipe-derived and explicitly logged use."""
    opening = max(0.0, float(opening_stock or 0))
    inbound = max(0.0, float(inbound_amount or 0))
    closing = max(0.0, float(closing_stock or 0))
    expected = max(0.0, float(expected_consumption or 0))
    explicit = max(0.0, float(explicit_wastage or 0))
    physical = round(opening + inbound - closing, 3)
    variance = round(physical - expected, 3)
    unexplained = round(variance - explicit, 3)
    return {
        "physical_consumption": physical,
        "expected_consumption": round(expected, 3),
        "explicit_wastage": round(explicit, 3),
        "variance": variance,
        "unexplained_variance": unexplained,
        "unexplained_wastage_value": round(
            max(0.0, unexplained) * max(0.0, float(unit_price or 0)), 2
        ),
    }


def coverage_metrics(
    *,
    current_amount: float,
    adequate_amount: float,
    average_daily_usage: float,
    shelf_life_days: int | None,
) -> dict:
    """Estimate how long stock and its configured target will last."""
    current = max(0.0, float(current_amount or 0))
    adequate = max(0.0, float(adequate_amount or 0))
    usage = max(0.0, float(average_daily_usage or 0))
    shelf_life = max(0, int(shelf_life_days or 0))
    coverage_days = round(current / usage, 1) if usage > 0 else None
    target_days = round(adequate / usage, 1) if usage > 0 and adequate > 0 else None
    spoilage_risk = bool(
        coverage_days is not None and shelf_life > 0 and coverage_days > shelf_life
    )
    return {
        "coverage_days": coverage_days,
        "target_days": target_days,
        "shelf_life_days": shelf_life or None,
        "spoilage_risk": spoilage_risk,
    }
