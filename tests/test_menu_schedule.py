from datetime import time
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from flask import Flask

from app.menu_schedule import (
    menu_item_serving_periods,
    menu_item_window_is_open,
    time_window_is_open,
)


class MenuScheduleTests(unittest.TestCase):
    def test_regular_hours_window(self):
        self.assertFalse(time_window_is_open("11:00", "23:00", time(10, 59)))
        self.assertTrue(time_window_is_open("11:00", "23:00", time(11, 0)))
        self.assertTrue(time_window_is_open("11:00", "23:00", time(22, 59)))
        self.assertFalse(time_window_is_open("11:00", "23:00", time(23, 0)))

    def test_breakfast_hours_overlap_regular_hours(self):
        self.assertTrue(time_window_is_open("08:00", "12:00", time(8, 0)))
        self.assertTrue(time_window_is_open("08:00", "12:00", time(11, 30)))
        self.assertFalse(time_window_is_open("08:00", "12:00", time(12, 0)))

    def test_overnight_window_is_supported(self):
        self.assertTrue(time_window_is_open("18:00", "02:00", time(23, 0)))
        self.assertTrue(time_window_is_open("18:00", "02:00", time(1, 30)))
        self.assertFalse(time_window_is_open("18:00", "02:00", time(10, 0)))

    def test_item_categories_derive_their_serving_windows(self):
        with TemporaryDirectory() as instance_path:
            app = Flask(__name__, instance_path=instance_path)
            breakfast_id = 10
            breakfast_item = SimpleNamespace(category_id=breakfast_id, category_ids_json="[10]", prep_station="")
            mixed_item = SimpleNamespace(category_id=breakfast_id, category_ids_json="[10, 20]", prep_station="")
            regular_item = SimpleNamespace(category_id=20, category_ids_json="[20, 30]", prep_station="")
            with app.app_context():
                self.assertEqual(menu_item_serving_periods(breakfast_item, breakfast_id), ("breakfast",))
                self.assertEqual(menu_item_serving_periods(mixed_item, breakfast_id), ("breakfast", "regular"))
                self.assertEqual(menu_item_serving_periods(regular_item, breakfast_id), ("regular",))
                self.assertTrue(menu_item_window_is_open(breakfast_item, time(10, 30), breakfast_id=breakfast_id))
                self.assertTrue(menu_item_window_is_open(mixed_item, time(10, 30), breakfast_id=breakfast_id))
                self.assertFalse(menu_item_window_is_open(regular_item, time(10, 30), breakfast_id=breakfast_id))
                self.assertTrue(menu_item_window_is_open(breakfast_item, time(11, 30), breakfast_id=breakfast_id))
                self.assertTrue(menu_item_window_is_open(mixed_item, time(11, 30), breakfast_id=breakfast_id))
                self.assertTrue(menu_item_window_is_open(regular_item, time(11, 30), breakfast_id=breakfast_id))
                self.assertFalse(menu_item_window_is_open(breakfast_item, time(12, 30), breakfast_id=breakfast_id))
                self.assertTrue(menu_item_window_is_open(mixed_item, time(12, 30), breakfast_id=breakfast_id))
                self.assertTrue(menu_item_window_is_open(regular_item, time(12, 30), breakfast_id=breakfast_id))


if __name__ == "__main__":
    unittest.main()
