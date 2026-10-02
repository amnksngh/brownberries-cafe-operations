import json
from types import SimpleNamespace
import unittest

from app.payment_summary import summarize_payments


def row(id, total, method=None, split=None, group=None, ratio=1, status="paid"):
    return {"order": SimpleNamespace(id=id, total_amount=total, payment_type=method,
            payment_breakdown_json=json.dumps(split) if split is not None else None,
            settlement_group_id=group, status=status), "ratio": ratio}


def amounts(summary):
    return {r["key"]: r["amount"] for r in summary["rows"]}


class PaymentSummaryTests(unittest.TestCase):
    def test_legacy_modes_are_grouped_and_only_paid_orders_count(self):
        result = summarize_payments([row(1, 100, "Cash"), row(2, 200, "QR"),
                                    row(3, 50, "Card Tap"), row(4, 75, "Card Dip"),
                                    row(5, 500, "UPI", status="open")])
        self.assertEqual(amounts(result), {"cash": 100, "card": 125, "upi": 200})
        self.assertEqual(result["total_collected"], 425)

    def test_shared_split_is_not_counted_once_per_order(self):
        split = [{"method": "Cash", "amount": 150}, {"method": "QR", "amount": 450}]
        result = summarize_payments([row(1, 200, "Split Payment", split, "group"),
                                    row(2, 400, "Split Payment", split, "group")])
        self.assertEqual(amounts(result), {"cash": 150, "card": 0, "upi": 450})
        self.assertEqual(result["total_collected"], 600)

    def test_partial_group_and_category_filter_are_proportional(self):
        split = [{"method": "Cash", "amount": 150}, {"method": "UPI", "amount": 450}]
        result = summarize_payments([row(1, 200, "Split Payment", split, "group", ratio=0.5)], allocated=True)
        self.assertEqual(amounts(result), {"cash": 25, "card": 0, "upi": 75})
        self.assertTrue(result["allocated"])

    def test_cash_uses_net_amount_not_tendered_notes(self):
        result = summarize_payments([row(1, 80, "Cash", [{"method": "Cash", "amount": 80,
                                                       "cash_tendered": 100, "change_amount": 20}])])
        self.assertEqual(amounts(result)["cash"], 80)

    def test_unknown_or_invalid_splits_are_not_assumed_upi(self):
        broken = row(1, 100, "Split Payment")
        broken["order"].payment_breakdown_json = "{invalid"
        result = summarize_payments([broken, row(2, 50), row(3, 20, "Bank Transfer")])
        self.assertEqual(amounts(result)["unknown"], 150)
        self.assertEqual(amounts(result)["other"], 20)
        self.assertEqual(amounts(result)["upi"], 0)

    def test_paise_rounding_reconciles_and_shared_group_preserves_mix(self):
        split = [{"method": "Cash", "amount": 0.01}, {"method": "UPI", "amount": 0.02}]
        result = summarize_payments([row(i, 0.01, "Split Payment", split, "group") for i in range(3)])
        self.assertEqual(amounts(result)["cash"], 0.01)
        self.assertEqual(amounts(result)["upi"], 0.02)
        self.assertEqual(result["total_collected"], 0.03)

    def test_empty_period_still_shows_cash_card_upi(self):
        result = summarize_payments([])
        self.assertEqual(amounts(result), {"cash": 0, "card": 0, "upi": 0})
        self.assertEqual(result["total_collected"], 0)

    def test_nonfinite_or_negative_legacy_values_do_not_break_dashboard(self):
        split = [{"method": "Cash", "amount": "NaN"}]
        result = summarize_payments([row(1, 100, "Split Payment", split), row(2, -10, "Cash")])
        self.assertEqual(amounts(result)["unknown"], 100)
        self.assertEqual(result["total_collected"], 100)


if __name__ == "__main__":
    unittest.main()
