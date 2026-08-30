import unittest

from app.inventory_control import (
    closing_metrics,
    convert_quantity,
    coverage_metrics,
    stock_band,
)


class InventoryControlTests(unittest.TestCase):
    def test_stock_band_uses_exact_plus_minus_fifteen_percent_boundaries(self):
        self.assertEqual(stock_band(84.999, 100, 15)["status"], "deficit")
        self.assertEqual(stock_band(85, 100, 15)["status"], "adequate")
        self.assertEqual(stock_band(115, 100, 15)["status"], "adequate")
        self.assertEqual(stock_band(115.001, 100, 15)["status"], "overstock")
        self.assertEqual(stock_band(60, 100, 15)["suggested_purchase"], 40)

    def test_zero_adequate_stock_is_unconfigured_not_a_false_deficit(self):
        result = stock_band(0, 0, 15)
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["suggested_purchase"], 0)

    def test_closing_separates_recipe_use_logged_waste_and_unexplained_loss(self):
        result = closing_metrics(
            opening_stock=20,
            inbound_amount=5,
            closing_stock=10,
            expected_consumption=11,
            explicit_wastage=1.5,
            unit_price=40,
        )
        self.assertEqual(result["physical_consumption"], 15)
        self.assertEqual(result["variance"], 4)
        self.assertEqual(result["unexplained_variance"], 2.5)
        self.assertEqual(result["unexplained_wastage_value"], 100)

    def test_recipe_units_convert_only_within_compatible_families(self):
        self.assertEqual(convert_quantity(500, "g", "kg"), 0.5)
        self.assertEqual(convert_quantity(1.5, "litre", "ml"), 1500)
        self.assertEqual(convert_quantity(2, "pieces", "pcs"), 2)
        self.assertIsNone(convert_quantity(1, "kg", "litre"))
        self.assertIsNone(convert_quantity(1, "packet", "kg"))

    def test_coverage_flags_stock_that_outlasts_shelf_life(self):
        result = coverage_metrics(
            current_amount=20,
            adequate_amount=12,
            average_daily_usage=2,
            shelf_life_days=5,
        )
        self.assertEqual(result["coverage_days"], 10)
        self.assertEqual(result["target_days"], 6)
        self.assertTrue(result["spoilage_risk"])


if __name__ == "__main__":
    unittest.main()
