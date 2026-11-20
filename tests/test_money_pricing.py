import random
import unittest
from decimal import Decimal as D

from model_ledger import MoneyError, NoPriceOnDate, UnknownModel, allocate, dec, parse_book, quantize
from model_ledger.money import canonical, fmt
from tests import oracles


class TestMoney(unittest.TestCase):
    def test_dec_reads_numbers_as_written(self):
        self.assertEqual(dec(0.1), D("0.1"))
        self.assertEqual(dec("3.50"), D("3.50"))
        self.assertEqual(dec(7), D(7))
        for bad in (True, None, float("nan"), float("inf"), "x", [1]):
            with self.subTest(bad=bad), self.assertRaises(MoneyError):
                dec(bad)

    def test_quantize_rounds_half_up(self):
        self.assertEqual(quantize(D("2.345")), D("2.35"))
        self.assertEqual(quantize(D("2.344999")), D("2.34"))
        self.assertEqual(fmt(D("-0.001")), "0.00")
        self.assertEqual(fmt(D("1234567.891")), "1,234,567.89")

    def test_canonical_is_one_string_per_value(self):
        self.assertEqual(canonical(D("3.50")), canonical(D("3.5")))
        self.assertEqual(canonical(D("-0.0")), "0")
        self.assertEqual(canonical(D("1E+2")), "100")

    def test_allocation_sums_exactly_and_is_within_a_cent(self):
        rng = random.Random(0)
        for _ in range(500):
            n = rng.randint(1, 8)
            weights = {f"t{i}": D(str(rng.choice([0, 1, 2, 3, 5, 7, 11, 13.5]))) for i in range(n)}
            if sum(weights.values()) == 0:
                continue
            total = D(rng.randint(0, 10_000_000)) / 100
            got = allocate(total, weights)
            self.assertEqual(sum(got.values()), total)
            wsum = sum(weights.values())
            for k, v in got.items():
                self.assertLess(abs(v - total * weights[k] / wsum), D("0.01"))
                self.assertGreaterEqual(v, 0)

    def test_allocation_is_optimal_by_brute_force(self):
        rng = random.Random(1)
        for _ in range(300):
            n = rng.randint(2, 7)
            weights = {f"t{i}": rng.choice([1, 2, 3, 5, 8, 13]) for i in range(n)}
            cents = rng.randint(0, 5000)
            got = allocate(D(cents) / 100, weights)
            from fractions import Fraction
            exact = {k: Fraction(w, sum(weights.values())) * cents for k, w in weights.items()}
            dev = max(abs(Fraction(int(got[k] * 100)) - exact[k]) for k in weights)
            self.assertEqual(dev, oracles.best_split_deviation(cents, weights))

    def test_allocation_ignores_input_order_and_ties_go_to_the_earlier_name(self):
        a = allocate(D("100"), {"c": 1, "a": 1, "b": 1})
        self.assertEqual(a, {"a": D("33.34"), "b": D("33.33"), "c": D("33.33")})
        self.assertEqual(a, allocate(D("100"), {"a": 1, "b": 1, "c": 1}))

    def test_allocation_edge_cases(self):
        self.assertEqual(allocate(D("0"), {"a": 1}), {"a": D("0.00")})
        self.assertEqual(allocate(D("5"), {"a": 0, "b": 3}), {"a": D("0.00"), "b": D("5.00")})
        self.assertEqual(allocate(D("0.005"), {"a": 1, "b": 1}), {"a": D("0.01"), "b": D("0.00")})     # total rounds to a cent first
        for total, w in ((D("1"), {}), (D("1"), {"a": 0}), (D("1"), {"a": -1}), (D("-1"), {"a": 1})):
            with self.subTest(total=total, w=w), self.assertRaises(MoneyError):
                allocate(total, w)


SPEC = {"input": 3, "output": 15, "cached_read": 0.3, "cache_write": 3.75}


class TestPricing(unittest.TestCase):
    def test_cost_matches_independent_computation(self):
        rng = random.Random(2)
        rate = {**SPEC, "long_threshold": 200000, "long": {"input": 6, "output": 22.5, "cached_read": 0.6, "cache_write": 7.5}}
        book = parse_book({"m": {**SPEC, "long_context": {"threshold": 200000, "input": 6, "output": 22.5, "cached_read": 0.6, "cache_write": 7.5}}}, {"batch": 0.5})
        for _ in range(2000):
            toks = {"input": rng.choice([0, 10, 5000, 150_000, 199_999, 200_000, 200_001, 900_000]), "output": rng.randint(0, 50_000),
                    "cached_read": rng.choice([0, 1000, 300_000]), "cache_write": rng.choice([0, 2000])}
            mode = rng.choice(["standard", "batch"])
            parts = book.cost_parts("m", "2026-05-05", mode, toks["input"], toks["output"], toks["cached_read"], toks["cache_write"])
            from fractions import Fraction
            self.assertEqual(Fraction(sum(parts.values())), oracles.cost_by_hand(rate, toks, 0.5 if mode == "batch" else 1))

    def test_long_context_boundary_is_strictly_greater(self):
        book = parse_book({"m": {"input": 1, "output": 1, "long_context": {"threshold": 100, "input": 2, "output": 2}}})
        at = sum(book.cost_parts("m", "2026-01-01", "standard", 100, 0).values())
        over = sum(book.cost_parts("m", "2026-01-01", "standard", 101, 0).values())
        self.assertEqual(at, D(100) / 1_000_000)
        self.assertEqual(over, D(202) / 1_000_000)

    def test_cache_classes_default_to_the_input_rate(self):
        book = parse_book({"m": {"input": 2, "output": 8}})
        self.assertEqual(sum(book.cost_parts("m", "2026-01-01", "standard", 0, 0, cached_read=1_000_000).values()), D(2))

    def test_prices_are_effective_dated_and_history_is_not_rewritten(self):
        book = parse_book({"m": [{"from": "2026-01-01", "input": 3, "output": 15}, {"from": "2026-03-01", "input": 2, "output": 10}]})
        self.assertEqual(sum(book.cost_parts("m", "2026-02-28", "standard", 1_000_000, 0).values()), D(3))
        self.assertEqual(sum(book.cost_parts("m", "2026-03-01", "standard", 1_000_000, 0).values()), D(2))
        with self.assertRaises(NoPriceOnDate):
            book.rate_for("m", "2025-12-31")
        with self.assertRaises(UnknownModel):
            book.rate_for("ghost", "2026-01-01")

    def test_modes_need_an_explicit_multiplier(self):
        book = parse_book({"m": {"input": 1, "output": 1}})
        with self.assertRaises(MoneyError):
            book.cost_parts("m", "2026-01-01", "batch", 1, 1)

    def test_book_validation(self):
        bad = [
            {}, {"m": {"input": 1}}, {"m": {"input": -1, "output": 1}}, {"m": {"input": 1, "output": 1, "x": 2}},
            {"m": [{"input": 1, "output": 1}, {"input": 2, "output": 2}]},
            {"m": [{"from": "2026-01-01", "input": 1, "output": 1}, {"from": "2026-01-01", "input": 2, "output": 2}]},
            {"m": {"from": "2026-13-01", "input": 1, "output": 1}},
            {"m": {"input": 1, "output": 1, "long_context": {"input": 2, "output": 2}}},
            {"m": {"input": 1, "output": 1, "long_context": {"threshold": 0, "input": 2, "output": 2}}},
            {"m": []},
        ]
        for spec in bad:
            with self.subTest(spec=str(spec)[:60]), self.assertRaises(MoneyError):
                parse_book(spec)
        with self.assertRaises(MoneyError):
            parse_book({"m": {"input": 1, "output": 1}}, {"batch": 0})

    def test_spec_round_trip_describes_the_book(self):
        book = parse_book({"m": [{"from": "2026-01-01", "input": 3, "output": 15}]}, {"batch": 0.5})
        spec = book.to_spec()
        self.assertEqual(spec["modes"], {"batch": "0.5", "standard": "1"})
        self.assertEqual(spec["prices"]["m"][0]["input"], "3")


if __name__ == "__main__":
    unittest.main()
