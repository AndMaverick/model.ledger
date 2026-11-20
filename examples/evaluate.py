"""Measure model.ledger against independent oracles on random inputs and print the counts the README quotes.

Everything except the last line is deterministic (fixed seeds). The oracles in tests/oracles.py share no code with the library.

Run from the repository root:  python examples/evaluate.py"""
from __future__ import annotations

import random
import sys
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import oracles  # noqa: E402

from model_ledger import (  # noqa: E402
    AuditLedger,
    allocate,
    bridge,
    make_event,
    parse_book,
    parse_invoice,
    reconcile,
    spikes,
)
from model_ledger.synthetic import daterange, usage  # noqa: E402


def random_book(rng: random.Random) -> tuple[dict, dict]:
    prices, plain = {}, {}
    for i, m in enumerate(("alpha", "beta", "gamma", "delta")):
        r = {c: round(rng.uniform(0.05, 20), 2) for c in ("input", "output", "cached_read", "cache_write")}
        spec = dict(r)
        plain[m] = dict(r)
        if i % 2 == 0:                                   # alpha and gamma have a long-context tier
            lo = {c: round(v * rng.uniform(1.2, 2.5), 2) for c, v in r.items()}
            thr = rng.choice([50_000, 100_000, 200_000])
            spec["long_context"] = {"threshold": thr, **lo}
            plain[m]["long_threshold"] = thr
            plain[m]["long"] = lo
        prices[m] = spec
    return prices, plain


def cost_check(n: int = 5000) -> tuple[int, int, int]:
    rng = random.Random(11)
    prices, plain = random_book(rng)
    modes = {"batch": 0.5, "priority": 2}
    led = AuditLedger(parse_book(prices, modes))
    exact = long_ctx = 0
    for i in range(n):
        m = rng.choice(list(plain))
        mode = rng.choice(["standard", "batch", "priority"])
        tok = {"input": rng.randint(0, 400_000), "output": rng.randint(0, 40_000), "cached_read": rng.randint(0, 300_000), "cache_write": rng.randint(0, 30_000)}
        ev = {"day": "2026-05-01", "team": "t", "model": m, "mode": mode, "input_tokens": tok["input"], "output_tokens": tok["output"],
              "cached_read_tokens": tok["cached_read"], "cache_write_tokens": tok["cache_write"], "id": f"e{i}"}
        cost = led.record(make_event(ev))
        want = oracles.cost_by_hand(plain[m], tok, {"standard": 1, "batch": 0.5, "priority": 2}[mode])
        assert cost is not None
        exact += Fraction(cost) == want
        if "long" in plain[m] and tok["input"] + tok["cached_read"] + tok["cache_write"] > plain[m]["long_threshold"]:
            long_ctx += 1
    return n, exact, long_ctx


def allocation_check(n: int = 3000) -> tuple[int, int, int, int]:
    rng = random.Random(5)
    adds = optimal = within = 0
    for _ in range(n):
        cents = rng.randint(1, 5_000_000)
        weights = {name: rng.randint(0, 10_000) for name in ("a", "b", "c")}
        if sum(weights.values()) == 0:
            weights["a"] = 1
        got = allocate(Decimal(cents) / 100, weights)
        adds += sum(got.values()) == Decimal(cents) / 100
        wsum = sum(weights.values())
        dev = max(abs(Fraction(int(v * 100)) - Fraction(cents * weights[k], wsum)) for k, v in got.items())
        optimal += dev == oracles.best_split_deviation(cents, weights)
        within += dev < 1
    return n, adds, optimal, within


def bridge_check(trials: int = 300) -> tuple[int, int, int]:
    rng = random.Random(3)
    ok = direct = 0
    for t in range(trials):
        prices, _ = random_book(rng)
        led = AuditLedger(parse_book(prices))
        for day in daterange("2026-01-01", "2026-02-28"):
            for m in prices:
                if rng.random() < 0.7:
                    k = 1.0 if day < "2026-02-01" else rng.uniform(0.2, 3.0)
                    led.record(make_event({"day": day, "team": "t", "model": m, "input_tokens": int(rng.randint(1, 10**6) * k),
                                           "output_tokens": int(rng.randint(0, 10**5) * k)}))
        a, b = ("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28")
        br = bridge(led, a, b)
        ok += br.reconciles
        ta = {k: (n, c) for k, (n, c) in led.class_costs(start=a[0], end=a[1]).items()}
        tb = {k: (n, c) for k, (n, c) in led.class_costs(start=b[0], end=b[1]).items()}
        direct += br.delta == oracles.bridge_identity(ta, tb)
        del t
    return trials, ok, direct


def reconcile_check(trials: int = 200) -> tuple[int, int, int]:
    rng = random.Random(9)
    usage_exact = rate_exact = 0
    for _ in range(trials):
        prices, plain = random_book(rng)
        for p in prices.values():
            p.pop("long_context", None)
        led = AuditLedger(parse_book(prices))
        for day in daterange("2026-03-01", "2026-03-31"):
            for m in prices:
                led.record(make_event({"day": day, "team": "t", "model": m, "input_tokens": rng.randint(1, 10**6), "output_tokens": rng.randint(0, 10**5)}))
        victim = rng.choice(list(prices))
        missing = rng.randint(1, 5_000_000)                      # input tokens the invoice billed that the ledger never saw
        factor = Fraction(rng.randint(990, 1020), 1000)           # the provider's effective rate against our price book
        lines = []
        for m in sorted(prices):
            toks = dict.fromkeys(("input", "output", "cached_read", "cache_write"), 0)
            cost = Fraction(0)
            for e in led.select(model=m, start="2026-03-01", end="2026-03-31"):
                for c, n in e.event.tokens().items():
                    toks[c] += n
                cost += Fraction(e.cost)
            extra = Fraction(0)
            if m == victim:
                toks["input"] += missing
                extra = Fraction(str(plain[m]["input"])) * missing / 1_000_000
            amount = (cost + extra) * (factor if m == victim else 1)
            lines.append({"model": m, "input_tokens": toks["input"], "output_tokens": toks["output"], "amount": str(Decimal(amount.numerator) / Decimal(amount.denominator))})
        rec = reconcile(led, parse_invoice(lines), "2026-03-01", "2026-03-31")
        got = {x.model: x for x in rec.lines}[victim]
        want_usage = Fraction(str(plain[victim]["input"])) * missing / 1_000_000
        usage_exact += abs(got.usage_variance - want_usage) < Fraction(1, 10**20)
        rate_exact += rec.explained and all(abs(x.rate_variance) < Fraction(1, 10**20) for x in rec.lines if x.model != victim)
    return trials, usage_exact, rate_exact


def tamper_check(trials: int = 400) -> tuple[int, int, int, int]:
    rng = random.Random(21)
    prices = {"alpha": {"input": 3, "output": 15}, "beta": {"input": 0.25, "output": 1.25}}
    base = [{"day": d, "team": rng.choice(["a", "b", "c"]), "model": rng.choice(list(prices)), "input_tokens": rng.randint(1, 10**6),
             "output_tokens": rng.randint(1, 10**5), "id": f"{d}:{i}"} for i, d in enumerate(daterange("2026-06-01", "2026-06-30") * 3)]

    def head(evs: list[dict]) -> str:
        led = AuditLedger(parse_book(prices))
        for e in evs:
            led.record(make_event(e), on_duplicate="skip")
        return led.head

    good = head(base)
    caught = {"edit": 0, "drop": 0, "swap": 0}
    for _ in range(trials // 4):
        k = rng.randrange(len(base))
        evs = [dict(e) for e in base]
        evs[k]["input_tokens"] += 1
        caught["edit"] += head(evs) != good
        evs = [dict(e) for e in base]
        del evs[k]
        caught["drop"] += head(evs) != good
        evs = [dict(e) for e in base]
        j = (k + 1) % len(evs)
        evs[k], evs[j] = evs[j], evs[k]
        caught["swap"] += head(evs) != good
    # tampering with a stored entry in place is caught by verify()
    in_place = 0
    for _ in range(trials // 4):
        led = AuditLedger(parse_book(prices))
        for e in base:
            led.record(make_event(e))
        assert led.verify()
        k = rng.randrange(len(led.entries))
        object.__setattr__(led.entries[k].event, "input_tokens", led.entries[k].event.input_tokens + 1)
        in_place += not led.verify()
    n = trials // 4
    return n, caught["edit"], caught["drop"] + caught["swap"], in_place


def spike_check(runs: int = 200) -> tuple[int, int, int, int, int]:
    rng = random.Random(33)
    clean_days = false_alarms = planted = found = 0
    for r in range(runs):
        streams = [{"team": "t", "model": "alpha", "input": 20_000_000, "output": 2_000_000, "growth": 0.003, "weekend": 0.4, "noise": 0.06}]
        evs = usage("2026-01-01", "2026-03-31", streams, seed=r)
        book = parse_book({"alpha": {"input": 3, "output": 15}})
        led = AuditLedger(book)
        for e in evs:
            led.record(make_event(e))
        flagged = spikes(led)
        clean_days += len(evs) - 7
        false_alarms += len(flagged)
        evs2 = [dict(e) for e in usage("2026-01-01", "2026-03-31", streams, seed=r)]
        day_ix = rng.randint(30, len(evs2) - 5)
        weekday = day_ix
        while __import__("datetime").date.fromisoformat(evs2[weekday]["day"]).weekday() >= 5:
            weekday += 1
        evs2[weekday]["input_tokens"] *= 2
        evs2[weekday]["output_tokens"] *= 2
        led2 = AuditLedger(book)
        for e in evs2:
            led2.record(make_event(e))
        planted += 1
        found += evs2[weekday]["day"] in {s.day for s in spikes(led2)}
    return runs, clean_days, false_alarms, planted, found


def throughput(n: int = 100_000) -> float:
    prices = {"alpha": {"input": 3, "output": 15}}
    evs = [make_event({"day": "2026-06-01", "team": "t", "model": "alpha", "input_tokens": 1000 + i % 97, "output_tokens": 100 + i % 13}) for i in range(n)]
    led = AuditLedger(parse_book(prices))
    t = time.perf_counter()
    for e in evs:
        led.record(e)
    return n / (time.perf_counter() - t)


def main() -> None:
    n, exact, long_ctx = cost_check()
    print(f"cost vs hand calculation: {exact} of {n} requests exactly equal ({long_ctx} of them in the long-context tier)")
    n, adds, optimal, within = allocation_check()
    print(f"allocation: {adds} of {n} splits add to the cent, {optimal} of {n} match the brute-force optimum, {within} of {n} within one cent of exact")
    n, ok, direct = bridge_check()
    print(f"price-volume-mix bridge: {ok} of {n} ledgers reconcile exactly, {direct} of {n} total change equals the oracle's")
    n, u, rt = reconcile_check()
    print(f"invoice reconciliation: planted missing usage recovered exactly in {u} of {n} trials, unplanted lines clean in {rt} of {n}")
    n, edit, structural, in_place = tamper_check()
    print(f"tamper evidence: {edit} of {n} one-token edits, {structural} of {2 * n} drops or swaps and {in_place} of {n} in-place edits detected")
    runs, days, fa, planted, found = spike_check()
    print(f"spike detection: {fa} false alarms in {days} clean days over {runs} runs, {found} of {planted} planted 2x weekday spikes found")
    print(f"ingest speed on this machine (varies): {throughput():,.0f} events per second")


if __name__ == "__main__":
    main()
