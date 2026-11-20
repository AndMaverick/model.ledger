"""Regenerate the two example ledgers. The usage is synthetic and the rates are illustrative, not any provider's price list.

Run from the repository root:  python examples/generate_examples.py"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model_ledger import AuditLedger, make_event, parse_book, quantize  # noqa: E402
from model_ledger.synthetic import usage  # noqa: E402

PRICES = {
    "frontier-large": [
        {"from": "2026-01-01", "input": 3.0, "output": 15.0, "cached_read": 0.3, "cache_write": 3.75,
         "long_context": {"threshold": 200000, "input": 6.0, "output": 22.5, "cached_read": 0.6, "cache_write": 7.5}},
    ],
    "frontier-small": [
        {"from": "2026-01-01", "input": 0.25, "output": 1.25, "cached_read": 0.03},
        {"from": "2026-10-01", "input": 0.2, "output": 1.0, "cached_read": 0.02},
    ],
    "embedding": {"input": 0.1, "output": 0.0},
}
MODES = {"batch": 0.5}


def streams(launch: bool) -> list[dict]:
    s = [
        {"team": "eng/search", "model": "frontier-small", "input": 90_000_000, "output": 9_000_000, "cached": 30_000_000, "growth": 0.003, "tags": {"feature": "query-rewrite", "env": "prod"}},
        {"team": "eng/search", "model": "embedding", "input": 400_000_000, "output": 0, "growth": 0.002, "tags": {"feature": "index", "env": "prod"}},
        {"team": "eng/support-bot", "model": "frontier-large", "input": 12_000_000, "output": 2_400_000, "cached": 6_000_000, "growth": 0.004, "tags": {"feature": "answers", "env": "prod"}},
        {"team": "product/assist", "model": "frontier-large", "input": 7_000_000, "output": 1_800_000, "cached": 2_000_000, "growth": 0.006, "tags": {"feature": "drafts", "env": "prod"}},
        {"team": "data/etl", "model": "frontier-small", "input": 60_000_000, "output": 6_000_000, "mode": "batch", "weekend": 1.0, "tags": {"feature": "classify", "env": "prod"}},
        {"team": "data/etl", "model": "frontier-small", "input": 4_000_000, "output": 400_000, "tags": {"feature": "classify", "env": "staging"}},
        {"team": "unattributed", "model": "frontier-small" if not launch else "frontier-large", "input": 1_500_000 if not launch else 12_000_000,
         "output": 200_000 if not launch else 1_600_000, "tags": {"env": "prod"}},
    ]
    if launch:
        s.append({"team": "product/assist", "model": "frontier-large", "input": 40_000_000, "output": 5_000_000, "cached": 5_000_000,
                  "surge": {"from": "2026-10-18", "factor": 3.4}, "weekend": 0.7, "noise": 0.1, "tags": {"feature": "agent-mode", "env": "prod"}})
    return s


def invoice_for(ledger: AuditLedger, start: str, end: str, *, discount: dict[str, Decimal] | None = None) -> list[dict]:
    """A provider invoice for the window, built from usage the ledger saw (optionally with a rate error on a model)."""
    lines = []
    for model in sorted({e.event.model for e in ledger.entries}):
        toks = {"input": 0, "output": 0, "cached_read": 0, "cache_write": 0}
        cost = Fraction(0)
        for e in ledger.select(model=model, start=start, end=end):
            for c, n in e.event.tokens().items():
                toks[c] += n
            cost += Fraction(e.cost)
        amount = Decimal(cost.numerator) / Decimal(cost.denominator)
        if discount and model in discount:
            amount *= discount[model]
        lines.append({"model": model, "input_tokens": toks["input"], "output_tokens": toks["output"], "cached_read_tokens": toks["cached_read"],
                      "cache_write_tokens": toks["cache_write"], "amount": str(quantize(amount, 2))})
    return lines


def build(launch: bool) -> dict:
    events = usage("2026-09-01", "2026-10-31", streams(launch), seed=7 if launch else 3)
    book = parse_book(PRICES, MODES)
    led = AuditLedger(book)
    for e in events:
        led.record(make_event(e))
    invoice = invoice_for(led, "2026-10-01", "2026-10-31", discount={"frontier-large": Decimal("1.012")} if launch else None)
    if launch:
        # the invoice also bills three days of embedding usage that never reached the ledger
        for line in invoice:
            if line["model"] == "embedding":
                line["input_tokens"] += 3 * 420_000_000
                line["amount"] = str(Decimal(line["amount"]) + Decimal("126.00"))
    data: dict = {
        "name": "Runaway launch, October 2026 (synthetic example)" if launch else "Platform teams, October 2026 (synthetic example)",
        "prices": PRICES, "modes": MODES, "events": events,
        "window": ["2026-10-01", "2026-10-31"], "as_of": "2026-10-31",
        "compare": {"a": ["2026-09-01", "2026-09-30"], "b": ["2026-10-01", "2026-10-31"]},
        "invoice": invoice,
        "shared": [{"name": "platform commitment", "amount": "1500.00", "basis": "cost"}],
    }
    if launch:
        data["budgets"] = [{"team": "product", "period": "2026-10", "amount": 18000}, {"team": "eng", "period": "2026-10", "amount": 26000}]
        data["gates"] = {"no_budget_breach": True, "no_projected_breach": True, "max_spike_days": 0, "max_unattributed_share": 0.05,
                         "max_invoice_variance": 1.0, "max_growth": 0.25}
    else:
        data["budgets"] = [{"team": "product", "period": "2026-10", "amount": 7000}, {"team": "eng", "period": "2026-10", "amount": 30000},
                           {"team": "data", "period": "2026-10", "amount": 3000}]
        data["gates"] = {"no_budget_breach": True, "no_projected_breach": True, "max_spike_days": 0, "max_unattributed_share": 0.05,
                         "max_invoice_variance": 1.0, "max_growth": 0.25, "chain_head": led.head}
    return data


def main() -> None:
    for name, launch in (("platform_team", False), ("runaway_launch", True)):
        (ROOT / "examples" / f"{name}.json").write_text(json.dumps(build(launch), indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
