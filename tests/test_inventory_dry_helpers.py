from datetime import date
import unittest

from flask import Flask

from app.cafe import (
    _clear_purchase_todos,
    _inventory_date,
    _purchase_request_summaries,
    _remove_purchase_todo,
    _toggle_purchase_todo,
)
from app.extensions import db
from app.models import InventoryToPurchase


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


if __name__ == "__main__":
    unittest.main()
