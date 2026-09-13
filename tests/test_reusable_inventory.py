import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask
from PIL import Image
from werkzeug.datastructures import FileStorage

from app.cafe import _record_reusable_loss_event, _save_reusable_asset_image
from app.extensions import db
from app.models import (
    ReusableInventoryAsset,
    ReusableInventoryCount,
    ReusableInventoryLossAllocation,
    ReusableInventoryLossEvent,
    ReusableInventoryPurchase,
    User,
)
from app.reusable_inventory import (
    reported_loss_split,
    reusable_count_change,
    reusable_stock_composition,
    shared_loss_allocations,
    weighted_average_unit_price,
)


class ReusableInventoryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_weighted_average_uses_all_purchase_units(self):
        self.assertEqual(weighted_average_unit_price(10, 50, 5, 80), 60.0)
        self.assertEqual(weighted_average_unit_price(0, 0, 4, 25), 25.0)

    def test_weekly_count_separates_losses_and_recoveries(self):
        loss = reusable_count_change(30, 27, 40)
        self.assertEqual(loss["lost_quantity"], 3)
        self.assertEqual(loss["recovered_quantity"], 0)
        self.assertEqual(loss["loss_value"], 120.0)

        recovery = reusable_count_change(27, 29, 40)
        self.assertEqual(recovery["lost_quantity"], 0)
        self.assertEqual(recovery["recovered_quantity"], 2)
        self.assertEqual(recovery["loss_value"], 0.0)

    def test_composition_is_always_a_hundred_percent(self):
        composition = reusable_stock_composition(40, 30)
        self.assertEqual(composition["current"], 30)
        self.assertEqual(composition["lost"], 10)
        self.assertEqual(
            composition["current_percent"] + composition["lost_percent"],
            100.0,
        )

    def test_reported_loss_is_split_without_overcharging_staff(self):
        self.assertEqual(
            reported_loss_split(100),
            {"staff_charge": 50.0, "cafe_share": 50.0},
        )
        self.assertEqual(
            reported_loss_split(99.99),
            {"staff_charge": 49.99, "cafe_share": 50.0},
        )

    def test_unreported_loss_shares_every_paise(self):
        allocations = shared_loss_allocations(100, [3, 2, 1])
        self.assertEqual([row["user_id"] for row in allocations], [1, 2, 3])
        self.assertEqual(
            [row["charge_amount"] for row in allocations], [33.34, 33.33, 33.33]
        )
        self.assertEqual(sum(row["charge_amount"] for row in allocations), 100.0)

    def test_unreported_loss_excludes_admin_privilege(self):
        admin = User(
            full_name="Cafe Admin",
            email="admin-test@example.com",
            password_hash="x",
            role="admin",
            active=True,
        )
        server = User(
            full_name="Server One",
            email="server-test@example.com",
            password_hash="x",
            role="server",
            active=True,
        )
        barista = User(
            full_name="Barista One",
            email="barista-test@example.com",
            password_hash="x",
            role="barista",
            active=True,
        )
        db.session.add_all([admin, server, barista])
        asset = ReusableInventoryAsset(
            name="Water Glass",
            purchased_quantity=10,
            current_quantity=8,
            average_unit_price=60,
        )
        db.session.add(asset)
        db.session.flush()
        count = ReusableInventoryCount(
            asset_id=asset.id,
            quantity_before=10,
            current_quantity=8,
            lost_quantity=2,
            recovered_quantity=0,
            unit_price_snapshot=60,
            loss_value=120,
        )
        db.session.add(count)
        event = _record_reusable_loss_event(
            asset=asset,
            count_row=count,
            loss_type="unreported_shortage",
            responsible_user=None,
            created_by_user=admin,
            note="Weekly shortage",
        )
        db.session.commit()

        self.assertIsInstance(event, ReusableInventoryLossEvent)
        self.assertEqual(event.shared_staff_count, 2)
        allocations = ReusableInventoryLossAllocation.query.order_by(
            ReusableInventoryLossAllocation.user_id
        ).all()
        self.assertEqual({row.user_id for row in allocations}, {server.id, barista.id})
        self.assertEqual(sum(row.charge_amount for row in allocations), 120.0)
        self.assertEqual(event.cafe_share_amount, 0.0)

    def test_purchase_and_count_history_are_preserved_separately(self):
        asset = ReusableInventoryAsset(
            name="Cappuccino Cup",
            area_scope="cafe",
            purchased_quantity=24,
            current_quantity=22,
            average_unit_price=90,
        )
        db.session.add(asset)
        db.session.flush()
        db.session.add(
            ReusableInventoryPurchase(
                asset_id=asset.id,
                quantity=24,
                unit_price=90,
                total_amount=2160,
            )
        )
        db.session.add(
            ReusableInventoryCount(
                asset_id=asset.id,
                quantity_before=24,
                current_quantity=22,
                lost_quantity=2,
                loss_value=180,
            )
        )
        db.session.commit()

        self.assertEqual(len(asset.purchases), 1)
        self.assertEqual(len(asset.stock_counts), 1)
        self.assertEqual(asset.purchases[0].total_amount, 2160)
        self.assertEqual(asset.stock_counts[0].loss_value, 180)

    def test_asset_image_is_normalized_to_webp(self):
        with TemporaryDirectory() as temp_dir:
            image_stream = BytesIO()
            Image.new("RGB", (120, 80), "#6f4a35").save(image_stream, format="PNG")
            image_stream.seek(0)
            upload = FileStorage(
                stream=image_stream,
                filename="cappuccino-cup.png",
                content_type="image/png",
            )
            image_app = Flask(__name__, static_folder=temp_dir)
            with image_app.app_context():
                image_url = _save_reusable_asset_image(upload)

            self.assertTrue(image_url.startswith("/static/uploads/inventory-reusable/"))
            saved_name = image_url.rsplit("/", 1)[-1]
            self.assertTrue(
                (Path(temp_dir) / "uploads" / "inventory-reusable" / saved_name).exists()
            )


if __name__ == "__main__":
    unittest.main()
