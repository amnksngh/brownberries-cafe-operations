from types import SimpleNamespace
import unittest

from app.main import _is_public_menu_item


class MenuPublicVisibilityTests(unittest.TestCase):
    def test_browsing_ignores_hours_but_not_internal_categories(self):
        public_item = SimpleNamespace(
            category_id=10,
            category_ids_json="[10]",
        )
        utility_item = SimpleNamespace(
            category_id=20,
            category_ids_json="[20]",
        )
        category_names = {10: "Soups & Beverages", 20: "Utility"}

        self.assertTrue(
            _is_public_menu_item(
                public_item,
                category_names,
                respect_serving_hours=False,
            )
        )
        self.assertFalse(
            _is_public_menu_item(
                utility_item,
                category_names,
                respect_serving_hours=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
