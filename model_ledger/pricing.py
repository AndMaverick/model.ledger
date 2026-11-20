"""Price books: what a token costs, by model, by token class, by date, and by how the request was made.

Rates are dollars per million tokens, the unit providers publish. A price change takes effect on a date and never rewrites history: an
event is priced with the rate that applied on its own day."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .money import MoneyError, dec
from .simple import UnknownModel  # one exception for both generations of the API

__all__ = ["CLASSES", "NoPriceOnDate", "PriceBook", "Rate", "UnknownModel", "make_rate", "parse_book", "parse_day"]

CLASSES = ("input", "output", "cached_read", "cache_write")
MILLION = Decimal(1_000_000)


class NoPriceOnDate(KeyError):
    """The model has prices, but none in force on the event's day."""


def parse_day(x: Any, where: str = "day") -> str:
    if not isinstance(x, str):
        raise MoneyError(f"{where}: expected a date like 2026-03-01")
    try:
        d = _dt.date.fromisoformat(x)
    except ValueError as exc:
        raise MoneyError(f"{where}: {x!r} is not a date like 2026-03-01") from exc
    if d.isoformat() != x:
        raise MoneyError(f"{where}: write the date as {d.isoformat()}")
    return x


@dataclass(frozen=True)
class Rate:
    """Dollars per million tokens for each class. Cached reads and cache writes default to the input rate when not stated."""

    input: Decimal
    output: Decimal
    cached_read: Decimal
    cache_write: Decimal
    long_threshold: int | None = None            # prompts longer than this use ``long`` for the whole request
    long: Rate | None = None

    def for_prompt(self, prompt_tokens: int) -> Rate:
        if self.long is not None and self.long_threshold is not None and prompt_tokens > self.long_threshold:
            return self.long
        return self

    def of(self, cls: str) -> Decimal:
        return {"input": self.input, "output": self.output, "cached_read": self.cached_read, "cache_write": self.cache_write}[cls]


def make_rate(spec: dict[str, Any], where: str = "rate") -> Rate:
    allowed = {"from", "input", "output", "cached_read", "cache_write", "long_context"}
    extra = sorted(set(spec) - allowed)
    if extra:
        raise MoneyError(f"{where}: unknown keys {extra}")
    if "input" not in spec or "output" not in spec:
        raise MoneyError(f"{where}: give 'input' and 'output' (dollars per million tokens)")
    inp, out = dec(spec["input"], f"{where}.input"), dec(spec["output"], f"{where}.output")
    cr = dec(spec["cached_read"], f"{where}.cached_read") if "cached_read" in spec else inp
    cw = dec(spec["cache_write"], f"{where}.cache_write") if "cache_write" in spec else inp
    if min(inp, out, cr, cw) < 0:
        raise MoneyError(f"{where}: rates must not be negative")
    long: Rate | None = None
    threshold: int | None = None
    if "long_context" in spec:
        lc = spec["long_context"]
        if not isinstance(lc, dict) or "threshold" not in lc:
            raise MoneyError(f"{where}.long_context: give 'threshold' and the rates that apply above it")
        threshold = lc["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise MoneyError(f"{where}.long_context.threshold: a positive whole number of tokens")
        long = make_rate({k: v for k, v in lc.items() if k != "threshold"}, f"{where}.long_context")
    return Rate(inp, out, cr, cw, threshold, long)


@dataclass
class PriceBook:
    """Effective-dated rates per model, plus the multipliers for non-standard request modes (batch, priority)."""

    rates: dict[str, list[tuple[str, Rate]]] = field(default_factory=dict)
    modes: dict[str, Decimal] = field(default_factory=lambda: {"standard": Decimal(1)})

    def add(self, model: str, rate: Rate, effective_from: str = "0001-01-01") -> None:
        parse_day(effective_from, "effective_from")
        rows = self.rates.setdefault(model, [])
        if any(d == effective_from for d, _ in rows):
            raise MoneyError(f"{model}: two prices take effect on {effective_from}")
        rows.append((effective_from, rate))
        rows.sort(key=lambda r: r[0])

    def rate_for(self, model: str, day: str) -> Rate:
        rows = self.rates.get(model)
        if not rows:
            raise UnknownModel(model)
        current = None
        for d, r in rows:
            if d <= day:
                current = r
        if current is None:
            raise NoPriceOnDate(f"{model} has no price in force on {day}")
        return current

    def multiplier(self, mode: str) -> Decimal:
        if mode not in self.modes:
            raise MoneyError(f"mode {mode!r} has no multiplier in the price book; known: {sorted(self.modes)}")
        return self.modes[mode]

    def cost_parts(self, model: str, day: str, mode: str, input_tokens: int, output_tokens: int, cached_read: int = 0, cache_write: int = 0) -> dict[str, Decimal]:
        """Exact cost of each token class for one request. A long prompt moves every class to the long-context rate."""
        rate = self.rate_for(model, day).for_prompt(input_tokens + cached_read + cache_write)
        m = self.multiplier(mode)
        tokens = {"input": input_tokens, "output": output_tokens, "cached_read": cached_read, "cache_write": cache_write}
        return {c: Decimal(tokens[c]) * rate.of(c) / MILLION * m for c in CLASSES}

    def to_spec(self) -> dict[str, Any]:
        def r(rate: Rate) -> dict[str, Any]:
            out: dict[str, Any] = {"input": str(rate.input), "output": str(rate.output), "cached_read": str(rate.cached_read), "cache_write": str(rate.cache_write)}
            if rate.long is not None:
                out["long_context"] = {"threshold": rate.long_threshold, **r(rate.long)}
            return out
        return {
            "prices": {m: [{"from": d, **r(rate)} for d, rate in rows] for m, rows in sorted(self.rates.items())},
            "modes": {k: str(v) for k, v in sorted(self.modes.items())},
        }


def parse_book(prices: Any, modes: Any = None) -> PriceBook:
    if not isinstance(prices, dict) or not prices:
        raise MoneyError("prices: need at least one model")
    book = PriceBook()
    for model, spec in sorted(prices.items()):
        rows = spec if isinstance(spec, list) else [spec]
        if not rows:
            raise MoneyError(f"prices.{model}: no rates")
        for k, row in enumerate(rows):
            where = f"prices.{model}[{k}]"
            if not isinstance(row, dict):
                raise MoneyError(f"{where}: expected an object")
            if len(rows) > 1 and "from" not in row:
                raise MoneyError(f"{where}: with several prices, each needs a 'from' date")
            book.add(model, make_rate(row, where), parse_day(row["from"], f"{where}.from") if "from" in row else "0001-01-01")
    if modes is not None:
        if not isinstance(modes, dict):
            raise MoneyError("modes: must map a mode name to a multiplier")
        for k, v in modes.items():
            m = dec(v, f"modes.{k}")
            if m <= 0:
                raise MoneyError(f"modes.{k}: must be positive")
            book.modes[k] = m
        book.modes.setdefault("standard", Decimal(1))
    return book
