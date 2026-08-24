from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from app.menu_navigation import build_menu_navigation


class MenuNavigationTests(unittest.TestCase):
    def test_real_catalog_shapes_map_to_expected_sections(self):
        now = datetime(2026, 8, 24)
        items = [
            SimpleNamespace(id=1, name="Mushroom duplex", item_type="Meal", created_at=now, is_brownberries_special=True),
            SimpleNamespace(id=2, name="Classic Iced Vanilla Latte", item_type="Beverage", created_at=now - timedelta(days=60), is_brownberries_special=False),
            SimpleNamespace(id=3, name="Vanilla affogato", item_type="Dessert", created_at=now - timedelta(days=10), is_brownberries_special=False),
        ]
        categories = {1: ["Starters"], 2: ["Hot & Cold Coffee"], 3: ["Desserts", "Hot & Cold Coffee"]}
        navigation = build_menu_navigation(items, categories, {1: 4, 2: 9}, now=now)

        self.assertIn("food:starters", navigation["item_tokens"][1])
        self.assertIn("beverages:cold-coffee", navigation["item_tokens"][2])
        self.assertIn("desserts:coffee-desserts", navigation["item_tokens"][3])
        self.assertIn("explore:brownberries-specials", navigation["item_tokens"][1])
        self.assertIn("explore:most-popular", navigation["item_tokens"][2])
        self.assertIn("explore:new-additions", navigation["item_tokens"][3])

    def test_mislabelled_chinese_beverage_remains_food(self):
        item = SimpleNamespace(
            id=7,
            name="Manchurian Gravy",
            item_type="Beverage",
            created_at=datetime(2026, 1, 1),
            is_brownberries_special=False,
        )
        navigation = build_menu_navigation([item], {7: ["Chinese"]}, {}, now=datetime(2026, 8, 24))
        self.assertIn("food:indo-chinese", navigation["item_tokens"][7])


if __name__ == "__main__":
    unittest.main()
