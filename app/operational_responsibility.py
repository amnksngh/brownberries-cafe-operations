"""Operational item, SOP, responsibility, and contribution helpers.

This module is intentionally additive.  MenuItem remains the only authority for
customer visibility and ordering; OperationalItem is an internal planning layer.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime

from .extensions import db
from .models import (
    EmployeeOperationalSkill,
    InventoryItem,
    MenuItem,
    OperationalItem,
    OperationalItemVariant,
    OperationalRecipeLine,
    OperationalRecipeVersion,
    OperationalResponsibilityAssignment,
    OperationalResponsibilityPlanVersion,
    OperationalSopStage,
    OperationalSopStep,
    OperationalSopVersion,
    OperationalStepRoleRequirement,
    Workstation,
)


PRODUCTION_TYPES = (
    ("unclassified", "Not classified yet"),
    ("purchased_resale", "Purchased – Resale"),
    ("single_stage", "Single-stage"),
    ("multi_stage", "Multi-stage"),
    ("prepared_component", "Prepared Component"),
    ("composite_item", "Composite Item"),
    ("combo", "Combo / Bundle"),
    ("external_partial", "External / Partially Prepared"),
)

PRODUCTION_ROUTES = (
    ("direct_resale", "Direct resale"),
    ("prepare_on_order", "Prepare on order"),
    ("batch_prepare", "Batch prepare"),
    ("batch_component_finish", "Batch component + order finishing"),
    ("composite", "Composite item"),
    ("combo_bundle", "Combo / bundle"),
    ("external_finish", "External preparation + internal finishing"),
)

ITEM_TYPES = (
    ("prepared_item", "Prepared dish / beverage"),
    ("prepared_component", "Prepared component"),
    ("purchased_product", "Purchased product"),
    ("raw_ingredient", "Raw ingredient"),
    ("combo", "Combo / bundle"),
)

STOCK_TRACKING_OPTIONS = (
    ("none", "None"),
    ("unit", "Unit"),
    ("batch", "Batch"),
)

PREPARATION_TIMINGS = (
    ("advance_batch", "Advance batch"),
    ("during_order", "During order"),
    ("assembly_only", "Assembly only"),
)

RESPONSIBILITY_TYPES = (
    ("responsible", "Responsible"),
    ("assistant", "Assistant"),
    ("supervisor", "Supervisor"),
    ("approver", "Approver / QC"),
    ("service", "Service / handover"),
)

ASSIGNMENT_TYPES = (
    ("primary", "Primary"),
    ("backup", "Backup"),
    ("supervisor", "Supervisor"),
    ("approver", "Approver"),
    ("emergency", "Emergency"),
)

SOP_STAGE_SUGGESTIONS = (
    "Mise en place",
    "Preparation",
    "Cooking / brewing",
    "Assembly",
    "Quality control",
    "Plating / packing",
    "Handover",
    "Cleaning / reset",
)


def _unique_code(prefix: str, source_id: int) -> str:
    base = f"{prefix}-{source_id:04d}"
    code = base
    suffix = 2
    while OperationalItem.query.filter_by(internal_code=code).first():
        code = f"{base}-{suffix}"
        suffix += 1
    return code


def _workstation_for_slug(slug: str | None):
    normalized = (slug or "").strip().lower()
    return Workstation.query.filter_by(slug=normalized).first() if normalized else None


def _menu_variants(menu_item: MenuItem) -> list[tuple[str, float, str]]:
    if not menu_item.has_size_variants or not menu_item.size_pricing_json:
        return [("Standard", 1, "portion")]
    try:
        rows = json.loads(menu_item.size_pricing_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        rows = []
    variants = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("size") or "").strip():
            variants.append((str(row["size"]).strip(), 1, "portion"))
    return variants or [("Standard", 1, "portion")]


def ensure_operational_items_seeded() -> None:
    """Create non-destructive internal profiles for existing menu/inventory data."""

    inventory_rows = InventoryItem.query.order_by(InventoryItem.id).all()
    inventory_by_name: dict[str, list[InventoryItem]] = defaultdict(list)
    for row in inventory_rows:
        inventory_by_name[(row.name or "").strip().lower()].append(row)

    changed = False
    for menu_item in MenuItem.query.order_by(MenuItem.id).all():
        profile = OperationalItem.query.filter_by(menu_item_id=menu_item.id).first()
        if profile:
            continue
        matching_inventory = next(
            (
                row
                for row in inventory_by_name.get((menu_item.name or "").strip().lower(), [])
                if not OperationalItem.query.filter_by(inventory_item_id=row.id).first()
            ),
            None,
        )
        workstation = _workstation_for_slug(menu_item.prep_station)
        profile = OperationalItem(
            internal_code=_unique_code("MENU", menu_item.id),
            name=menu_item.name,
            item_type="prepared_item",
            production_type="unclassified",
            production_route="prepare_on_order",
            sellable=True,
            purchasable=matching_inventory is not None,
            internally_produced=True,
            stock_tracking="unit" if matching_inventory else "none",
            menu_item_id=menu_item.id,
            inventory_item_id=matching_inventory.id if matching_inventory else None,
            default_workstation_id=workstation.id if workstation else None,
            active=not bool(menu_item.is_deleted),
            notes="Created from the existing menu catalog. Classify before approving an SOP.",
        )
        db.session.add(profile)
        db.session.flush()
        for name, quantity, unit in _menu_variants(menu_item):
            db.session.add(
                OperationalItemVariant(
                    item_id=profile.id,
                    name=name,
                    serving_quantity=quantity,
                    serving_unit=unit,
                )
            )
        changed = True

    for inventory_item in inventory_rows:
        if OperationalItem.query.filter_by(inventory_item_id=inventory_item.id).first():
            continue
        workstation = _workstation_for_slug(inventory_item.area)
        profile = OperationalItem(
            internal_code=_unique_code("INV", inventory_item.id),
            name=inventory_item.name,
            item_type="raw_ingredient",
            production_type="unclassified",
            production_route="direct_resale",
            sellable=False,
            purchasable=True,
            internally_produced=False,
            stock_tracking="unit",
            inventory_item_id=inventory_item.id,
            default_workstation_id=workstation.id if workstation else None,
            active=True,
            notes="Created from the existing inventory catalog. Internal only unless separately linked to Menu Management.",
        )
        db.session.add(profile)
        db.session.flush()
        db.session.add(
            OperationalItemVariant(
                item_id=profile.id,
                name="Standard",
                serving_quantity=1,
                serving_unit=inventory_item.unit or "unit",
            )
        )
        changed = True

    if changed:
        db.session.commit()


def current_recipe(item: OperationalItem, include_draft: bool = True):
    rows = sorted(item.recipe_versions, key=lambda row: (row.version_number, row.id), reverse=True)
    if include_draft:
        draft = next((row for row in rows if row.status == "draft"), None)
        if draft:
            return draft
    now = datetime.utcnow()
    return next(
        (
            row
            for row in rows
            if row.status == "approved"
            and (not row.effective_from or row.effective_from <= now)
            and (not row.effective_to or row.effective_to > now)
        ),
        next((row for row in rows if row.status == "approved"), None),
    )


def current_sop(item: OperationalItem, include_draft: bool = True):
    rows = sorted(item.sop_versions, key=lambda row: (row.version_number, row.id), reverse=True)
    if include_draft:
        draft = next((row for row in rows if row.status == "draft"), None)
        if draft:
            return draft
    now = datetime.utcnow()
    return next(
        (
            row
            for row in rows
            if row.status == "approved"
            and (not row.effective_from or row.effective_from <= now)
            and (not row.effective_to or row.effective_to > now)
        ),
        next((row for row in rows if row.status == "approved"), None),
    )


def current_plan(sop: OperationalSopVersion | None, include_draft: bool = True):
    if not sop:
        return None
    rows = sorted(sop.responsibility_plans, key=lambda row: (row.version_number, row.id), reverse=True)
    if include_draft:
        draft = next((row for row in rows if row.status == "draft"), None)
        if draft:
            return draft
    return next((row for row in rows if row.status == "approved"), None)


def ensure_draft_recipe(item: OperationalItem) -> OperationalRecipeVersion:
    existing = current_recipe(item, include_draft=True)
    if existing and existing.status == "draft":
        return existing
    next_version = max([row.version_number for row in item.recipe_versions] or [0]) + 1
    row = OperationalRecipeVersion(
        item_id=item.id,
        variant_id=item.variants[0].id if item.variants else None,
        version_number=next_version,
        yield_quantity=1,
        yield_unit=item.variants[0].serving_unit if item.variants else "portion",
        status="draft",
    )
    db.session.add(row)
    db.session.flush()
    return row


def ensure_draft_sop(item: OperationalItem) -> OperationalSopVersion:
    existing = current_sop(item, include_draft=True)
    if existing and existing.status == "draft":
        if not current_plan(existing, include_draft=True):
            db.session.add(
                OperationalResponsibilityPlanVersion(
                    sop_version_id=existing.id, version_number=1, status="draft"
                )
            )
            db.session.flush()
        return existing
    next_version = max([row.version_number for row in item.sop_versions] or [0]) + 1
    recipe = current_recipe(item, include_draft=False)
    row = OperationalSopVersion(
        item_id=item.id,
        variant_id=item.variants[0].id if item.variants else None,
        recipe_version_id=recipe.id if recipe else None,
        version_number=next_version,
        name=f"{item.name} SOP",
        production_mode=item.production_route,
        yield_quantity=1,
        yield_unit=item.variants[0].serving_unit if item.variants else "portion",
        status="draft",
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(
        OperationalResponsibilityPlanVersion(
            sop_version_id=row.id, version_number=1, status="draft"
        )
    )
    db.session.flush()
    return row


def clone_recipe(source: OperationalRecipeVersion) -> OperationalRecipeVersion:
    item = source.item
    next_version = max([row.version_number for row in item.recipe_versions] or [0]) + 1
    clone = OperationalRecipeVersion(
        item_id=item.id,
        variant_id=source.variant_id,
        version_number=next_version,
        yield_quantity=source.yield_quantity,
        yield_unit=source.yield_unit,
        status="draft",
        notes=source.notes,
    )
    db.session.add(clone)
    db.session.flush()
    for line in source.lines:
        db.session.add(
            OperationalRecipeLine(
                recipe_version_id=clone.id,
                input_item_id=line.input_item_id,
                input_variant_id=line.input_variant_id,
                required_component_version_id=line.required_component_version_id,
                quantity=line.quantity,
                unit=line.unit,
                yield_loss_percent=line.yield_loss_percent,
                preparation_timing=line.preparation_timing,
                substitution_allowed=line.substitution_allowed,
            )
        )
    return clone


def clone_sop(source: OperationalSopVersion) -> OperationalSopVersion:
    item = source.item
    next_version = max([row.version_number for row in item.sop_versions] or [0]) + 1
    clone = OperationalSopVersion(
        item_id=item.id,
        variant_id=source.variant_id,
        recipe_version_id=source.recipe_version_id,
        version_number=next_version,
        name=source.name,
        production_mode=source.production_mode,
        yield_quantity=source.yield_quantity,
        yield_unit=source.yield_unit,
        standard_duration_seconds=source.standard_duration_seconds,
        standard_labour_seconds=source.standard_labour_seconds,
        required_equipment=source.required_equipment,
        status="draft",
        notes=source.notes,
    )
    db.session.add(clone)
    db.session.flush()
    requirement_map = {}
    for stage in source.stages:
        stage_clone = OperationalSopStage(
            sop_version_id=clone.id, name=stage.name, sequence=stage.sequence
        )
        db.session.add(stage_clone)
        db.session.flush()
        for step in stage.steps:
            step_clone = OperationalSopStep(
                stage_id=stage_clone.id,
                sequence=step.sequence,
                instruction=step.instruction,
                active_time_seconds=step.active_time_seconds,
                passive_time_seconds=step.passive_time_seconds,
                skill_id=step.skill_id,
                minimum_skill_level=step.minimum_skill_level,
                workstation_id=step.workstation_id,
                is_critical=step.is_critical,
                criticality_weight=step.criticality_weight,
                can_run_parallel=step.can_run_parallel,
                quality_checkpoint=step.quality_checkpoint,
                evidence_required=step.evidence_required,
            )
            db.session.add(step_clone)
            db.session.flush()
            for req in step.role_requirements:
                req_clone = OperationalStepRoleRequirement(
                    step_id=step_clone.id,
                    role_name=req.role_name,
                    responsibility_type=req.responsibility_type,
                    participation_factor=req.participation_factor,
                    skill_weight=req.skill_weight,
                    headcount=req.headcount,
                )
                db.session.add(req_clone)
                db.session.flush()
                requirement_map[req.id] = req_clone.id
    source_plan = current_plan(source, include_draft=True)
    plan = OperationalResponsibilityPlanVersion(
        sop_version_id=clone.id, version_number=1, status="draft"
    )
    db.session.add(plan)
    db.session.flush()
    if source_plan:
        for assignment in source_plan.assignments:
            new_requirement_id = requirement_map.get(assignment.step_role_requirement_id)
            if new_requirement_id:
                db.session.add(
                    OperationalResponsibilityAssignment(
                        plan_version_id=plan.id,
                        step_role_requirement_id=new_requirement_id,
                        employee_id=assignment.employee_id,
                        assignment_type=assignment.assignment_type,
                        priority=assignment.priority,
                    )
                )
    return clone


def _recipe_cycle(item_id: int, input_item_id: int, depth: int = 0, visited=None) -> bool:
    if item_id == input_item_id:
        return True
    if depth >= 4:
        return True
    visited = set(visited or set())
    if input_item_id in visited:
        return False
    visited.add(input_item_id)
    component = db.session.get(OperationalItem, input_item_id)
    recipe = current_recipe(component, include_draft=True) if component else None
    return bool(
        recipe
        and any(
            _recipe_cycle(item_id, line.input_item_id, depth + 1, visited)
            for line in recipe.lines
        )
    )


def recipe_input_is_valid(item_id: int, input_item_id: int) -> bool:
    return item_id != input_item_id and not _recipe_cycle(item_id, input_item_id)


def sop_contribution(item: OperationalItem, include_components: bool = True, _depth=0, _visited=None):
    """Return derived labour and weighted contribution; percentages are never stored."""

    visited = set(_visited or set())
    if item.id in visited or _depth > 4:
        return {"rows": [], "labour_seconds": 0.0, "weighted_seconds": 0.0, "cycle": True}
    visited.add(item.id)
    grouped = defaultdict(lambda: {"labour_seconds": 0.0, "weighted_seconds": 0.0, "source": set()})
    sop = current_sop(item, include_draft=True)
    if sop:
        for stage in sop.stages:
            for step in stage.steps:
                for req in step.role_requirements:
                    labour = max(0, step.active_time_seconds) * max(0, req.participation_factor) * max(1, req.headcount)
                    weighted = labour * max(0, req.skill_weight) * max(0, step.criticality_weight)
                    key = req.role_name.strip().lower() or "unattributed"
                    grouped[key]["labour_seconds"] += labour
                    grouped[key]["weighted_seconds"] += weighted
                    grouped[key]["source"].add("Own SOP")

    recipe = current_recipe(item, include_draft=True) if include_components else None
    if recipe and _depth < 4:
        for line in recipe.lines:
            component = line.input_item
            if not component or component.id in visited:
                continue
            child = sop_contribution(component, True, _depth + 1, visited)
            component_sop = current_sop(component, include_draft=True)
            component_yield = float(component_sop.yield_quantity or 1) if component_sop else 1.0
            quantity_factor = max(0.0, float(line.quantity or 0)) / max(component_yield, 0.000001)
            loss_factor = 1 + max(0.0, float(line.yield_loss_percent or 0)) / 100
            for child_row in child["rows"]:
                key = child_row["role_key"]
                grouped[key]["labour_seconds"] += child_row["labour_seconds"] * quantity_factor * loss_factor
                grouped[key]["weighted_seconds"] += child_row["weighted_seconds"] * quantity_factor * loss_factor
                grouped[key]["source"].add(component.name)

    labour_total = sum(row["labour_seconds"] for row in grouped.values())
    weighted_total = sum(row["weighted_seconds"] for row in grouped.values())
    rows = []
    for role_key, values in sorted(grouped.items(), key=lambda entry: -entry[1]["labour_seconds"]):
        rows.append(
            {
                "role_key": role_key,
                "role_name": role_key.replace("_", " ").title(),
                "labour_seconds": round(values["labour_seconds"], 2),
                "weighted_seconds": round(values["weighted_seconds"], 2),
                "labour_percent": round(values["labour_seconds"] * 100 / labour_total, 2) if labour_total else 0,
                "weighted_percent": round(values["weighted_seconds"] * 100 / weighted_total, 2) if weighted_total else 0,
                "source": ", ".join(sorted(values["source"])),
            }
        )
    return {
        "rows": rows,
        "labour_seconds": round(labour_total, 2),
        "weighted_seconds": round(weighted_total, 2),
        "cycle": False,
    }


def responsibility_assignments_for(sop: OperationalSopVersion | None):
    plan = current_plan(sop, include_draft=True)
    grouped = defaultdict(list)
    if plan:
        for assignment in plan.assignments:
            grouped[assignment.step_role_requirement_id].append(assignment)
    return grouped


def validation_messages(item: OperationalItem) -> list[dict]:
    messages = []

    def add(level, code, text):
        messages.append({"level": level, "code": code, "text": text})

    if item.production_type == "unclassified":
        add("warning", "production-type", "Choose a production type before this profile is operationally ready.")
    if item.active_from and item.active_to and item.active_to < item.active_from:
        add("error", "active-dates", "Active Until cannot be earlier than Active From.")
    if item.sellable and not item.menu_item_id:
        add("info", "internal-sellable", "Marked sellable internally but not exposed to customers because it is not linked to Menu Management.")
    if item.menu_item_id:
        add("info", "menu-visibility", "Customer visibility still follows the linked Menu Item's availability and deletion settings.")
    sop = current_sop(item, include_draft=True)
    recipe = current_recipe(item, include_draft=True)
    requires_sop = item.internally_produced and item.production_route != "direct_resale"
    if requires_sop and not sop:
        add("error", "missing-sop", "Internally produced items need an SOP.")
    if item.production_type in {"prepared_component", "composite_item", "multi_stage"} and not recipe:
        add("warning", "missing-recipe", "This production type normally needs a recipe or component structure.")
    if item.production_type == "purchased_resale" and sop and any(stage.steps for stage in sop.stages):
        add("warning", "resale-sop", "Purchased–resale items normally need receiving/service responsibility rather than a preparation SOP.")
    if recipe:
        for line in recipe.lines:
            if not recipe_input_is_valid(item.id, line.input_item_id):
                add("error", "recipe-cycle", f"Recipe component {line.input_item.name} creates a cycle or exceeds four levels.")
    assignments = responsibility_assignments_for(sop)
    if sop:
        steps = [step for stage in sop.stages for step in stage.steps]
        if not steps:
            add("error", "missing-steps", "The SOP has no steps.")
        for step in steps:
            if not step.role_requirements:
                add("error", "missing-role", f"Step {step.sequence} ({step.instruction[:45]}) has no responsible role.")
            if step.active_time_seconds <= 0 and step.passive_time_seconds <= 0:
                add("warning", "missing-time", f"Step {step.sequence} has no active or passive standard time.")
            for req in step.role_requirements:
                rows = assignments.get(req.id, [])
                if not any(row.assignment_type == "primary" for row in rows):
                    add("warning", "missing-primary", f"{req.role_name.title()} on step {step.sequence} has no primary employee.")
                if step.is_critical and not any(row.assignment_type in {"backup", "emergency"} for row in rows):
                    add("warning", "missing-backup", f"Critical step {step.sequence} has no backup for {req.role_name.title()}.")
                if step.skill_id:
                    qualified = EmployeeOperationalSkill.query.filter_by(
                        skill_id=step.skill_id, status="active"
                    ).filter(EmployeeOperationalSkill.competency_level >= step.minimum_skill_level).first()
                    if not qualified:
                        add("warning", "skill-coverage", f"No active employee is certified for {step.skill.name} level {step.minimum_skill_level}.")
    if not messages:
        add("ok", "ready", "No setup issues found.")
    return messages


def update_sop_totals(sop: OperationalSopVersion) -> None:
    steps = [step for stage in sop.stages for step in stage.steps]
    sop.standard_duration_seconds = sum(
        max(0, step.active_time_seconds) + max(0, step.passive_time_seconds) for step in steps
    )
    sop.standard_labour_seconds = round(
        sum(
            max(0, step.active_time_seconds)
            * max(0, req.participation_factor)
            * max(1, req.headcount)
            for step in steps
            for req in step.role_requirements
        )
    )


def approve_recipe(recipe: OperationalRecipeVersion, actor_id: int) -> None:
    if recipe.status != "draft":
        raise ValueError("Only a draft recipe can be approved.")
    now = datetime.utcnow()
    for row in recipe.item.recipe_versions:
        if row.id != recipe.id and row.status == "approved" and not row.effective_to:
            row.effective_to = now
    recipe.status = "approved"
    recipe.effective_from = now
    recipe.approved_by_user_id = actor_id


def approve_sop(sop: OperationalSopVersion, actor_id: int) -> None:
    if sop.status != "draft":
        raise ValueError("Only a draft SOP can be approved.")
    steps = [step for stage in sop.stages for step in stage.steps]
    if not steps:
        raise ValueError("Add at least one SOP step before approval.")
    if any(not step.role_requirements for step in steps):
        raise ValueError("Every SOP step needs at least one role requirement before approval.")
    now = datetime.utcnow()
    for row in sop.item.sop_versions:
        if row.id != sop.id and row.status == "approved" and not row.effective_to:
            row.effective_to = now
            old_plan = current_plan(row, include_draft=False)
            if old_plan and not old_plan.effective_to:
                old_plan.effective_to = now
    update_sop_totals(sop)
    sop.status = "approved"
    sop.effective_from = now
    sop.approved_by_user_id = actor_id
    plan = current_plan(sop, include_draft=True)
    if plan:
        plan.status = "approved"
        plan.effective_from = now
        plan.approved_by_user_id = actor_id


def profile_payload(item: OperationalItem) -> dict:
    sop = current_sop(item, include_draft=True)
    recipe = current_recipe(item, include_draft=True)
    contribution = sop_contribution(item)
    return {
        "id": item.id,
        "internal_code": item.internal_code,
        "name": item.name,
        "production_type": item.production_type,
        "production_route": item.production_route,
        "flags": {
            "sellable": item.sellable,
            "purchasable": item.purchasable,
            "internally_produced": item.internally_produced,
            "stock_tracking": item.stock_tracking,
        },
        "customer_visible": bool(item.menu_item and item.menu_item.available and not item.menu_item.is_deleted),
        "visibility_source": "Menu Management only",
        "recipe_version": recipe.version_number if recipe else None,
        "recipe_status": recipe.status if recipe else None,
        "sop_version": sop.version_number if sop else None,
        "sop_status": sop.status if sop else None,
        "contribution": contribution,
        "validation": validation_messages(item),
    }
