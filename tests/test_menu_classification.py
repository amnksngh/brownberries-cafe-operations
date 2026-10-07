from datetime import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from flask import Flask, g
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import MenuCategory, MenuItem, MenuNavGroup, MenuNavSection, InventoryRecipe, OperationalItem, Workstation
from app.workstation_setup import initialize_workstations
from app.cafe import _ensure_workstations_seeded
from app.menu_classification import backfill_menu_classification, is_customer_visible, navigation_label, apply_navigation_filter
from app.menu_schedule import menu_item_serving_periods, menu_item_window_is_open
from app.main import _is_public_menu_item
from app.cafe import bp, _apply_menu_item_form_values, _menu_item_category_names_for_stats
from app.mobile_staff import _menu_payload
from app.menu_navigation import load_menu_navigation_configuration


class MenuClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = Flask(__name__, instance_path=self.temp.name)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(bp)
        self.app.before_request(lambda: setattr(g, "current_user", SimpleNamespace(role="admin", active=True)))
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([MenuCategory(id=1, name="Breakfast"), MenuCategory(id=2, name="Coffee"), MenuCategory(id=3, name="Utility")])
        group = MenuNavGroup(id=1, slug="beverages", label="Beverages")
        self.section = MenuNavSection(id=12, group=group, slug="coffee", label="Hot Coffee")
        db.session.add(self.section)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp.cleanup()

    def item(self, primary=2, memberships="[2]", **kwargs):
        item = MenuItem(name="Coffee", item_type="Beverage", price=100, category_id=primary,
                        category_ids_json=memberships, prep_station="", navigation_section_id=12, **kwargs)
        db.session.add(item)
        db.session.commit()
        return item

    def test_migration_preserves_legacy_fields_navigation_and_visibility(self):
        fixtures = [(1, "[1]", "breakfast", True), (2, "[1,2]", "both", True),
                    (2, "[2]", "regular", True), (3, "[3]", "regular", False),
                    (2, "broken", "regular", True), (2, "[3]", "regular", False)]
        for primary, memberships, hours, visible in fixtures:
            item = self.item(primary, memberships)
            backfill_menu_classification()
            self.assertEqual((item.serving_hours, item.customer_visible), (hours, visible))
            self.assertEqual((item.category_id, item.category_ids_json, item.navigation_section_id), (primary, memberships, 12))

    def test_backfill_does_not_overwrite_explicit_edits(self):
        item = self.item(serving_hours="breakfast", customer_visible=False)
        backfill_menu_classification()
        backfill_menu_classification()
        self.assertEqual((item.serving_hours, item.customer_visible), ("breakfast", False))

    def test_explicit_hours_override_categories_and_both_is_not_all_day(self):
        item = self.item(serving_hours="both", customer_visible=True)
        self.assertEqual(menu_item_serving_periods(item), ("breakfast", "regular"))
        for hour, expected in [(7, False), (8, True), (11, True), (12, True), (22, True), (23, False)]:
            self.assertEqual(menu_item_window_is_open(item, time(hour)), expected)
        item.serving_hours = "breakfast"
        self.assertFalse(menu_item_window_is_open(item, time(13)))

    def test_browsing_ignores_hours_but_preserves_explicit_hidden_items(self):
        item = self.item(3, "[3]", serving_hours="breakfast", customer_visible=True)
        self.assertTrue(_is_public_menu_item(item, {3: "Utility"}, respect_serving_hours=False))
        item.customer_visible = False
        self.assertFalse(_is_public_menu_item(item, {3: "Utility"}, respect_serving_hours=False))

    def test_edit_hours_preserves_legacy_classification(self):
        item = self.item(serving_hours="regular", customer_visible=True)
        form = MultiDict(dict(serving_hours="both", visibility_present="1", customer_visible="on", available="on"))
        self.assertIsNone(_apply_menu_item_form_values(item, form, MultiDict()))
        self.assertEqual(item.serving_hours, "both")
        self.assertEqual((item.category_id, item.category_ids_json), (2, "[2]"))

    def test_invalid_hours_rejected(self):
        item = self.item(serving_hours="regular")
        self.assertIsNotNone(_apply_menu_item_form_values(item, MultiDict(dict(serving_hours="all-day")), MultiDict()))
        self.assertEqual(item.serving_hours, "regular")

    def test_bulk_form_prefix_and_hidden_visibility(self):
        item = self.item(serving_hours="regular", customer_visible=True)
        form = MultiDict({"9__serving_hours": "breakfast", "9__visibility_present": "1", "9__available": "on"})
        self.assertIsNone(_apply_menu_item_form_values(item, form, MultiDict(), prefix="9__"))
        self.assertEqual(item.serving_hours, "breakfast")
        self.assertFalse(item.customer_visible)

    def test_workstation_still_restricts_explicit_hours(self):
        item = self.item(serving_hours="both", customer_visible=True)
        item.prep_station = "barista"
        g.workstation_schedule_map = {"barista": {"start_time": "14:00", "end_time": "18:00"}}
        self.assertFalse(menu_item_window_is_open(item, time(10)))
        self.assertTrue(menu_item_window_is_open(item, time(15)))
        self.assertFalse(menu_item_window_is_open(item, time(19)))

    def test_mobile_menu_respects_explicit_visibility(self):
        item = self.item(serving_hours="both", customer_visible=False)
        self.assertIsNone(_menu_payload(item))
        item.customer_visible = True
        self.assertEqual(_menu_payload(item)["category_names"], ["Beverages / Hot Coffee"])

    def test_navigation_filters_use_exact_section_and_reports_single_placement(self):
        item = self.item()
        self.assertEqual(apply_navigation_filter(MenuItem.query, 2).count(), 0)
        self.assertEqual(apply_navigation_filter(MenuItem.query, 12).count(), 1)
        self.assertEqual(navigation_label(item), "Beverages / Hot Coffee")
        self.assertEqual(_menu_item_category_names_for_stats(item, {}), ["Beverages / Hot Coffee"])

    def test_grid_single_save_json_and_clear_optional_fields(self):
        item = self.item(description="Old description", calories=100)
        response = self.app.test_client().post(f"/cafe/menu/items/{item.id}/update", data={
            "name": "Updated Coffee", "price": "125", "description": "", "calories": "", "available": "on"
        }, headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertEqual((item.name, item.price, item.description, item.calories), ("Updated Coffee", 125, None, None))

    def test_grid_bulk_save_is_atomic_on_invalid_second_row(self):
        first, second = self.item(), self.item()
        data = {"item_ids": [str(first.id), str(second.id)], f"{first.id}__price": "125", f"{second.id}__price": "-2"}
        client = self.app.test_client()
        response = client.post("/cafe/menu/items/bulk-update", data=data, headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])
        self.assertEqual(first.price, 100)
        data[f"{second.id}__price"] = "150"
        response = client.post("/cafe/menu/items/bulk-update", data=data, headers={"Accept": "application/json"})
        self.assertTrue(response.json["ok"])
        self.assertEqual((first.price, second.price), (125, 150))

    def test_navigation_reorder_and_single_default(self):
        self.section.is_default = True
        other = MenuNavSection(id=13, group_id=1, slug="tea", label="Tea", active=True)
        db.session.add(other)
        db.session.commit()
        response = self.app.test_client().post('/cafe/menu/navigation/groups/1/layout', json={
            'section_ids': [13, 12], 'default_section_id': 13})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.section.is_default)
        self.assertTrue(other.is_default)
        config = load_menu_navigation_configuration()
        self.assertEqual([s['id'] for s in config['groups'][0]['sections']], [13, 12])
        self.assertEqual(config['groups'][0]['default_section'], 'tea')

    def test_permanent_delete_requires_archive_and_confirmation(self):
        item = self.item()
        client = self.app.test_client()
        url = f'/cafe/menu/items/{item.id}/permanent-delete'
        self.assertEqual(client.post(url, data={'confirm_permanent':'yes'}).status_code, 400)
        item.is_deleted = True
        db.session.commit()
        self.assertEqual(client.post(url).status_code, 400)
        self.assertIsNotNone(db.session.get(MenuItem, item.id))

    def test_permanent_delete_preserves_operational_profile(self):
        item = self.item(is_deleted=True)
        item_id = item.id
        profile = OperationalItem(internal_code='test-delete', name='Coffee', menu_item_id=item_id)
        db.session.add(profile)
        db.session.commit()
        response = self.app.test_client().post(f'/cafe/menu/items/{item_id}/permanent-delete', data={'confirm_permanent':'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(MenuItem, item_id))
        self.assertIsNotNone(db.session.get(OperationalItem, profile.id))
        self.assertIsNone(profile.menu_item_id)

    def test_permanent_delete_blocks_recipe_links(self):
        item = self.item(is_deleted=True)
        db.session.add(InventoryRecipe(menu_item_id=item.id))
        db.session.commit()
        response = self.app.test_client().post(f'/cafe/menu/items/{item.id}/permanent-delete', data={'confirm_permanent':'yes'})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(db.session.get(MenuItem, item.id))

    def test_workstation_defaults_seed_only_once_even_if_all_deleted(self):
        initialize_workstations()
        self.assertEqual(Workstation.query.count(), 2)
        Workstation.query.delete()
        db.session.commit()
        initialize_workstations()
        _ensure_workstations_seeded()
        self.assertEqual(Workstation.query.count(), 0)

    def test_existing_workstation_customizations_survive_initialization(self):
        station = Workstation(slug='kitchen', name='Custom Kitchen', active=False, display_order=99)
        db.session.add(station)
        db.session.commit()
        initialize_workstations()
        self.assertEqual(Workstation.query.count(), 1)
        self.assertEqual((station.name, station.active, station.display_order), ('Custom Kitchen', False, 99))

    def test_delete_linked_workstation_then_recreate(self):
        initialize_workstations()
        station = Workstation.query.filter_by(slug='kitchen').one()
        item = self.item()
        item.prep_station = station.slug
        profile = OperationalItem(internal_code='test-station', name='Coffee', menu_item_id=item.id, default_workstation_id=station.id)
        db.session.add(profile)
        db.session.commit()
        client = self.app.test_client()
        response = client.post(f'/cafe/menu/workstations/{station.id}/delete')
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertEqual(item.prep_station, '')
        self.assertIsNone(profile.default_workstation_id)
        initialize_workstations()
        self.assertIsNone(Workstation.query.filter_by(slug='kitchen').first())
        response = client.post('/cafe/menu/workstations', data={'name':'New Kitchen', 'slug':'kitchen'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Workstation.query.filter_by(slug='kitchen').one().name, 'New Kitchen')

    def test_navigation_reorder_rejects_missing_duplicate_and_foreign_ids(self):
        client = self.app.test_client()
        for ids in ([], [12, 12], [999], ['12']):
            response = client.post('/cafe/menu/navigation/groups/1/layout', json={
                'section_ids': ids, 'default_section_id': 12})
            self.assertEqual(response.status_code, 409)
        self.section.active = False
        db.session.commit()
        response = client.post('/cafe/menu/navigation/groups/1/layout', json={
            'section_ids': [12], 'default_section_id': 12})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
