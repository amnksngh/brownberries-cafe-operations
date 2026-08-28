import unittest

from app.inventory_catalog import CATALOG_ITEMS, INVENTORY_CATEGORIES


class InventoryCatalogTests(unittest.TestCase):
    def test_catalog_is_bilingual_unique_and_well_classified(self):
        names = [row["name"] for row in CATALOG_ITEMS]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 250)
        for row in CATALOG_ITEMS:
            self.assertEqual(row["name"], f'{row["english"]} ({row["hindi"]})')
            self.assertTrue(row["hindi"])
            self.assertIn(row["category"], INVENTORY_CATEGORIES)
            self.assertIn(row["item_type"], {"perishable", "non_perishable"})
            self.assertTrue(row["area"])
            self.assertTrue(row["unit"])

    def test_requested_core_items_are_present_with_corrected_names(self):
        english_names = {row["english"] for row in CATALOG_ITEMS}
        required = {
            "Potato",
            "Cucumber",
            "Tomato",
            "Fresh Green Chilli",
            "Fresh Coriander Leaves",
            "Onion",
            "Cabbage",
            "Mint Leaves",
            "Red Capsicum",
            "Yellow Capsicum",
            "Spinach",
            "Garlic",
            "Carrot",
            "Pumpkin",
            "Bottle Gourd",
            "Lemon",
            "Cauliflower",
            "Ginger",
            "Broccoli",
            "French Beans",
            "Lettuce",
            "Beetroot",
            "Mushroom",
            "Refined Oil",
            "Black Pepper",
            "Asafoetida",
            "Tandoori Spread Sauce",
            "Pav Bread",
            "Sandwich Bread",
            "Burger Buns",
            "Curd",
            "Fresh Milk",
            "Paneer",
            "Desi Ghee",
            "Mozzarella Cheese",
            "Sweet Corn",
            "Green Chilli Sauce",
            "Red Chilli Sauce",
            "Tomato Ketchup",
            "Soy Sauce",
            "Chaat Masala",
            "Chilli Flakes",
            "Cumin Seeds",
            "Parcel Box 350 mL",
            "Parcel Box 500 mL",
            "Parcel Box 650 mL",
            "Transparent Sipper Glass 250 mL",
            "Transparent Sipper Glass 320 mL",
        }
        self.assertTrue(required.issubset(english_names))


if __name__ == "__main__":
    unittest.main()
