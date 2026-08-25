from datetime import date, datetime
import unittest

from flask import Flask

from app.extensions import db
from app.models import (
    CafeOrder,
    CafeOrderItem,
    CafeTable,
    MenuCategory,
    MenuItem,
    OperationalDailyOwnershipOverride,
    OperationalItem,
    OperationalRecipeLine,
    OperationalRecipeVersion,
    OperationalResponsibilityAssignment,
    OperationalResponsibilityPlanVersion,
    OperationalSopStage,
    OperationalSopStep,
    OperationalSopVersion,
    OperationalStepRoleRequirement,
    User,
    Workstation,
)
from app.operational_responsibility import (
    active_daily_ownership_override,
    apply_daily_ownership_override,
    approve_sop,
    approve_recipe,
    clone_sop,
    ensure_operational_items_seeded,
    named_responsibility_summary,
    recipe_input_is_valid,
    sop_contribution,
    restore_default_daily_ownership,
    set_daily_ownership_override,
    snapshot_active_daily_ownership,
)


class OperationalResponsibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test",
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _item(self, code, name, production_type="single_stage"):
        row = OperationalItem(
            internal_code=code,
            name=name,
            item_type="prepared_item",
            production_type=production_type,
            production_route="prepare_on_order",
            internally_produced=True,
            active=True,
        )
        db.session.add(row)
        db.session.flush()
        return row

    def _sop_with_role(self, item, role, active_seconds, yield_quantity=1):
        sop = OperationalSopVersion(
            item_id=item.id,
            version_number=1,
            name=f"{item.name} SOP",
            yield_quantity=yield_quantity,
            yield_unit="g" if yield_quantity > 1 else "portion",
            status="draft",
        )
        db.session.add(sop)
        db.session.flush()
        stage = OperationalSopStage(sop_version_id=sop.id, name="Preparation", sequence=1)
        db.session.add(stage)
        db.session.flush()
        step = OperationalSopStep(
            stage_id=stage.id,
            sequence=1,
            instruction="Prepare",
            active_time_seconds=active_seconds,
            passive_time_seconds=0,
            criticality_weight=1,
        )
        db.session.add(step)
        db.session.flush()
        db.session.add(
            OperationalStepRoleRequirement(
                step_id=step.id,
                role_name=role,
                participation_factor=1,
                skill_weight=1,
                headcount=1,
            )
        )
        db.session.flush()
        return sop

    def test_existing_menu_visibility_is_unchanged_by_additive_backfill(self):
        category = MenuCategory(name="Food")
        station = Workstation(slug="kitchen", name="Kitchen", active=True)
        db.session.add_all([category, station])
        db.session.flush()
        visible = MenuItem(
            category_id=category.id,
            item_type="Meal",
            name="Visible Dish",
            price=100,
            available=True,
            is_deleted=False,
            prep_station="kitchen",
        )
        hidden = MenuItem(
            category_id=category.id,
            item_type="Meal",
            name="Hidden Dish",
            price=100,
            available=False,
            is_deleted=True,
            prep_station="kitchen",
        )
        db.session.add_all([visible, hidden])
        db.session.commit()

        ensure_operational_items_seeded()

        self.assertTrue(db.session.get(MenuItem, visible.id).available)
        self.assertFalse(db.session.get(MenuItem, visible.id).is_deleted)
        self.assertFalse(db.session.get(MenuItem, hidden.id).available)
        self.assertTrue(db.session.get(MenuItem, hidden.id).is_deleted)
        self.assertEqual(OperationalItem.query.filter_by(menu_item_id=visible.id).count(), 1)
        self.assertEqual(OperationalItem.query.filter_by(menu_item_id=hidden.id).count(), 1)

    def test_component_labour_is_inherited_by_consumed_quantity(self):
        sauce = self._item("COMP-SAUCE", "Sauce", "prepared_component")
        pasta = self._item("MENU-PASTA", "Pasta", "composite_item")
        self._sop_with_role(sauce, "prep_chef", 6000, yield_quantity=5000)
        self._sop_with_role(pasta, "pasta_chef", 300, yield_quantity=1)
        recipe = OperationalRecipeVersion(
            item_id=pasta.id,
            version_number=1,
            yield_quantity=1,
            yield_unit="portion",
            status="draft",
        )
        db.session.add(recipe)
        db.session.flush()
        db.session.add(
            OperationalRecipeLine(
                recipe_version_id=recipe.id,
                input_item_id=sauce.id,
                quantity=100,
                unit="g",
                yield_loss_percent=0,
                preparation_timing="advance_batch",
            )
        )
        db.session.commit()

        result = sop_contribution(pasta)
        by_role = {row["role_key"]: row for row in result["rows"]}
        self.assertEqual(result["labour_seconds"], 420)
        self.assertEqual(by_role["prep_chef"]["labour_seconds"], 120)
        self.assertEqual(by_role["pasta_chef"]["labour_seconds"], 300)
        self.assertAlmostEqual(sum(row["labour_percent"] for row in result["rows"]), 100, places=1)

    def test_approved_sop_is_closed_when_cloned_revision_is_approved(self):
        admin = User(
            full_name="Admin",
            email="admin@example.test",
            password_hash="x",
            role="admin",
            active=True,
        )
        item = self._item("LATTE", "Latte")
        db.session.add(admin)
        db.session.flush()
        first = self._sop_with_role(item, "barista", 120)
        approve_sop(first, admin.id)
        db.session.commit()
        self.assertEqual(first.status, "approved")
        self.assertIsNotNone(first.effective_from)

        revision = clone_sop(first)
        db.session.flush()
        revision.stages[0].steps[0].active_time_seconds = 150
        approve_sop(revision, admin.id)
        db.session.commit()

        self.assertEqual(revision.version_number, 2)
        self.assertEqual(revision.status, "approved")
        self.assertIsNotNone(first.effective_to)
        self.assertLessEqual(first.effective_to, datetime.utcnow())

    def test_recipe_cycles_are_rejected(self):
        parent = self._item("PARENT", "Parent", "composite_item")
        component = self._item("CHILD", "Child", "prepared_component")
        child_recipe = OperationalRecipeVersion(
            item_id=component.id,
            version_number=1,
            yield_quantity=1,
            yield_unit="portion",
            status="draft",
        )
        db.session.add(child_recipe)
        db.session.flush()
        db.session.add(
            OperationalRecipeLine(
                recipe_version_id=child_recipe.id,
                input_item_id=parent.id,
                quantity=1,
                unit="portion",
            )
        )
        db.session.commit()

        self.assertFalse(recipe_input_is_valid(parent.id, component.id))
        self.assertFalse(recipe_input_is_valid(parent.id, parent.id))

    def test_named_ticket_responsibility_inherits_component_effort(self):
        admin = User(
            full_name="Approving Admin",
            email="approver@example.test",
            password_hash="x",
            role="admin",
            active=True,
        )
        pasta_chef = User(
            full_name="Pasta Chef",
            email="pasta@example.test",
            password_hash="x",
            role="staff",
            active=True,
        )
        prep_chef = User(
            full_name="Prep Chef",
            email="prep@example.test",
            password_hash="x",
            role="staff",
            active=True,
        )
        backup = User(
            full_name="Backup Chef",
            email="backup@example.test",
            password_hash="x",
            role="staff",
            active=True,
        )
        db.session.add_all([admin, pasta_chef, prep_chef, backup])
        db.session.flush()
        sauce = self._item("COMP-SAUCE-LIVE", "Live Sauce", "prepared_component")
        pasta = self._item("MENU-PASTA-LIVE", "Live Pasta", "composite_item")
        sauce_sop = self._sop_with_role(sauce, "prep_chef", 6000, yield_quantity=5000)
        pasta_sop = self._sop_with_role(pasta, "pasta_chef", 300, yield_quantity=1)

        sauce_requirement = sauce_sop.stages[0].steps[0].role_requirements[0]
        pasta_requirement = pasta_sop.stages[0].steps[0].role_requirements[0]
        sauce_plan = OperationalResponsibilityPlanVersion(
            sop_version_id=sauce_sop.id, version_number=1, status="draft"
        )
        pasta_plan = OperationalResponsibilityPlanVersion(
            sop_version_id=pasta_sop.id, version_number=1, status="draft"
        )
        db.session.add_all([sauce_plan, pasta_plan])
        db.session.flush()
        db.session.add_all(
            [
                OperationalResponsibilityAssignment(
                    plan_version_id=sauce_plan.id,
                    step_role_requirement_id=sauce_requirement.id,
                    employee_id=prep_chef.id,
                    assignment_type="primary",
                ),
                OperationalResponsibilityAssignment(
                    plan_version_id=sauce_plan.id,
                    step_role_requirement_id=sauce_requirement.id,
                    employee_id=backup.id,
                    assignment_type="backup",
                ),
                OperationalResponsibilityAssignment(
                    plan_version_id=pasta_plan.id,
                    step_role_requirement_id=pasta_requirement.id,
                    employee_id=pasta_chef.id,
                    assignment_type="primary",
                ),
            ]
        )
        recipe = OperationalRecipeVersion(
            item_id=pasta.id,
            version_number=1,
            yield_quantity=1,
            yield_unit="portion",
            status="draft",
        )
        db.session.add(recipe)
        db.session.flush()
        db.session.add(
            OperationalRecipeLine(
                recipe_version_id=recipe.id,
                input_item_id=sauce.id,
                quantity=100,
                unit="g",
                yield_loss_percent=0,
                preparation_timing="advance_batch",
            )
        )
        approve_sop(sauce_sop, admin.id)
        approve_sop(pasta_sop, admin.id)
        approve_recipe(recipe, admin.id)
        db.session.commit()

        result = named_responsibility_summary(pasta)
        by_name = {row["name"]: row for row in result["contributors"]}
        self.assertEqual(result["primary"]["name"], "Pasta Chef")
        self.assertEqual(result["total_labour_seconds"], 420)
        self.assertEqual(by_name["Pasta Chef"]["seconds"], 300)
        self.assertEqual(by_name["Pasta Chef"]["percentage"], 71.43)
        self.assertEqual(by_name["Prep Chef"]["seconds"], 120)
        self.assertEqual(by_name["Prep Chef"]["percentage"], 28.57)
        self.assertEqual([row["name"] for row in result["backups"]], ["Backup Chef"])
        self.assertEqual(result["unassigned_percentage"], 0)

    def test_ticket_responsibility_uses_legacy_owner_until_sop_is_approved(self):
        legacy_owner = User(
            full_name="Legacy Barista",
            email="legacy@example.test",
            password_hash="x",
            role="staff",
            active=True,
        )
        item = self._item("LATTE-LEGACY", "Latte Legacy")
        db.session.add(legacy_owner)
        db.session.commit()

        result = named_responsibility_summary(item, fallback_user=legacy_owner)

        self.assertEqual(result["status"], "assigned")
        self.assertEqual(result["primary"]["name"], "Legacy Barista")
        self.assertEqual(result["contributors"][0]["percentage"], 100)
        self.assertIn("Legacy menu responsibility", result["basis"])

    def test_approved_unassigned_work_is_explicitly_unattributed(self):
        admin = User(
            full_name="Admin",
            email="unassigned-admin@example.test",
            password_hash="x",
            role="admin",
            active=True,
        )
        item = self._item("UNASSIGNED", "Unassigned Dish")
        db.session.add(admin)
        db.session.flush()
        sop = self._sop_with_role(item, "chef", 90)
        approve_sop(sop, admin.id)
        db.session.commit()

        result = named_responsibility_summary(item, fallback_user=admin)

        self.assertEqual(result["status"], "unassigned")
        self.assertIsNone(result["primary"])
        self.assertEqual(result["contributors"], [])
        self.assertEqual(result["unassigned_percentage"], 100)

    def test_daily_override_covers_clicked_and_future_tickets_then_restores_default(self):
        category = MenuCategory(name="Beverages")
        table = CafeTable(name="T01", qr_slug="t01-test", seating_capacity=2)
        primary = User(
            full_name="Default Barista",
            email="default-barista@example.test",
            password_hash="x",
            role="barista",
            active=True,
        )
        backup = User(
            full_name="Backup Barista",
            email="backup-barista@example.test",
            password_hash="x",
            role="barista",
            active=True,
        )
        second = User(
            full_name="Second Barista",
            email="second-barista@example.test",
            password_hash="x",
            role="barista",
            active=True,
        )
        db.session.add_all([category, table, primary, backup, second])
        db.session.flush()
        menu_item = MenuItem(
            category_id=category.id,
            item_type="Coffee",
            name="Classic Cold Coffee",
            price=150,
            prep_station="barista",
            chef_user_id=primary.id,
        )
        db.session.add(menu_item)
        db.session.flush()
        order = CafeOrder(
            table_id=table.id,
            ordered_by_user_id=primary.id,
            status="open",
        )
        db.session.add(order)
        db.session.flush()
        clicked = CafeOrderItem(
            order_id=order.id,
            menu_item_id=menu_item.id,
            quantity=1,
            unit_price=150,
            approval_status="approved",
            prep_status="pending",
        )
        db.session.add(clicked)
        db.session.flush()
        service_date = date.today()

        first_override = set_daily_ownership_override(
            clicked,
            service_date,
            [(backup, 100)],
            reason="leave",
            actor_id=primary.id,
        )
        db.session.commit()
        self.assertEqual(clicked.responsibility_assignment_mode, "override")
        self.assertEqual(active_daily_ownership_override(menu_item.id, service_date).id, first_override.id)

        later_order = CafeOrder(
            table_id=table.id,
            ordered_by_user_id=primary.id,
            status="open",
        )
        db.session.add(later_order)
        db.session.flush()
        later_ticket = CafeOrderItem(
            order_id=later_order.id,
            menu_item_id=menu_item.id,
            quantity=1,
            unit_price=150,
        )
        db.session.add(later_ticket)
        snapshot_active_daily_ownership(later_ticket, service_date)
        db.session.flush()
        self.assertEqual(later_ticket.responsibility_override_id, first_override.id)

        shared_override = set_daily_ownership_override(
            clicked,
            service_date,
            [(backup, 60), (second, 40)],
            reason="multiple_staff",
            actor_id=primary.id,
        )
        db.session.commit()
        self.assertIsNotNone(first_override.effective_to)
        self.assertEqual(later_ticket.responsibility_override_id, first_override.id)
        base = named_responsibility_summary(None, fallback_user=primary)
        summary = apply_daily_ownership_override(base, shared_override)
        self.assertEqual(summary["primary"]["name"], "Backup Barista")
        self.assertEqual(
            [(row["name"], row["percentage"]) for row in summary["contributors"]],
            [("Backup Barista", 60), ("Second Barista", 40)],
        )

        latest_order = CafeOrder(
            table_id=table.id,
            ordered_by_user_id=primary.id,
            status="open",
        )
        db.session.add(latest_order)
        db.session.flush()
        latest_ticket = CafeOrderItem(
            order_id=latest_order.id,
            menu_item_id=menu_item.id,
            quantity=1,
            unit_price=150,
        )
        db.session.add(latest_ticket)
        snapshot_active_daily_ownership(latest_ticket, service_date)
        db.session.flush()
        self.assertEqual(latest_ticket.responsibility_override_id, shared_override.id)

        restore_default_daily_ownership(
            clicked,
            service_date,
            actor_id=primary.id,
        )
        db.session.commit()
        self.assertEqual(clicked.responsibility_assignment_mode, "default")
        self.assertEqual(clicked.responsibility_override_id, shared_override.id)
        self.assertIsNone(active_daily_ownership_override(menu_item.id, service_date))
        self.assertEqual(latest_ticket.responsibility_assignment_mode, "override")
        self.assertEqual(OperationalDailyOwnershipOverride.query.count(), 2)

        after_restore_order = CafeOrder(
            table_id=table.id,
            ordered_by_user_id=primary.id,
            status="open",
        )
        db.session.add(after_restore_order)
        db.session.flush()
        after_restore_ticket = CafeOrderItem(
            order_id=after_restore_order.id,
            menu_item_id=menu_item.id,
            quantity=1,
            unit_price=150,
        )
        db.session.add(after_restore_ticket)
        snapshot_active_daily_ownership(after_restore_ticket, service_date)
        db.session.flush()
        self.assertEqual(after_restore_ticket.responsibility_assignment_mode, "default")
        self.assertIsNone(after_restore_ticket.responsibility_override_id)


if __name__ == "__main__":
    unittest.main()
