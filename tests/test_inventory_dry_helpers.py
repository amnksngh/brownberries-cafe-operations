from datetime import date
import unittest

from flask import Flask

from app import _normalize_inventory_item_types
from app.cafe import (
    _clear_purchase_todos,
    _apply_logged_purchase_to_todos,
    _ensure_inventory_item_categories,
    _inventory_date,
    _inventory_item_status,
    _normalize_inventory_category_color,
    _normalize_inventory_category_icon,
    _normalize_inventory_item_type,
    _normalize_inventory_section,
    _purchase_request_summaries,
    _remove_purchase_todo,
    _sync_closing_purchase_recommendations,
    _toggle_purchase_todo,
)
from app.extensions import db
from app.models import (
    InventoryCategory,
    InventoryDailyClosing,
    InventoryItem,
    InventoryToPurchase,
)


class InventoryDryHelperTests(unittest.TestCase):
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

    def test_inventory_date_uses_one_safe_fallback_rule(self):
        fallback = date(2026, 8, 26)
        self.assertEqual(_inventory_date("2026-08-25", fallback), date(2026, 8, 25))
        self.assertEqual(_inventory_date("", fallback), fallback)
        self.assertEqual(_inventory_date("not-a-date", fallback), fallback)

    def test_legacy_inventory_sections_open_the_consolidated_screens(self):
        self.assertEqual(_normalize_inventory_section("items"), "items_stock")
        self.assertEqual(_normalize_inventory_section("stock_levels"), "items_stock")
        self.assertEqual(_normalize_inventory_section("categories"), "items_stock")
        self.assertEqual(_normalize_inventory_section("analytics"), "dashboard")
        self.assertEqual(_normalize_inventory_section("movements"), "audit")
        self.assertEqual(_normalize_inventory_section("unknown"), "dashboard")

    def test_stock_status_uses_adequate_quantity_and_symmetric_tolerance(self):
        item = InventoryItem(
            name="Milk",
            category_name="Dairy",
            unit="litre",
            current_amount=14,
            reorder_level=2,
            required_amount=10,
        )
        self.assertEqual(
            _inventory_item_status(item, {"stock_tolerance_percent": 15}),
            "overstock",
        )
        item.current_amount = 8
        self.assertEqual(
            _inventory_item_status(item, {"stock_tolerance_percent": 15}),
            "deficit",
        )
        item.current_amount = 10
        self.assertEqual(
            _inventory_item_status(item, {"stock_tolerance_percent": 15}),
            "adequate",
        )
        item.required_amount = 0
        self.assertEqual(
            _inventory_item_status(item, {"stock_tolerance_percent": 15}),
            "unconfigured",
        )

    def test_inventory_type_and_visual_category_values_are_normalized(self):
        self.assertEqual(_normalize_inventory_item_type("Perishable"), "perishable")
        self.assertEqual(_normalize_inventory_item_type("non-perishable"), "non_perishable")
        self.assertEqual(_normalize_inventory_item_type("unknown"), "non_perishable")
        self.assertEqual(_normalize_inventory_category_icon("leaf"), "leaf")
        self.assertEqual(_normalize_inventory_category_icon("custom-script"), "package")
        self.assertEqual(_normalize_inventory_category_color("#6CAB7A"), "#6cab7a")
        self.assertEqual(_normalize_inventory_category_color("red"), "#6cab7a")

    def test_existing_item_categories_are_preserved_and_manageable(self):
        db.session.add(
            InventoryItem(
                name="Cabbage",
                area="cafe",
                category_name="Perishable",
                unit="kg",
            )
        )
        db.session.commit()

        _ensure_inventory_item_categories()
        _ensure_inventory_item_categories()

        rows = InventoryCategory.query.filter(
            db.func.lower(InventoryCategory.name) == "perishable"
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].active)

    def test_legacy_perishability_category_is_migrated_without_losing_meaning(self):
        category = InventoryCategory(
            name="Perishable",
            icon="box",
            color="#8a735f",
            active=True,
        )
        item = InventoryItem(
            name="Fresh Cream",
            area="cafe",
            category_name="Perishable",
            item_type="non_perishable",
            unit="kg",
        )
        db.session.add_all([category, item])
        db.session.commit()

        _normalize_inventory_item_types()

        self.assertEqual(item.item_type, "perishable")
        self.assertEqual(item.category_name, "Perishable")
        self.assertFalse(category.active)

    def test_purchase_summary_reuses_one_payload_for_all_views(self):
        active_rows = [
            {
                "status": "open",
                "inventory_item_id": 10,
                "item_name": "Milk",
                "quantity_amount": 2.0,
                "quantity_unit": "litre",
                "purchase_price": 60.0,
                "workstation_slug": "barista",
                "workstation_name": "Barista Counter",
            },
            {
                "status": "open",
                "inventory_item_id": 10,
                "item_name": "Milk",
                "quantity_amount": 3.0,
                "quantity_unit": "litre",
                "purchase_price": 60.0,
                "workstation_slug": "kitchen",
                "workstation_name": "Kitchen",
            },
            {
                "status": "purchased",
                "inventory_item_id": 20,
                "item_name": "Cabbage",
                "quantity_amount": 1.0,
                "quantity_unit": "kg",
                "purchase_price": 40.0,
                "workstation_slug": "kitchen",
                "workstation_name": "Kitchen",
            },
        ]

        accumulated, by_workstation = _purchase_request_summaries(active_rows)

        self.assertEqual(len(accumulated), 1)
        self.assertEqual(accumulated[0]["item_name"], "Milk")
        self.assertEqual(accumulated[0]["quantity"], 5.0)
        self.assertEqual(accumulated[0]["estimated_cost"], 300.0)
        self.assertEqual(
            accumulated[0]["workstations"], "Barista Counter, Kitchen"
        )
        self.assertEqual([row["label"] for row in by_workstation], ["Barista Counter", "Kitchen"])

    def test_purchase_state_transitions_are_shared(self):
        first = InventoryToPurchase(
            item_name="Milk", status="open", active=True, quantity_amount=2
        )
        second = InventoryToPurchase(
            item_name="Cabbage", status="open", active=True, quantity_amount=1
        )
        db.session.add_all([first, second])
        db.session.commit()

        _toggle_purchase_todo(first, None)
        self.assertEqual(first.status, "purchased")
        self.assertIsNotNone(first.completed_at)
        _toggle_purchase_todo(first, None)
        self.assertEqual(first.status, "open")
        self.assertIsNone(first.completed_at)

        _remove_purchase_todo(first, None)
        self.assertFalse(first.active)
        self.assertEqual(first.status, "removed")
        self.assertIsNotNone(first.closed_at)

        changed = _clear_purchase_todos(None)
        self.assertEqual(changed, 1)
        self.assertFalse(second.active)
        self.assertEqual(second.status, "cleared")
        self.assertIsNotNone(second.closed_at)

    def test_closing_purchase_recommendations_are_idempotent_and_resolve(self):
        item = InventoryItem(
            name="Milk",
            area="cafe",
            category_name="Dairy",
            unit="litre",
            current_amount=4,
            required_amount=10,
        )
        closing = InventoryDailyClosing(
            closing_date=date(2026, 8, 28),
            item=item,
            opening_stock=8,
            closing_stock=4,
            stock_status="deficit",
            suggested_purchase_amount=6,
        )
        db.session.add_all([item, closing])
        db.session.commit()

        first = _sync_closing_purchase_recommendations(
            [closing], closing.closing_date, None
        )
        db.session.commit()
        second = _sync_closing_purchase_recommendations(
            [closing], closing.closing_date, None
        )
        db.session.commit()

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(InventoryToPurchase.query.filter_by(active=True).count(), 1)
        row = InventoryToPurchase.query.filter_by(active=True).one()
        self.assertEqual(row.quantity_amount, 6)
        self.assertEqual(row.source_type, "daily_closing")

        closing.stock_status = "adequate"
        closing.suggested_purchase_amount = 0
        resolved = _sync_closing_purchase_recommendations(
            [closing], closing.closing_date, None
        )
        db.session.commit()
        self.assertEqual(resolved["resolved"], 1)
        self.assertEqual(InventoryToPurchase.query.filter_by(active=True).count(), 0)

    def test_logged_purchase_completes_or_reduces_matching_requests(self):
        item = InventoryItem(name="Milk", area="cafe", unit="litre")
        db.session.add(item)
        db.session.flush()
        first = InventoryToPurchase(
            item_name=item.name,
            inventory_item_id=item.id,
            quantity_amount=2,
            quantity_unit=item.unit,
            status="open",
            active=True,
        )
        second = InventoryToPurchase(
            item_name=item.name,
            inventory_item_id=item.id,
            quantity_amount=3,
            quantity_unit=item.unit,
            status="open",
            active=True,
        )
        db.session.add_all([first, second])
        db.session.commit()

        result = _apply_logged_purchase_to_todos(item.id, 4, None)
        db.session.commit()

        self.assertEqual(result, {"completed": 1, "partial": 1})
        self.assertEqual(first.status, "purchased")
        self.assertEqual(second.status, "open")
        self.assertEqual(second.quantity_amount, 1)


if __name__ == "__main__":
    unittest.main()
