"""Independent implementations used to check the library: they share no code with it."""
from __future__ import annotations

import hashlib
import itertools
import json
import statistics
from fractions import Fraction

MILLION = 1_000_000


def cost_by_hand(rate, tokens, multiplier=1):
    """Cost of one request from a plain dict of per-million rates and token counts, in Fractions.

    ``rate`` = {"input", "output", "cached_read", "cache_write", optional "long_threshold" and "long": {...}}."""
    prompt = tokens.get("input", 0) + tokens.get("cached_read", 0) + tokens.get("cache_write", 0)
    r = rate["long"] if rate.get("long") and prompt > rate["long_threshold"] else rate
    total = Fraction(0)
    for cls in ("input", "output", "cached_read", "cache_write"):
        total += Fraction(str(r[cls])) * tokens.get(cls, 0) / MILLION
    return total * Fraction(str(multiplier))


def best_split_deviation(total_cents, weights):
    """Smallest possible largest-deviation from the exact proportional split over every whole-cent split (brute force)."""
    names = sorted(weights)
    wsum = sum(Fraction(str(weights[n])) for n in names)
    exact = {n: Fraction(str(weights[n])) / wsum * total_cents for n in names}
    best = None
    floors = {n: int(exact[n]) for n in names}
    for extra in itertools.product((0, 1), repeat=len(names)):
        if sum(floors[n] + e for n, e in zip(names, extra, strict=True)) != total_cents:
            continue
        dev = max(abs(floors[n] + e - exact[n]) for n, e in zip(names, extra, strict=True))
        if best is None or dev < best:
            best = dev
    return best


def chain_head(events_costs):
    """Recompute the hash chain from (event dict, canonical cost string) pairs, straight from the documented recipe."""
    prev = "0" * 64
    for ev, cost in events_costs:
        body = json.dumps(ev, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        prev = hashlib.sha256(f"{prev}\n{body}\n{cost}".encode("ascii")).hexdigest()
    return prev


def median_mad(xs):
    m = statistics.median(xs)
    return m, statistics.median([abs(x - m) for x in xs])


def bridge_identity(a, b):
    """Total change recomputed directly from the (model, class) -> (tokens, cost) tables."""
    return sum(Fraction(c) for _, c in b.values()) - sum(Fraction(c) for _, c in a.values())
