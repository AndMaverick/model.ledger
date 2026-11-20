"""Why did spend change? A price-volume-mix bridge whose pieces add up exactly to the change.

Spend is tokens times rate, summed over models and token classes. Between two periods the change splits into:

- volume: the same mix of tokens, more or fewer of them, at last period's rates;
- mix: tokens shifting between models and classes (toward dearer or cheaper ones) at last period's rates;
- rate: the same tokens costing more or less per token (a price change, a discount, a switch to long-context pricing);
- new and discontinued: model and class lines that did not exist in one of the periods.

All arithmetic is in exact fractions, so volume + mix + rate + new + discontinued equals the change with nothing left over."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from .ledger import AuditLedger


@dataclass(frozen=True)
class Line:
    model: str
    cls: str
    tokens_a: int
    tokens_b: int
    cost_a: Fraction
    cost_b: Fraction
    volume: Fraction          # (tokens_b - tokens_a) at the old rate; mix is split out of the sum of these
    rate: Fraction
    kind: str                 # "continuing", "new" or "discontinued"


@dataclass(frozen=True)
class Bridge:
    cost_a: Fraction
    cost_b: Fraction
    volume: Fraction
    mix: Fraction
    rate: Fraction
    new: Fraction
    discontinued: Fraction
    lines: tuple[Line, ...]

    @property
    def delta(self) -> Fraction:
        return self.cost_b - self.cost_a

    @property
    def reconciles(self) -> bool:
        return self.volume + self.mix + self.rate + self.new + self.discontinued == self.delta

    def effects(self) -> dict[str, Fraction]:
        return {"volume": self.volume, "mix": self.mix, "rate": self.rate, "new": self.new, "discontinued": self.discontinued}


def bridge_items(a: dict[tuple[str, str], tuple[int, Fraction]], b: dict[tuple[str, str], tuple[int, Fraction]]) -> Bridge:
    """The bridge between two sets of (model, class) -> (tokens, cost) lines."""
    lines = []
    cost_a = sum((c for _, c in a.values()), Fraction(0))
    cost_b = sum((c for _, c in b.values()), Fraction(0))
    new = disc = rate_total = vol_sum = Fraction(0)
    q0c = q1c = Fraction(0)
    c0c = Fraction(0)
    for key in sorted(set(a) | set(b)):
        qa, ca = a.get(key, (0, Fraction(0)))
        qb, cb = b.get(key, (0, Fraction(0)))
        if qa == 0 and qb == 0 and ca == cb:
            continue                           # a zero-token line with the same cost in both periods says nothing
        if qa > 0 and qb > 0:
            ra, rb = ca / qa, cb / qb
            volume = (qb - qa) * ra
            rate = (rb - ra) * qb
            lines.append(Line(key[0], key[1], qa, qb, ca, cb, volume, rate, "continuing"))
            vol_sum += volume
            rate_total += rate
            q0c += qa
            q1c += qb
            c0c += ca
        elif qb > 0 or cb != 0:
            new += cb - ca
            lines.append(Line(key[0], key[1], qa, qb, ca, cb, Fraction(0), Fraction(0), "new"))
        else:
            disc += cb - ca
            lines.append(Line(key[0], key[1], qa, qb, ca, cb, Fraction(0), Fraction(0), "discontinued"))
    pure = c0c * (q1c - q0c) / q0c if q0c else Fraction(0)
    return Bridge(cost_a, cost_b, pure, vol_sum - pure, rate_total, new, disc, tuple(lines))


def bridge(ledger: AuditLedger, a: tuple[str, str], b: tuple[str, str]) -> Bridge:
    """Bridge between two date ranges ((start, end) inclusive) of the same ledger."""
    def items(window: tuple[str, str]) -> dict[tuple[str, str], tuple[int, Fraction]]:
        return {k: (n, Fraction(c)) for k, (n, c) in ledger.class_costs(start=window[0], end=window[1]).items() if n or c}
    return bridge_items(items(a), items(b))


def money(x: Fraction, places: int = 2) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return (Decimal(x.numerator) / Decimal(x.denominator)).quantize(q)
