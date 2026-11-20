import copy
import random
import unittest
from decimal import Decimal as D

from model_ledger import (
    AuditLedger,
    DuplicateEvent,
    MoneyError,
    allocate_shared,
    fully_loaded,
    make_budget,
    make_event,
    make_shared,
    parse_book,
    status,
)
from model_ledger.ledger import Entry, entry_hash
from tests import oracles

BOOK = parse_book({"m": {"input": 1000, "output": 2000}, "n": {"input": 10, "output": 10}}, {"batch": 0.5})


def ev(day="2026-03-01", team="a", model="m", i=1000, o=0, **kw):
    return make_event({"day": day, "team": team, "model": model, "input_tokens": i, "output_tokens": o, **kw})


def filled(n=40, seed=0):
    rng = random.Random(seed)
    led = AuditLedger(BOOK)
    for k in range(n):
        led.record(ev(day=f"2026-03-{rng.randint(1, 28):02d}", team=rng.choice(["a", "b/x", "b/y", "c"]), model=rng.choice(["m", "n"]),
                      i=rng.randint(0, 5000), o=rng.randint(0, 5000), id=f"id{k}", tags={"feature": rng.choice(["f1", "f2"])}))
    return led


class TestEvents(unittest.TestCase):
    def test_validation(self):
        base = {"day": "2026-03-01", "team": "a", "model": "m", "input_tokens": 1, "output_tokens": 1}
        bad = [
            {**base, "day": "03/01/2026"}, {**base, "day": "2026-3-1"}, {**base, "team": ""}, {**base, "input_tokens": -1},
            {**base, "input_tokens": 1.5}, {**base, "output_tokens": True}, {**base, "extra": 1}, {**base, "tags": {"a": 1}},
            {**base, "id": ""}, {k: v for k, v in base.items() if k != "model"}, "nope",
        ]
        for raw in bad:
            with self.subTest(raw=str(raw)[:60]), self.assertRaises(MoneyError):
                make_event(raw)

    def test_unknown_model_and_mode_raise_instead_of_costing_zero(self):
        led = AuditLedger(BOOK)
        with self.assertRaises(KeyError):
            led.record(ev(model="ghost"))
        with self.assertRaises(MoneyError):
            led.record(ev(mode="priority"))
        self.assertEqual(led.entries, [])


class TestLedger(unittest.TestCase):
    def test_costs_and_rollups_agree(self):
        led = filled()
        total = led.total()
        self.assertEqual(sum(e.cost for e in led.entries), total)
        for dim in ("team", "model", "day", "month", "mode", "tag:feature"):
            self.assertEqual(sum(led.group(dim).values()), total, dim)
        self.assertEqual(led.group("tag:missing"), {"(none)": total})

    def test_team_tree_rolls_children_into_parents(self):
        led = filled()
        tree = led.team_tree()
        self.assertEqual(tree["b"], tree["b/x"] + tree["b/y"])
        self.assertEqual(tree["a"], led.total(team="a"))
        self.assertEqual(led.total(team="b"), tree["b"])
        self.assertNotEqual(led.total(team="b"), led.total(team="b/x"))

    def test_filters_do_not_match_name_prefixes_that_are_not_children(self):
        led = AuditLedger(BOOK)
        led.record(ev(team="eng"))
        led.record(ev(team="engineering"))
        led.record(ev(team="eng/search"))
        self.assertEqual(led.total(team="eng"), D(2))

    def test_windows_and_models(self):
        led = filled()
        a = led.total(start="2026-03-01", end="2026-03-14")
        b = led.total(start="2026-03-15", end="2026-03-31")
        self.assertEqual(a + b, led.total())
        self.assertEqual(led.total(model="m") + led.total(model="n"), led.total())

    def test_duplicates_are_refused_or_skipped(self):
        led = AuditLedger(BOOK)
        led.record(ev(id="x"))
        with self.assertRaises(DuplicateEvent):
            led.record(ev(id="x"))
        self.assertIsNone(led.record(ev(id="x"), on_duplicate="skip"))
        self.assertEqual(len(led.entries), 1)
        self.assertEqual(led.record_many([ev(id="x"), ev(id="y")], on_duplicate="skip"), 1)
        led.record(ev())
        led.record(ev())                                    # events without ids are never treated as duplicates
        self.assertEqual(len(led.entries), 4)
        with self.assertRaises(ValueError):
            led.record(ev(id="z"), on_duplicate="ignore")

    def test_tokens_by_class(self):
        led = AuditLedger(BOOK)
        led.record(ev(i=10, o=5, cached_read_tokens=3, cache_write_tokens=2))
        self.assertEqual(led.tokens(), {"input": 10, "output": 5, "cached_read": 3, "cache_write": 2})

    def test_unknown_dimension(self):
        with self.assertRaises(ValueError):
            filled().group("colour")


class TestChain(unittest.TestCase):
    def test_head_matches_an_independent_recomputation(self):
        led = filled(30)
        pairs = [(e.event.to_dict(), _canon(e.cost)) for e in led.entries]
        self.assertEqual(led.head, oracles.chain_head(pairs))

    def test_verify_passes_on_untouched_ledger_and_head_is_deterministic(self):
        a, b = filled(25, 3), filled(25, 3)
        self.assertTrue(a.verify())
        self.assertEqual(a.head, b.head)
        self.assertNotEqual(a.head, filled(25, 4).head)

    def test_any_change_is_detected(self):
        base = filled(30, 5)
        head = base.head
        rng = random.Random(0)
        detected = 0
        trials = 200
        for _ in range(trials):
            led = AuditLedger(BOOK)
            led.entries = copy.deepcopy(base.entries)
            i = rng.randrange(len(led.entries))
            old = led.entries[i]
            kind = rng.choice(["tokens", "team", "day", "cost", "drop", "swap"])
            if kind == "tokens":
                e = old.event.__class__(**{**old.event.__dict__, "input_tokens": old.event.input_tokens + 1})
                led.entries[i] = Entry(e, old.cost, old.parts, old.hash)
            elif kind == "team":
                e = old.event.__class__(**{**old.event.__dict__, "team": old.event.team + "x"})
                led.entries[i] = Entry(e, old.cost, old.parts, old.hash)
            elif kind == "day":
                e = old.event.__class__(**{**old.event.__dict__, "day": "2026-04-01"})
                led.entries[i] = Entry(e, old.cost, old.parts, old.hash)
            elif kind == "cost":
                led.entries[i] = Entry(old.event, old.cost + D("0.000001"), old.parts, old.hash)
            elif kind == "drop":
                del led.entries[i]
            else:
                j = (i + 1) % len(led.entries)
                if led.entries[i].event == led.entries[j].event:
                    detected += 1
                    continue
                led.entries[i], led.entries[j] = led.entries[j], led.entries[i]
            detected += (not led.verify()) or led.head != head
        self.assertEqual(detected, trials)

    def test_hash_binds_the_previous_entry(self):
        e = ev()
        self.assertNotEqual(entry_hash("0" * 64, e, D(1)), entry_hash("1" * 64, e, D(1)))
        self.assertNotEqual(entry_hash("0" * 64, e, D(1)), entry_hash("0" * 64, e, D("1.000001")))
        self.assertEqual(entry_hash("0" * 64, e, D("1.0")), entry_hash("0" * 64, e, D("1")))     # numerically equal costs hash equally

    def test_empty_ledger_head_is_the_genesis_value(self):
        self.assertEqual(AuditLedger(BOOK).head, "0" * 64)


def _canon(x):
    from model_ledger.money import canonical
    return canonical(x)


class TestShared(unittest.TestCase):
    def test_allocation_sums_to_the_amount_for_every_basis(self):
        led = filled(60, 2)
        shared = [make_shared("fee", "1234.57", "cost"), make_shared("seat", "100", "tokens"), make_shared("fixed", "99.99", "fixed", {"a": 1, "c": 3})]
        out = allocate_shared(led, shared)
        for s in shared:
            self.assertEqual(sum(out[s.name].values()), s.amount)
        self.assertEqual(set(out["fixed"]), {"a", "c"})
        self.assertEqual(out["fee"], allocate_shared(led, shared[:1])["fee"])

    def test_cost_basis_is_proportional_to_spend(self):
        led = AuditLedger(BOOK)
        led.record(ev(team="a", i=3000))
        led.record(ev(team="b", i=1000))
        out = allocate_shared(led, [make_shared("fee", "100", "cost")])
        self.assertEqual(out["fee"], {"a": D("75.00"), "b": D("25.00")})

    def test_fully_loaded_adds_allocations_to_direct_spend(self):
        loaded = fully_loaded({"a": D("10.004"), "b": D("5")}, {"fee": {"a": D("1.00"), "z": D("2.00")}})
        self.assertEqual(loaded, {"a": D("11.00"), "b": D("5.00"), "z": D("2.00")})

    def test_errors(self):
        led = AuditLedger(BOOK)
        led.record(ev())
        for args in (("x", "-1", "cost"), ("x", "1", "fixed"), ("x", "1", "colour")):
            with self.subTest(args=args), self.assertRaises(MoneyError):
                allocate_shared(led, [make_shared(*args)])
        with self.assertRaises(MoneyError):
            allocate_shared(led, [make_shared("x", "1", "cost")], start="2030-01-01", end="2030-01-02")


class TestBudget(unittest.TestCase):
    def led(self, per_day=D(1), days=10, team="eng/search"):
        led = AuditLedger(BOOK)
        for d in range(1, days + 1):
            led.record(ev(day=f"2026-03-{d:02d}", team=team, i=int(per_day * 1000)))
        return led

    def test_pace_projection_and_crossing_day_by_hand(self):
        s = status(self.led(), make_budget("eng", "2026-03", 25), "2026-03-10")
        self.assertEqual((s.spent, s.days_elapsed, s.days_in_period, s.projected), (D(10), 10, 31, D(31)))
        self.assertEqual(s.projected_breach, "2026-03-25")        # 15 more days at 1 a day
        self.assertIsNone(s.breached_on)
        self.assertTrue(s.will_breach)
        self.assertEqual(s.alerts, ())                       # 40 percent: no alert threshold passed yet

    def test_breach_day_is_the_first_day_cumulative_spend_reaches_the_budget(self):
        s = status(self.led(), make_budget("eng", "2026-03", 7), "2026-03-10")
        self.assertEqual(s.breached_on, "2026-03-07")
        self.assertIsNone(s.projected_breach)
        self.assertEqual(s.alerts, (D("0.5"), D("0.8"), D(1)))
        exact = status(self.led(), make_budget("eng", "2026-03", 7), "2026-03-06")
        self.assertIsNone(exact.breached_on)
        self.assertEqual(exact.alerts, (D("0.5"), D("0.8")))

    def test_budget_that_holds(self):
        s = status(self.led(), make_budget("eng", "2026-03", 100), "2026-03-10")
        self.assertFalse(s.will_breach)
        self.assertIsNone(s.projected_breach)

    def test_projection_that_lands_on_the_budget_exactly_is_not_a_breach(self):
        s = status(self.led(), make_budget("eng", "2026-03", 31), "2026-03-10")
        self.assertEqual(s.projected, D(31))
        self.assertFalse(s.will_breach)

    def test_hierarchy_and_periods(self):
        led = self.led()
        self.assertEqual(status(led, make_budget("eng", "2026-03", 100), "2026-03-10").spent, D(10))
        self.assertEqual(status(led, make_budget("eng/search", "2026-03", 100), "2026-03-10").spent, D(10))
        self.assertEqual(status(led, make_budget("data", "2026-03", 100), "2026-03-10").spent, D(0))
        self.assertEqual(status(led, make_budget("eng", "2026-02", 100), "2026-03-10").spent, D(0))     # a past month sees only its own days
        before = status(led, make_budget("eng", "2026-04", 100), "2026-03-10")
        self.assertEqual((before.days_elapsed, before.spent, before.projected), (0, D(0), D(0)))
        after = status(led, make_budget("eng", "2026-02", 100), "2026-03-10")
        self.assertEqual(after.days_elapsed, 28)

    def test_leap_year_month_length(self):
        led = AuditLedger(BOOK)
        led.record(ev(day="2028-02-01", i=1000))
        self.assertEqual(status(led, make_budget("a", "2028-02", 100), "2028-02-01").days_in_period, 29)

    def test_validation(self):
        for args in (("a", "2026-3", 1), ("a", "2026-13", 1), ("a", "2026-03", 0), ("a", "2026-03", "x")):
            with self.subTest(args=args), self.assertRaises(MoneyError):
                make_budget(*args)


if __name__ == "__main__":
    unittest.main()
