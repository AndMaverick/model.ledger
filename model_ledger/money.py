"""Exact money. Amounts are Decimals, never floats; rounding happens once, at the edge, by a stated rule."""
from __future__ import annotations

import decimal
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

decimal.getcontext().prec = 60


class MoneyError(ValueError):
    """The input contains something the ledger cannot use."""


def dec(x: Any, where: str = "value") -> Decimal:
    """A number as an exact Decimal. Floats go through their shortest decimal form, so 0.1 is one tenth. Strings are accepted."""
    if isinstance(x, bool):
        raise MoneyError(f"{where}: expected a number, got {x!r}")
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, int):
        d = Decimal(x)
    elif isinstance(x, float):
        d = Decimal(repr(x))
    elif isinstance(x, str):
        try:
            d = Decimal(x.strip())
        except decimal.InvalidOperation as exc:
            raise MoneyError(f"{where}: {x!r} is not a number") from exc
    else:
        raise MoneyError(f"{where}: expected a number, got {x!r}")
    if not d.is_finite():
        raise MoneyError(f"{where}: must be finite")
    return d


def quantize(x: Decimal, places: int = 2) -> Decimal:
    """Round half up to ``places`` decimal places (the convention on most invoices)."""
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def fmt(x: Decimal, places: int = 2) -> str:
    q = quantize(x, places)
    return f"{abs(q) if q == 0 else q:,.{places}f}"


def canonical(x: Decimal) -> str:
    """One string per value: 3.50 and 3.5 both become 3.5. Used when hashing."""
    s = format(x.normalize(), "f")
    return "0" if s in {"-0", ""} else s


def frac(x: Decimal) -> Fraction:
    return Fraction(x)


def allocate(total: Decimal, weights: Mapping[str, Decimal | int | float], places: int = 2) -> dict[str, Decimal]:
    """Split ``total`` in proportion to ``weights`` so the parts add up to the total to the last cent.

    Largest-remainder method: every share is rounded down to the cent, and the cents left over go, one each, to the shares that lost the
    most in rounding (ties to the earlier name). The total is first rounded to ``places``. No share is more than one cent from its exact
    proportion, which is the best any whole-cent split can do."""
    if not weights:
        raise MoneyError("allocate: no one to allocate to")
    w = {k: dec(v, f"weight {k}") for k, v in weights.items()}
    if any(v < 0 for v in w.values()):
        raise MoneyError("allocate: weights must not be negative")
    wsum = sum(w.values(), Decimal(0))
    if wsum <= 0:
        raise MoneyError("allocate: weights must sum above zero")
    unit = Decimal(1).scaleb(-places)
    target = quantize(total, places)
    units = int(target / unit)
    exact = {k: Fraction(v) / Fraction(wsum) * units for k, v in w.items()}
    base = {k: int(e) if e >= 0 else -int(-e) for k, e in exact.items()}        # floor toward zero; totals are non-negative in practice
    if units < 0:
        raise MoneyError("allocate: total must not be negative")
    left = units - sum(base.values())
    order = sorted(exact, key=lambda k: (-(exact[k] - base[k]), k))
    for k in order[:left]:
        base[k] += 1
    return {k: base[k] * unit for k in sorted(base)}
