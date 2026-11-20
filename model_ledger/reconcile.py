"""Reconciling the ledger to a provider invoice, line by line, with every dollar of difference assigned to a cause.

For each model, the invoice says how many tokens it billed and what it charged. The difference between the invoice and the ledger is split
into two parts that add up exactly:

- usage variance: the invoice billed a different number of tokens than the ledger recorded, priced at the ledger's own rates. Events are
  missing from the ledger (positive) or were counted twice (negative).
- rate variance: the invoice charged a different amount than the ledger's rates imply for the tokens it billed. The price book is stale, a
  discount or commitment applies, or the provider rounds differently.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .ledger import AuditLedger
from .money import MoneyError, dec
from .pricing import CLASSES, UnknownModel


@dataclass(frozen=True)
class InvoiceLine:
    model: str
    tokens: dict[str, int]
    amount: Decimal


def parse_invoice(raw: Any) -> list[InvoiceLine]:
    if not isinstance(raw, list) or not raw:
        raise MoneyError("invoice: a list of lines, one per model")
    out, seen = [], set()
    for k, line in enumerate(raw):
        where = f"invoice[{k}]"
        if not isinstance(line, dict) or set(line) - {"model", "input_tokens", "output_tokens", "cached_read_tokens", "cache_write_tokens", "amount"}:
            raise MoneyError(f"{where}: expected model, amount and token counts")
        model = line.get("model")
        if not isinstance(model, str) or not model or model in seen:
            raise MoneyError(f"{where}.model: must be a unique non-empty string")
        seen.add(model)
        toks = {}
        for cls in CLASSES:
            v = line.get(f"{cls}_tokens", 0)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise MoneyError(f"{where}.{cls}_tokens: a non-negative whole number")
            toks[cls] = v
        amount = dec(line.get("amount"), f"{where}.amount")
        if amount < 0:
            raise MoneyError(f"{where}.amount: must not be negative")
        out.append(InvoiceLine(model, toks, amount))
    return out


@dataclass(frozen=True)
class LineResult:
    model: str
    ledger_cost: Fraction
    invoice_amount: Fraction
    usage_variance: Fraction
    rate_variance: Fraction
    token_difference: dict[str, int]          # invoice minus ledger, per class

    @property
    def variance(self) -> Fraction:
        return self.invoice_amount - self.ledger_cost


@dataclass(frozen=True)
class Reconciliation:
    lines: tuple[LineResult, ...]
    ledger_total: Fraction
    invoice_total: Fraction

    @property
    def variance(self) -> Fraction:
        return self.invoice_total - self.ledger_total

    @property
    def usage_variance(self) -> Fraction:
        return sum((x.usage_variance for x in self.lines), Fraction(0))

    @property
    def rate_variance(self) -> Fraction:
        return sum((x.rate_variance for x in self.lines), Fraction(0))

    @property
    def explained(self) -> bool:
        """Usage and rate variance add up to the whole difference."""
        return self.usage_variance + self.rate_variance == self.variance


def reconcile(ledger: AuditLedger, invoice: list[InvoiceLine], start: str, end: str) -> Reconciliation:
    """Compare ledger and invoice for the window ``start`` to ``end``. Models on only one side are included."""
    ledger_lines = ledger.class_costs(start=start, end=end)
    inv = {line.model: line for line in invoice}
    models = sorted({m for m, _ in ledger_lines} | set(inv))
    results = []
    for m in models:
        line = inv.get(m)
        led_cost = Fraction(0)
        expected = Fraction(0)
        diff = {}
        for cls in CLASSES:
            tok, cost = ledger_lines.get((m, cls), (0, Decimal(0)))
            led_cost += Fraction(cost)
            billed = line.tokens[cls] if line else 0
            diff[cls] = billed - tok
            if billed:
                if tok:
                    expected += Fraction(cost) / tok * billed
                else:
                    try:
                        rate = ledger.book.rate_for(m, end).for_prompt(0).of(cls)
                    except (UnknownModel, KeyError) as exc:
                        raise MoneyError(f"{m}: the invoice bills {cls} tokens but the price book has no rate in force on {end}") from exc
                    expected += Fraction(rate) / 1_000_000 * billed * Fraction(ledger.book.multiplier("standard"))
        amount = Fraction(line.amount) if line else Fraction(0)
        results.append(LineResult(m, led_cost, amount, expected - led_cost, amount - expected, diff))
    return Reconciliation(tuple(results), sum((r.ledger_cost for r in results), Fraction(0)), sum((r.invoice_amount for r in results), Fraction(0)))
