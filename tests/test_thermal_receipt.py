"""Receipt layouts use fixtures only; never submit a physical print job."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from flask import Flask, render_template


class ThermalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'))
        self.app.add_url_rule('/cafe/receipt/<int:order_id>', 'cafe.public_receipt', lambda order_id: '')

    def render_receipt(self, thermal=False, preview=False):
        order = SimpleNamespace(id=7, daily_sequence=3, display_code='T03', is_delivery=False,
                                table=SimpleNamespace(name='T08'), status='paid', payment_type='UPI')
        with self.app.test_request_context('/'):
            return render_template('cafe/receipt_public.html', thermal_receipt=thermal, is_preview=preview,
                order=order, receipt_orders=[order], receipt_lines=[dict(order_public_id='#3',
                item_name='Classic Cold Coffee', size_label='Standard', is_parcel=False, quantity=2,
                unit_price=100, line_total=200)], payment_breakdown=[dict(method='UPI', amount=200)],
                subtotal=200, settlement_total=200, packaging_charge=0, delivery_charge=0,
                service_tax_amount=0, paid_at_display='16 Oct 2026, 04:00 PM', receipt_location='Cafe',
                feedback_link='/feedback', review_link='/review')

    def test_thermal_keeps_items_payments_totals_and_content_sized_page(self):
        html = self.render_receipt(thermal=True)
        for value in ('Classic Cold Coffee', 'UPI', '200.00', 'sizeThermalPage', 'size: 80mm', 'width: 72mm'):
            self.assertIn(value, html)
        self.assertNotIn('min-height:', html)
        self.assertIn('images/cafe-logo.png', html)
        self.assertLess(html.index('<img class="receipt-logo"'), html.index('<h1>'))

    def test_standard_pdf_layout_unchanged(self):
        html = self.render_receipt()
        self.assertNotIn('sizeThermalPage', html)
        self.assertIn('margin: 14mm', html)

    def test_preview_has_no_print_button(self):
        html = self.render_receipt(preview=True)
        self.assertNotIn('onclick="window.print()"', html)
        self.assertIn('Unsettled bill preview', html)

    def test_prompt_uses_exact_order_and_consumes_url(self):
        with self.app.test_request_context('/'):
            html = render_template('cafe/receipt_print_prompt.html')
        self.assertIn('/cafe/receipt/0', html)
        self.assertIn("address.searchParams.delete('print_receipt')", html)
        self.assertIn('sessionStorage.setItem', html)
        self.assertIn("onclick = () => prompt.close()", html)


if __name__ == '__main__':
    unittest.main()
