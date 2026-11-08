import unittest

from model_ledger import Ledger, Price, UnknownModel, Usage

PRICES = {"small": Price(0.2, 0.6), "large": Price(3.0, 15.0)}


class LedgerTests(unittest.TestCase):
    def test_cost_uses_input_and_output_rates(self):
        ledger = Ledger(PRICES)
        self.assertAlmostEqual(ledger.cost(Usage("a", "large", 2000, 1000, "2026-01-01")), 21.0)

    def test_unknown_model_raises(self):
        with self.assertRaises(UnknownModel):
            Ledger(PRICES).record(Usage("a", "ghost", 1, 1, "2026-01-01"))

    def test_rollups(self):
        ledger = Ledger(PRICES)
        ledger.record(Usage("a", "small", 1000, 0, "2026-01-01"))
        ledger.record(Usage("b", "large", 1000, 0, "2026-01-01"))
        self.assertEqual(ledger.by_team(), {"a": 0.2, "b": 3.0})
        self.assertEqual(ledger.by_model(), {"large": 3.0, "small": 0.2})
        self.assertAlmostEqual(ledger.total(), 3.2)
        self.assertAlmostEqual(ledger.total("a"), 0.2)

    def test_over_budget(self):
        ledger = Ledger(PRICES, budgets={"a": 1.0, "b": 5.0})
        ledger.record(Usage("a", "large", 1000, 0, "2026-01-01"))
        self.assertEqual(ledger.over_budget(), {"a": (3.0, 1.0)})

    def test_spike_detected(self):
        ledger = Ledger(PRICES)
        for day in range(1, 6):
            ledger.record(Usage("a", "small", 1000, 0, f"2026-01-0{day}"))
        ledger.record(Usage("a", "small", 100000, 0, "2026-01-06"))
        self.assertEqual(ledger.spikes(), ["2026-01-06"])

    def test_flat_history_does_not_flag(self):
        ledger = Ledger(PRICES)
        for day in range(1, 8):
            ledger.record(Usage("a", "small", 1000, 0, f"2026-01-0{day}"))
        self.assertEqual(ledger.spikes(), [])


if __name__ == "__main__":
    unittest.main()
