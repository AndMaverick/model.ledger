import random
import statistics
import unittest
from decimal import Decimal as D
from fractions import Fraction as F

from model_ledger import AuditLedger, MoneyError, bridge, bridge_items, make_event, parse_book, parse_invoice, reconcile, spikes
from model_ledger.anomaly import MAD_SCALE, mad, median
from model_ledger.synthetic import usage
from tests import oracles

BOOK = parse_book({"m": {"input": 1000, "output": 4000}, "n": {"input": 100, "output": 100}}, {"batch": 0.5})


def ev(day, team="a", model="m", i=1000, o=0, **kw):
    return make_event({"day": day, "team": team, "model": model, "input_tokens": i, "output_tokens": o, **kw})


def series(values, team="a", start=1):
    led = AuditLedger(BOOK)
    for k, v in enumerate(values):
        led.record(ev(f"2026-03-{start + k:02d}", team=team, i=int(v * 1000)))
    return led


class TestMedian(unittest.TestCase):
    def test_matches_statistics_module(self):
        rng = random.Random(0)
        for _ in range(200):
            xs = [D(rng.randint(0, 100)) for _ in range(rng.randint(1, 15))]
            self.assertEqual(median(xs), D(str(statistics.median(xs))))
            m, d = oracles.median_mad(xs)
            self.assertEqual(mad(xs), D(str(d)))
        with self.assertRaises(ValueError):
            median([])


class TestSpikes(unittest.TestCase):
    def test_flags_a_planted_spike_and_names_the_driver(self):
        led = series([10, 11, 9, 10, 10, 11, 10, 9, 10, 10])
        led.record(ev("2026-03-11", team="b", i=500_000))
        found = spikes(led)
        self.assertEqual([s.day for s in found], ["2026-03-11"])
        self.assertEqual(found[0].drivers[0][0], "b")
        self.assertGreater(found[0].drivers[0][1], 400)

    def test_threshold_is_the_documented_formula(self):
        vals = [10, 12, 9, 11, 10, 13, 10, 40]
        led = series(vals)
        hist = [D(v) for v in vals[:7]]
        m, d = oracles.median_mad(hist)
        base, spread = D(str(m)), max(MAD_SCALE * D(str(d)), D("0.1") * D(str(m)))
        s = spikes(led, min_history=7)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].threshold, base + 4 * spread)
        self.assertEqual(s[0].baseline, base)

    def test_a_spike_does_not_hide_the_next_one(self):
        # a mean-and-deviation rule is dragged up by the first spike; the median is not
        vals = [10] * 10 + [100, 10, 10, 100]
        days = [s.day for s in spikes(series(vals))]
        self.assertEqual(days, ["2026-03-11", "2026-03-14"])

    def test_flat_history_and_quiet_noise_do_not_flag(self):
        self.assertEqual(spikes(series([10] * 20)), [])
        self.assertEqual(spikes(series([10, 11, 10, 9, 10, 11, 10, 10, 11, 10, 12, 10])), [])

    def test_missing_days_count_as_zero(self):
        led = AuditLedger(BOOK)
        for d in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            led.record(ev(f"2026-03-{d:02d}", i=10_000))
        led.record(ev("2026-03-20", i=10_000))               # days 11 to 19 are silent, then normal volume resumes
        self.assertEqual(spikes(led), [])

    def test_drivers_are_sorted_and_name_only_groups_above_their_own_norm(self):
        led = series([10] * 12)
        led.record(ev("2026-03-13", team="b", i=100_000))
        led.record(ev("2026-03-13", team="c", i=50_000))
        s = spikes(led)[0]
        self.assertEqual([d[0] for d in s.drivers][:2], ["b", "c"])
        self.assertEqual({d[0] for d in s.drivers}, {"b", "c"})                 # team a fell, so it is not a driver
        self.assertEqual([d[1] for d in s.drivers], [D(100), D(50)])
        self.assertEqual(list(s.drivers), sorted(s.drivers, key=lambda r: (-r[1], r[0])))

    def test_false_alarm_rate_on_stationary_noise_is_low_and_a_real_surge_is_caught(self):
        false_days = days = 0
        caught = 0
        for seed in range(20):
            ev_list = usage("2026-01-01", "2026-03-31", [{"team": "a", "model": "m", "input": 1_000_000, "output": 100_000, "noise": 0.08, "weekend": 1.0}], seed)
            led = AuditLedger(BOOK)
            for e in ev_list:
                led.record(make_event(e))
            false_days += len(spikes(led))
            days += len(ev_list)
            surge = usage("2026-01-01", "2026-03-31", [{"team": "a", "model": "m", "input": 1_000_000, "output": 100_000, "noise": 0.08, "weekend": 1.0,
                                                          "surge": {"from": "2026-03-20", "factor": 2.0}}], seed)
            led2 = AuditLedger(BOOK)
            for e in surge:
                led2.record(make_event(e))
            caught += any(s.day == "2026-03-20" for s in spikes(led2))
        self.assertLess(false_days / days, 0.01)
        self.assertGreaterEqual(caught, 19)

    def test_filters_and_validation(self):
        led = series([10] * 12)
        led.record(ev("2026-03-13", team="b", i=500_000))
        self.assertEqual(spikes(led, team="a"), [])
        self.assertEqual(spikes(AuditLedger(BOOK)), [])


def tables(rng, models=("m", "n"), classes=("input", "output")):
    out = {}
    for m in models:
        for c in classes:
            if rng.random() < 0.8:
                q = rng.randint(1, 10**6)
                out[(m, c)] = (q, F(rng.randint(1, 10**5), rng.choice([7, 100, 1000, 3])))
    return out


class TestBridge(unittest.TestCase):
    def test_effects_always_add_up_to_the_change(self):
        rng = random.Random(3)
        for _ in range(500):
            a, b = tables(rng), tables(rng)
            br = bridge_items(a, b)
            self.assertTrue(br.reconciles)
            self.assertEqual(br.delta, oracles.bridge_identity(a, b))
            self.assertEqual(sum(br.effects().values(), F(0)), br.delta)

    def test_no_change_means_no_effects(self):
        t = tables(random.Random(1))
        br = bridge_items(t, dict(t))
        self.assertEqual(br.delta, 0)
        self.assertTrue(all(v == 0 for v in br.effects().values()))

    def test_pure_volume_pure_rate_and_new_and_gone(self):
        a = {("m", "input"): (100, F(10)), ("old", "input"): (50, F(5))}
        b = {("m", "input"): (200, F(20)), ("fresh", "input"): (10, F(3))}
        br = bridge_items(a, b)
        self.assertEqual(br.volume, 10)          # doubled volume at the same rate on a base of 10
        self.assertEqual((br.mix, br.rate), (0, 0))
        self.assertEqual((br.new, br.discontinued), (3, -5))
        self.assertEqual(br.delta, 23 - 15)
        rate_only = bridge_items({("m", "input"): (100, F(10))}, {("m", "input"): (100, F(15))})
        self.assertEqual((rate_only.rate, rate_only.volume, rate_only.mix), (5, 0, 0))

    def test_mix_shift_towards_a_dearer_model(self):
        a = {("cheap", "input"): (100, F(10)), ("dear", "input"): (100, F(100))}
        b = {("cheap", "input"): (50, F(5)), ("dear", "input"): (150, F(150))}      # same 200 tokens, shifted to the dear model
        br = bridge_items(a, b)
        self.assertEqual(br.volume, 0)
        self.assertEqual(br.mix, 45)
        self.assertEqual(br.rate, 0)

    def test_price_change_shows_up_as_rate_not_volume(self):
        book = parse_book({"m": [{"from": "2026-01-01", "input": 2, "output": 8}, {"from": "2026-02-01", "input": 1, "output": 4}]})
        led = AuditLedger(book)
        for d in ("2026-01-10", "2026-02-10"):
            led.record(ev(d, i=1_000_000, o=500_000))
        br = bridge(led, ("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"))
        self.assertEqual((br.volume, br.mix), (0, 0))
        self.assertEqual(br.rate, F(-3))
        self.assertTrue(br.reconciles)

    def test_bridge_on_a_real_ledger_reconciles(self):
        led = AuditLedger(BOOK)
        rng = random.Random(4)
        for d in range(1, 29):
            for m in ("m", "n"):
                led.record(ev(f"2026-0{1 if d < 15 else 2}-{d:02d}", model=m, i=rng.randint(0, 9999), o=rng.randint(0, 9999), cached_read_tokens=rng.randint(0, 99)))
        br = bridge(led, ("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"))
        self.assertTrue(br.reconciles)
        self.assertEqual(br.delta, F(led.total(start="2026-02-01", end="2026-02-28")) - F(led.total(start="2026-01-01", end="2026-01-31")))


class TestReconcile(unittest.TestCase):
    def ledger(self, seed=0, n=60):
        rng = random.Random(seed)
        led = AuditLedger(BOOK)
        for k in range(n):
            led.record(ev(f"2026-03-{rng.randint(1, 28):02d}", model=rng.choice(["m", "n"]), i=rng.randint(1000, 90_000), o=rng.randint(100, 9000),
                          cached_read_tokens=rng.choice([0, 500]), id=f"e{k}"))
        return led

    def invoice(self, led, scale=None, amount_scale=None, drop=None):
        lines = []
        for m in ("m", "n"):
            sel = led.select(model=m)
            toks = led.tokens(model=m)
            amount = sum((e.cost for e in sel), D(0))
            if scale and m in scale:
                toks = {k: int(v * scale[m]) for k, v in toks.items()}
            if amount_scale and m in amount_scale:
                amount *= amount_scale[m]
            lines.append({"model": m, "input_tokens": toks["input"], "output_tokens": toks["output"], "cached_read_tokens": toks["cached_read"],
                          "cache_write_tokens": toks["cache_write"], "amount": str(amount)})
        return parse_invoice(lines)

    def test_a_faithful_invoice_reconciles_to_zero(self):
        led = self.ledger()
        r = reconcile(led, self.invoice(led), "2026-03-01", "2026-03-31")
        self.assertEqual(r.variance, 0)
        self.assertEqual((r.usage_variance, r.rate_variance), (0, 0))
        self.assertTrue(r.explained)

    def test_usage_and_rate_always_explain_the_whole_difference(self):
        rng = random.Random(1)
        for seed in range(60):
            led = self.ledger(seed)
            inv = self.invoice(led, scale={"m": rng.uniform(0.8, 1.3), "n": rng.uniform(0.9, 1.1)}, amount_scale={"m": D(str(rng.uniform(0.9, 1.1))), "n": D(str(rng.uniform(0.95, 1.05)))})
            r = reconcile(led, inv, "2026-03-01", "2026-03-31")
            self.assertTrue(r.explained, msg=f"seed {seed}")
            for x in r.lines:
                self.assertEqual(x.usage_variance + x.rate_variance, x.variance)

    def test_planted_missing_usage_is_priced_at_the_ledger_rates(self):
        led = self.ledger(2)
        missing = led.select(model="m")[0]
        inv = [{"model": "m", "input_tokens": led.tokens(model="m")["input"] + missing.event.input_tokens,
                "output_tokens": led.tokens(model="m")["output"] + missing.event.output_tokens,
                "cached_read_tokens": led.tokens(model="m")["cached_read"] + missing.event.cached_read_tokens,
                "amount": str(led.total(model="m") + missing.cost)},
               {"model": "n", "input_tokens": led.tokens(model="n")["input"], "output_tokens": led.tokens(model="n")["output"],
                "cached_read_tokens": led.tokens(model="n")["cached_read"], "amount": str(led.total(model="n"))}]
        r = reconcile(led, parse_invoice(inv), "2026-03-01", "2026-03-31")
        m = next(x for x in r.lines if x.model == "m")
        # the invoice bills extra tokens at the model's average realized rate per class; usage variance is that, rate variance is the remainder
        self.assertGreater(m.usage_variance, 0)
        self.assertEqual(m.usage_variance + m.rate_variance, m.variance)
        self.assertEqual(m.variance, F(missing.cost))
        self.assertGreater(m.token_difference["input"], 0)

    def test_double_counted_events_show_as_negative_usage(self):
        led = self.ledger(3)
        dup = led.select(model="n")[0]
        led.record(ev(dup.event.day, model="n", i=dup.event.input_tokens, o=dup.event.output_tokens, cached_read_tokens=dup.event.cached_read_tokens))
        inv = self.invoice(AuditLedger(BOOK))                              # an empty ledger copy has no tokens: build the invoice by hand instead
        lines = []
        for m in ("m", "n"):
            sel = [e for e in led.select(model=m) if e is not led.entries[-1]]
            toks = {c: sum(e.event.tokens()[c] for e in sel) for c in ("input", "output", "cached_read", "cache_write")}
            lines.append({"model": m, "input_tokens": toks["input"], "output_tokens": toks["output"], "cached_read_tokens": toks["cached_read"],
                          "amount": str(sum((e.cost for e in sel), D(0)))})
        r = reconcile(led, parse_invoice(lines), "2026-03-01", "2026-03-31")
        n = next(x for x in r.lines if x.model == "n")
        self.assertLess(n.usage_variance, 0)
        self.assertEqual(next(x for x in r.lines if x.model == "m").variance, 0)
        del inv

    def test_a_stale_price_book_is_a_pure_rate_variance(self):
        led = self.ledger(4)
        r = reconcile(led, self.invoice(led, amount_scale={"m": D("1.05")}), "2026-03-01", "2026-03-31")
        m = next(x for x in r.lines if x.model == "m")
        self.assertEqual(m.usage_variance, 0)
        self.assertEqual(m.rate_variance, F(led.total(model="m")) * F(5, 100))

    def test_models_on_one_side_only(self):
        led = self.ledger(5)
        inv = parse_invoice([{"model": "ghost", "input_tokens": 1000, "amount": "7"}])
        with self.assertRaises(MoneyError):
            reconcile(led, inv, "2026-03-01", "2026-03-31")          # the book has no rate for it, so it cannot be explained
        book = parse_book({"m": {"input": 1000, "output": 4000}, "n": {"input": 100, "output": 100}, "ghost": {"input": 2, "output": 2}})
        led2 = AuditLedger(book)
        led2.record(ev("2026-03-01", model="m"))
        r = reconcile(led2, inv, "2026-03-01", "2026-03-31")
        ghost = next(x for x in r.lines if x.model == "ghost")
        self.assertEqual(ghost.ledger_cost, 0)
        self.assertEqual(ghost.variance, 7)
        self.assertTrue(r.explained)

    def test_invoice_validation(self):
        for raw in ([], "x", [{"model": "m"}], [{"model": "m", "amount": "1", "input_tokens": -1}], [{"model": "m", "amount": "1"}, {"model": "m", "amount": "2"}],
                    [{"model": "m", "amount": "-1"}], [{"model": "m", "amount": "1", "colour": 2}]):
            with self.subTest(raw=str(raw)[:50]), self.assertRaises(MoneyError):
                parse_invoice(raw)


if __name__ == "__main__":
    unittest.main()
