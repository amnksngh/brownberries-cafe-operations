from datetime import time
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from flask import Flask

from app.menu_schedule import menu_item_window_is_open, time_window_is_open


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

    def test_items_use_their_assigned_default_window(self):
        with TemporaryDirectory() as instance_path:
            app = Flask(__name__, instance_path=instance_path)
            regular_item = SimpleNamespace(serving_period="regular", prep_station="")
            breakfast_item = SimpleNamespace(serving_period="breakfast", prep_station="")
            with app.app_context():
                self.assertFalse(menu_item_window_is_open(regular_item, time(10, 30)))
                self.assertTrue(menu_item_window_is_open(breakfast_item, time(10, 30)))
                self.assertTrue(menu_item_window_is_open(regular_item, time(11, 30)))
                self.assertTrue(menu_item_window_is_open(breakfast_item, time(11, 30)))
                self.assertTrue(menu_item_window_is_open(regular_item, time(12, 30)))
                self.assertFalse(menu_item_window_is_open(breakfast_item, time(12, 30)))


if __name__ == "__main__":
    unittest.main()
