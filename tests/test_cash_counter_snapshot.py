import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from app.cafe import _cash_counter_snapshot


class CashCounterSnapshotTests(unittest.TestCase):
    def snapshot(self, entries):
        query = MagicMock()
        query.filter.return_value.order_by.return_value.all.return_value = entries
        with patch("app.cafe.CashCounterEntry") as model:
            model.query = query
            return _cash_counter_snapshot()

    def test_empty_drawer_preserves_unreconciled_legacy_cash(self):
        entries = [SimpleNamespace(entry_type="deposit", amount=n, denominations_json=None)
                   for n in [315, 20, 185, 230]]
        entries += [SimpleNamespace(entry_type=t, amount=500,
                                   denominations_json=json.dumps({"note_500": 1}))
                    for t in ["deposit", "withdrawal"]]
        result = self.snapshot(entries)
        self.assertEqual(result["tracked_cash"], 0)
        self.assertEqual(result["total"], 750)
        self.assertEqual(result["unallocated"], 750)

    def test_tracked_cash_matches_ledger_when_denominations_complete(self):
        result = self.snapshot([SimpleNamespace(entry_type="deposit", amount=200,
                                               denominations_json='{"note_100": 2}')])
        self.assertEqual(result["tracked_cash"], 200)
        self.assertEqual(result["unallocated"], 0)
