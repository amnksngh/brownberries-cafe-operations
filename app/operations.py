from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from .auth_helpers import roles_required
from .extensions import db
from .models import (
    EmployeeOperationalSkill,
    OperationalItem,
    OperationalItemVariant,
    OperationalRecipeLine,
    OperationalRecipeVersion,
    OperationalResponsibilityAssignment,
    OperationalResponsibilityPlanVersion,
    OperationalSkill,
    OperationalSopStage,
    OperationalSopStep,
    OperationalSopVersion,
    OperationalStepRoleRequirement,
    User,
    UserType,
    Workstation,
)
from .operational_responsibility import (
    ASSIGNMENT_TYPES,
    ITEM_TYPES,
    PREPARATION_TIMINGS,
    PRODUCTION_ROUTES,
    PRODUCTION_TYPES,
    RESPONSIBILITY_TYPES,
    SOP_STAGE_SUGGESTIONS,
    STOCK_TRACKING_OPTIONS,
    approve_recipe,
    approve_sop,
    clone_recipe,
    clone_sop,
    current_plan,
    current_recipe,
    current_sop,
    ensure_draft_recipe,
    ensure_draft_sop,
    ensure_operational_items_seeded,
    profile_payload,
    recipe_input_is_valid,
    responsibility_assignments_for,
    sop_contribution,
    update_sop_totals,
    validation_messages,
)


bp = Blueprint("operations", __name__, url_prefix="/cafe/operations")


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _date_value(value):
    try:
        return date.fromisoformat((value or "").strip()) if (value or "").strip() else None
    except ValueError:
        return None


def _item_redirect(item_id, anchor="overview"):
    return redirect(url_for("operations.index", item_id=item_id, _anchor=anchor))


def _draft_recipe_or_404(recipe_id):
    recipe = db.get_or_404(OperationalRecipeVersion, recipe_id)
    if recipe.status != "draft":
        flash("Approved recipe versions are immutable. Create a new revision to make changes.", "error")
        return recipe, False
    return recipe, True


def _draft_sop_or_404(sop_id):
    sop = db.get_or_404(OperationalSopVersion, sop_id)
    if sop.status != "draft":
        flash("Approved SOP versions are immutable. Create a new revision to make changes.", "error")
        return sop, False
    return sop, True


def _role_options():
    values = {
        "executive_chef",
        "sous_chef",
        "chef_de_partie",
        "chef",
        "commis",
        "prep_cook",
        "barista",
        "trainee",
        "expo",
        "server",
        "steward",
        "shift_lead",
    }
    values.update((row.name or "").strip().lower() for row in UserType.query.all() if row.name)
    for user in User.query.filter_by(active=True).all():
        values.update(user.assigned_roles())
    return sorted(value for value in values if value)


@bp.route("/")
@roles_required("admin", "manager")
def index():
    ensure_operational_items_seeded()
    query_text = (request.args.get("q") or "").strip()
    production_filter = (request.args.get("production_type") or "").strip()
    # Menu Management owns prepared, sellable, and manually-created operational
    # records. Profiles created only from raw inventory remain available as recipe
    # inputs, but are managed from Inventory Management and do not appear here.
    menu_managed_filter = db.or_(
        OperationalItem.menu_item_id.isnot(None),
        OperationalItem.inventory_item_id.is_(None),
        OperationalItem.internally_produced.is_(True),
        OperationalItem.sellable.is_(True),
    )
    item_query = OperationalItem.query.filter(menu_managed_filter).order_by(
        OperationalItem.name.asc()
    )
    if query_text:
        like = f"%{query_text}%"
        item_query = item_query.filter(
            db.or_(OperationalItem.name.ilike(like), OperationalItem.internal_code.ilike(like))
        )
    if production_filter:
        item_query = item_query.filter_by(production_type=production_filter)
    items = item_query.all()
    selected_id = request.args.get("item_id", type=int)
    selected_item = next((row for row in items if row.id == selected_id), None)
    if not selected_item and items:
        selected_item = items[0]

    recipe = current_recipe(selected_item, include_draft=True) if selected_item else None
    sop = current_sop(selected_item, include_draft=True) if selected_item else None
    plan = current_plan(sop, include_draft=True)
    assignments = responsibility_assignments_for(sop)
    contribution = sop_contribution(selected_item) if selected_item else {"rows": [], "labour_seconds": 0, "weighted_seconds": 0}
    validation = validation_messages(selected_item) if selected_item else []
    operational_items = OperationalItem.query.filter_by(active=True).order_by(OperationalItem.name.asc()).all()
    staff = [
        user
        for user in User.query.filter_by(active=True).order_by(User.full_name.asc()).all()
        if not (user.email or "").lower().endswith(".guest@brownberries.local")
    ]
    skills = OperationalSkill.query.order_by(OperationalSkill.name.asc()).all()
    certifications = (
        EmployeeOperationalSkill.query.order_by(EmployeeOperationalSkill.updated_at.desc()).all()
    )
    all_profiles = OperationalItem.query.filter(menu_managed_filter).all()
    readiness = {"ready": 0, "warning": 0, "error": 0}
    for profile in all_profiles:
        levels = {row["level"] for row in validation_messages(profile)}
        if "error" in levels:
            readiness["error"] += 1
        elif "warning" in levels:
            readiness["warning"] += 1
        else:
            readiness["ready"] += 1
    return render_template(
        "cafe/operational_items.html",
        items=items,
        all_profile_count=len(all_profiles),
        selected_item=selected_item,
        recipe=recipe,
        sop=sop,
        plan=plan,
        assignments=assignments,
        contribution=contribution,
        validation=validation,
        readiness=readiness,
        query_text=query_text,
        production_filter=production_filter,
        production_types=PRODUCTION_TYPES,
        production_routes=PRODUCTION_ROUTES,
        item_types=ITEM_TYPES,
        stock_tracking_options=STOCK_TRACKING_OPTIONS,
        preparation_timings=PREPARATION_TIMINGS,
        responsibility_types=RESPONSIBILITY_TYPES,
        assignment_types=ASSIGNMENT_TYPES,
        stage_suggestions=SOP_STAGE_SUGGESTIONS,
        workstations=Workstation.query.order_by(Workstation.display_order, Workstation.name).all(),
        operational_items=operational_items,
        skills=skills,
        certifications=certifications,
        staff=staff,
        role_options=_role_options(),
    )


@bp.route("/items", methods=["POST"])
@roles_required("admin", "manager")
def create_item():
    name = (request.form.get("name") or "").strip()
    code = (request.form.get("internal_code") or "").strip().upper()
    if not name or not code:
        flash("Internal code and item name are required.", "error")
        return redirect(url_for("operations.index"))
    if OperationalItem.query.filter(db.func.lower(OperationalItem.internal_code) == code.lower()).first():
        flash("That internal code already exists.", "error")
        return redirect(url_for("operations.index"))
    row = OperationalItem(
        internal_code=code,
        name=name,
        item_type=(request.form.get("item_type") or "prepared_component").strip(),
        production_type=(request.form.get("production_type") or "prepared_component").strip(),
        production_route=(request.form.get("production_route") or "batch_prepare").strip(),
        sellable=False,
        purchasable=False,
        internally_produced=True,
        stock_tracking="batch",
        active=True,
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(
        OperationalItemVariant(
            item_id=row.id, name="Standard", serving_quantity=1, serving_unit="portion"
        )
    )
    db.session.commit()
    flash("Internal operational item created. It is not visible to customers.", "success")
    return _item_redirect(row.id)


@bp.route("/items/<int:item_id>/overview", methods=["POST"])
@roles_required("admin", "manager")
def update_item(item_id):
    row = db.get_or_404(OperationalItem, item_id)
    code = (request.form.get("internal_code") or "").strip().upper()
    duplicate = OperationalItem.query.filter(
        db.func.lower(OperationalItem.internal_code) == code.lower(), OperationalItem.id != row.id
    ).first()
    if not code or duplicate:
        flash("Use a unique internal code.", "error")
        return _item_redirect(row.id)
    row.internal_code = code
    row.name = (request.form.get("name") or row.name).strip()
    row.item_type = (request.form.get("item_type") or "prepared_item").strip()
    row.production_type = (request.form.get("production_type") or "unclassified").strip()
    row.production_route = (request.form.get("production_route") or "prepare_on_order").strip()
    row.sellable = "sellable" in request.form
    row.purchasable = "purchasable" in request.form
    row.internally_produced = "internally_produced" in request.form
    row.stock_tracking = (request.form.get("stock_tracking") or "none").strip()
    row.default_workstation_id = request.form.get("default_workstation_id", type=int)
    row.active_from = _date_value(request.form.get("active_from"))
    row.active_to = _date_value(request.form.get("active_to"))
    row.active = "active" in request.form
    row.notes = (request.form.get("notes") or "").strip() or None
    db.session.commit()
    flash("Operational profile updated. Customer menu visibility was not changed.", "success")
    return _item_redirect(row.id)


@bp.route("/items/<int:item_id>/variants", methods=["POST"])
@roles_required("admin", "manager")
def add_variant(item_id):
    item = db.get_or_404(OperationalItem, item_id)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Variant name is required.", "error")
    elif OperationalItemVariant.query.filter(
        OperationalItemVariant.item_id == item.id,
        db.func.lower(OperationalItemVariant.name) == name.lower(),
    ).first():
        flash("That variant already exists for this item.", "error")
    else:
        db.session.add(
            OperationalItemVariant(
                item_id=item.id,
                name=name,
                serving_quantity=max(0.0001, _safe_float(request.form.get("serving_quantity"), 1)),
                serving_unit=(request.form.get("serving_unit") or "portion").strip(),
            )
        )
        db.session.commit()
        flash("Variant added.", "success")
    return _item_redirect(item.id, "variants")


@bp.route("/variants/<int:variant_id>/archive", methods=["POST"])
@roles_required("admin", "manager")
def archive_variant(variant_id):
    row = db.get_or_404(OperationalItemVariant, variant_id)
    row.active = False
    db.session.commit()
    flash("Variant archived; historical versions remain intact.", "success")
    return _item_redirect(row.item_id, "variants")


@bp.route("/items/<int:item_id>/recipe/start", methods=["POST"])
@roles_required("admin", "manager")
def start_recipe(item_id):
    item = db.get_or_404(OperationalItem, item_id)
    ensure_draft_recipe(item)
    db.session.commit()
    flash("Draft recipe ready.", "success")
    return _item_redirect(item.id, "recipe")


@bp.route("/recipes/<int:recipe_id>", methods=["POST"])
@roles_required("admin", "manager")
def update_recipe(recipe_id):
    recipe, editable = _draft_recipe_or_404(recipe_id)
    if editable:
        variant_id = request.form.get("variant_id", type=int)
        recipe.variant_id = variant_id if any(row.id == variant_id for row in recipe.item.variants) else None
        recipe.yield_quantity = max(0.0001, _safe_float(request.form.get("yield_quantity"), 1))
        recipe.yield_unit = (request.form.get("yield_unit") or "portion").strip()
        recipe.notes = (request.form.get("notes") or "").strip() or None
        db.session.commit()
        flash("Draft recipe header saved.", "success")
    return _item_redirect(recipe.item_id, "recipe")


@bp.route("/recipes/<int:recipe_id>/lines", methods=["POST"])
@roles_required("admin", "manager")
def add_recipe_line(recipe_id):
    recipe, editable = _draft_recipe_or_404(recipe_id)
    if not editable:
        return _item_redirect(recipe.item_id, "recipe")
    input_item_id = request.form.get("input_item_id", type=int)
    input_item = db.session.get(OperationalItem, input_item_id)
    if not input_item or not recipe_input_is_valid(recipe.item_id, input_item.id):
        flash("That component would create a recipe cycle or exceed the four-level operational limit.", "error")
        return _item_redirect(recipe.item_id, "recipe")
    input_variant_id = request.form.get("input_variant_id", type=int)
    if input_variant_id and not any(row.id == input_variant_id for row in input_item.variants):
        input_variant_id = None
    line = OperationalRecipeLine(
        recipe_version_id=recipe.id,
        input_item_id=input_item.id,
        input_variant_id=input_variant_id,
        quantity=max(0, _safe_float(request.form.get("quantity"), 0)),
        unit=(request.form.get("unit") or "unit").strip(),
        yield_loss_percent=min(100, max(0, _safe_float(request.form.get("yield_loss_percent"), 0))),
        preparation_timing=(request.form.get("preparation_timing") or "during_order").strip(),
        substitution_allowed="substitution_allowed" in request.form,
    )
    component_version = current_recipe(input_item, include_draft=False)
    line.required_component_version_id = component_version.id if component_version else None
    db.session.add(line)
    db.session.commit()
    flash("Recipe input added.", "success")
    return _item_redirect(recipe.item_id, "recipe")


@bp.route("/recipe-lines/<int:line_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_recipe_line(line_id):
    line = db.get_or_404(OperationalRecipeLine, line_id)
    recipe = line.recipe_version
    if recipe.status != "draft":
        flash("Approved recipe versions cannot be edited.", "error")
    else:
        db.session.delete(line)
        db.session.commit()
        flash("Recipe input removed from the draft.", "success")
    return _item_redirect(recipe.item_id, "recipe")


@bp.route("/recipes/<int:recipe_id>/approve", methods=["POST"])
@roles_required("admin", "manager")
def approve_recipe_route(recipe_id):
    recipe = db.get_or_404(OperationalRecipeVersion, recipe_id)
    try:
        approve_recipe(recipe, g.current_user.id)
        db.session.commit()
        flash(f"Recipe version {recipe.version_number} approved and locked.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return _item_redirect(recipe.item_id, "history")


@bp.route("/recipes/<int:recipe_id>/revise", methods=["POST"])
@roles_required("admin", "manager")
def revise_recipe(recipe_id):
    source = db.get_or_404(OperationalRecipeVersion, recipe_id)
    existing = next((row for row in source.item.recipe_versions if row.status == "draft"), None)
    if existing:
        flash("This item already has a draft recipe revision.", "error")
    else:
        clone_recipe(source)
        db.session.commit()
        flash("New draft recipe revision created; the approved version remains unchanged.", "success")
    return _item_redirect(source.item_id, "recipe")


@bp.route("/items/<int:item_id>/sop/start", methods=["POST"])
@roles_required("admin", "manager")
def start_sop(item_id):
    item = db.get_or_404(OperationalItem, item_id)
    ensure_draft_sop(item)
    db.session.commit()
    flash("Draft SOP and responsibility plan ready.", "success")
    return _item_redirect(item.id, "sop")


@bp.route("/sops/<int:sop_id>", methods=["POST"])
@roles_required("admin", "manager")
def update_sop(sop_id):
    sop, editable = _draft_sop_or_404(sop_id)
    if editable:
        variant_id = request.form.get("variant_id", type=int)
        recipe_id = request.form.get("recipe_version_id", type=int)
        sop.name = (request.form.get("name") or sop.name).strip()
        sop.production_mode = (request.form.get("production_mode") or sop.item.production_route).strip()
        sop.variant_id = variant_id if any(row.id == variant_id for row in sop.item.variants) else None
        sop.recipe_version_id = recipe_id if any(row.id == recipe_id for row in sop.item.recipe_versions) else None
        sop.yield_quantity = max(0.0001, _safe_float(request.form.get("yield_quantity"), 1))
        sop.yield_unit = (request.form.get("yield_unit") or "portion").strip()
        sop.required_equipment = (request.form.get("required_equipment") or "").strip() or None
        sop.notes = (request.form.get("notes") or "").strip() or None
        update_sop_totals(sop)
        db.session.commit()
        flash("Draft SOP header saved.", "success")
    return _item_redirect(sop.item_id, "sop")


@bp.route("/sops/<int:sop_id>/steps", methods=["POST"])
@roles_required("admin", "manager")
def add_sop_step(sop_id):
    sop, editable = _draft_sop_or_404(sop_id)
    if not editable:
        return _item_redirect(sop.item_id, "sop")
    instruction = (request.form.get("instruction") or "").strip()
    stage_name = (request.form.get("stage_name") or "Preparation").strip()
    if not instruction:
        flash("Step instruction is required.", "error")
        return _item_redirect(sop.item_id, "sop")
    stage = next((row for row in sop.stages if row.name.lower() == stage_name.lower()), None)
    if not stage:
        stage = OperationalSopStage(
            sop_version_id=sop.id,
            name=stage_name,
            sequence=max([row.sequence for row in sop.stages] or [0]) + 1,
        )
        db.session.add(stage)
        db.session.flush()
    step = OperationalSopStep(
        stage_id=stage.id,
        sequence=max([row.sequence for row in stage.steps] or [0]) + 1,
        instruction=instruction,
        active_time_seconds=max(0, _safe_int(request.form.get("active_time_seconds"), 0)),
        passive_time_seconds=max(0, _safe_int(request.form.get("passive_time_seconds"), 0)),
        skill_id=request.form.get("skill_id", type=int),
        minimum_skill_level=min(5, max(1, _safe_int(request.form.get("minimum_skill_level"), 1))),
        workstation_id=request.form.get("workstation_id", type=int),
        is_critical="is_critical" in request.form,
        criticality_weight=max(0.1, _safe_float(request.form.get("criticality_weight"), 1)),
        can_run_parallel="can_run_parallel" in request.form,
        quality_checkpoint=(request.form.get("quality_checkpoint") or "").strip() or None,
        evidence_required=(request.form.get("evidence_required") or "none").strip(),
    )
    db.session.add(step)
    db.session.flush()
    role_name = (request.form.get("role_name") or "").strip().lower()
    if role_name:
        db.session.add(
            OperationalStepRoleRequirement(
                step_id=step.id,
                role_name=role_name,
                responsibility_type=(request.form.get("responsibility_type") or "responsible").strip(),
                participation_factor=max(0, _safe_float(request.form.get("participation_factor"), 1)),
                skill_weight=max(0, _safe_float(request.form.get("skill_weight"), 1)),
                headcount=max(1, _safe_int(request.form.get("headcount"), 1)),
            )
        )
    db.session.flush()
    update_sop_totals(sop)
    db.session.commit()
    flash("SOP step added. Add named primary and backups in the responsibility matrix.", "success")
    return _item_redirect(sop.item_id, "sop")


@bp.route("/steps/<int:step_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_sop_step(step_id):
    step = db.get_or_404(OperationalSopStep, step_id)
    sop = step.stage.sop_version
    if sop.status != "draft":
        flash("Approved SOP steps cannot be deleted.", "error")
    else:
        requirement_ids = [row.id for row in step.role_requirements]
        if requirement_ids:
            OperationalResponsibilityAssignment.query.filter(
                OperationalResponsibilityAssignment.step_role_requirement_id.in_(requirement_ids)
            ).delete(synchronize_session=False)
        db.session.delete(step)
        db.session.flush()
        update_sop_totals(sop)
        db.session.commit()
        flash("Draft SOP step removed.", "success")
    return _item_redirect(sop.item_id, "sop")


@bp.route("/steps/<int:step_id>/requirements", methods=["POST"])
@roles_required("admin", "manager")
def add_requirement(step_id):
    step = db.get_or_404(OperationalSopStep, step_id)
    sop = step.stage.sop_version
    if sop.status != "draft":
        flash("Approved responsibility plans are immutable.", "error")
        return _item_redirect(sop.item_id, "responsibilities")
    role_name = (request.form.get("role_name") or "").strip().lower()
    if not role_name:
        flash("Role is required.", "error")
        return _item_redirect(sop.item_id, "responsibilities")
    req = OperationalStepRoleRequirement(
        step_id=step.id,
        role_name=role_name,
        responsibility_type=(request.form.get("responsibility_type") or "responsible").strip(),
        participation_factor=max(0, _safe_float(request.form.get("participation_factor"), 1)),
        skill_weight=max(0, _safe_float(request.form.get("skill_weight"), 1)),
        headcount=max(1, _safe_int(request.form.get("headcount"), 1)),
    )
    db.session.add(req)
    db.session.flush()
    update_sop_totals(sop)
    db.session.commit()
    flash("Role requirement added.", "success")
    return _item_redirect(sop.item_id, "responsibilities")


@bp.route("/requirements/<int:requirement_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_requirement(requirement_id):
    req = db.get_or_404(OperationalStepRoleRequirement, requirement_id)
    sop = req.step.stage.sop_version
    if sop.status != "draft":
        flash("Approved responsibility plans are immutable.", "error")
    else:
        OperationalResponsibilityAssignment.query.filter_by(
            step_role_requirement_id=req.id
        ).delete(synchronize_session=False)
        db.session.delete(req)
        db.session.flush()
        update_sop_totals(sop)
        db.session.commit()
        flash("Role requirement removed from the draft.", "success")
    return _item_redirect(sop.item_id, "responsibilities")


@bp.route("/requirements/<int:requirement_id>/assignments", methods=["POST"])
@roles_required("admin", "manager")
def add_assignment(requirement_id):
    req = db.get_or_404(OperationalStepRoleRequirement, requirement_id)
    sop = req.step.stage.sop_version
    if sop.status != "draft":
        flash("Approved responsibility plans are immutable.", "error")
        return _item_redirect(sop.item_id, "responsibilities")
    plan = current_plan(sop, include_draft=True)
    if not plan:
        plan = OperationalResponsibilityPlanVersion(
            sop_version_id=sop.id, version_number=1, status="draft"
        )
        db.session.add(plan)
        db.session.flush()
    employee_id = request.form.get("employee_id", type=int)
    employee = db.session.get(User, employee_id)
    assignment_type = (request.form.get("assignment_type") or "primary").strip()
    if not employee or not employee.active:
        flash("Select an active employee.", "error")
    elif OperationalResponsibilityAssignment.query.filter_by(
        plan_version_id=plan.id,
        step_role_requirement_id=req.id,
        employee_id=employee.id,
        assignment_type=assignment_type,
    ).first():
        flash("That employee already has this assignment.", "error")
    else:
        priority = max(1, _safe_int(request.form.get("priority"), 1))
        db.session.add(
            OperationalResponsibilityAssignment(
                plan_version_id=plan.id,
                step_role_requirement_id=req.id,
                employee_id=employee.id,
                assignment_type=assignment_type,
                priority=priority,
            )
        )
        db.session.commit()
        flash("Named responsibility assignment added.", "success")
    return _item_redirect(sop.item_id, "responsibilities")


@bp.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
@roles_required("admin", "manager")
def delete_assignment(assignment_id):
    row = db.get_or_404(OperationalResponsibilityAssignment, assignment_id)
    item_id = row.plan_version.sop_version.item_id
    if row.plan_version.status != "draft":
        flash("Approved assignments are historical records and cannot be removed.", "error")
    else:
        db.session.delete(row)
        db.session.commit()
        flash("Assignment removed from the draft plan.", "success")
    return _item_redirect(item_id, "responsibilities")


@bp.route("/sops/<int:sop_id>/approve", methods=["POST"])
@roles_required("admin", "manager")
def approve_sop_route(sop_id):
    sop = db.get_or_404(OperationalSopVersion, sop_id)
    try:
        approve_sop(sop, g.current_user.id)
        db.session.commit()
        flash(f"SOP and responsibility plan version {sop.version_number} approved and locked.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return _item_redirect(sop.item_id, "history")


@bp.route("/sops/<int:sop_id>/revise", methods=["POST"])
@roles_required("admin", "manager")
def revise_sop(sop_id):
    source = db.get_or_404(OperationalSopVersion, sop_id)
    existing = next((row for row in source.item.sop_versions if row.status == "draft"), None)
    if existing:
        flash("This item already has a draft SOP revision.", "error")
    else:
        clone_sop(source)
        db.session.commit()
        flash("New draft SOP and responsibility plan created from the approved version.", "success")
    return _item_redirect(source.item_id, "sop")


@bp.route("/skills", methods=["POST"])
@roles_required("admin", "manager")
def add_skill():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Skill name is required.", "error")
    elif OperationalSkill.query.filter(db.func.lower(OperationalSkill.name) == name.lower()).first():
        flash("That skill already exists.", "error")
    else:
        db.session.add(
            OperationalSkill(
                name=name,
                description=(request.form.get("description") or "").strip() or None,
            )
        )
        db.session.commit()
        flash("Skill added.", "success")
    return redirect(url_for("operations.index", _anchor="capabilities"))


@bp.route("/certifications", methods=["POST"])
@roles_required("admin", "manager")
def add_certification():
    employee = db.session.get(User, request.form.get("employee_id", type=int))
    skill = db.session.get(OperationalSkill, request.form.get("skill_id", type=int))
    if not employee or not employee.active or not skill:
        flash("Choose an active employee and skill.", "error")
    else:
        existing = EmployeeOperationalSkill.query.filter_by(
            user_id=employee.id, skill_id=skill.id, status="active"
        ).all()
        for row in existing:
            row.status = "superseded"
            row.valid_to = date.today()
        db.session.add(
            EmployeeOperationalSkill(
                user_id=employee.id,
                skill_id=skill.id,
                competency_level=min(5, max(1, _safe_int(request.form.get("competency_level"), 1))),
                status="active",
                valid_from=_date_value(request.form.get("valid_from")) or date.today(),
                valid_to=_date_value(request.form.get("valid_to")),
                certified_by_user_id=g.current_user.id,
            )
        )
        db.session.commit()
        flash("Skill certification saved with effective dates.", "success")
    return redirect(url_for("operations.index", _anchor="capabilities"))


@bp.route("/certifications/<int:certification_id>/end", methods=["POST"])
@roles_required("admin", "manager")
def end_certification(certification_id):
    row = db.get_or_404(EmployeeOperationalSkill, certification_id)
    row.status = "expired"
    row.valid_to = date.today()
    db.session.commit()
    flash("Certification ended; its history remains available.", "success")
    return redirect(url_for("operations.index", _anchor="capabilities"))


@bp.route("/items/<int:item_id>/profile.json")
@roles_required("admin", "manager")
def item_profile_json(item_id):
    return jsonify(profile_payload(db.get_or_404(OperationalItem, item_id)))
