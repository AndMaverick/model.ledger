"""Plain-text and JSON rendering of an analysis."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .money import fmt


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, sort_keys=True)


def _d(s: str, places: int = 2) -> str:
    return fmt(Decimal(s), places)


def _table(title: str, rows: dict[str, str], total: str) -> list[str]:
    out = [title]
    tot = Decimal(total)
    for k, v in sorted(rows.items(), key=lambda kv: (-Decimal(kv[1]), kv[0])):
        share = f"{Decimal(v) / tot:6.1%}" if tot else ""
        out.append(f"  {k:<24} {_d(v):>14} {share}")
    return out


def to_text(r: dict[str, Any]) -> str:
    lg = r["ledger"]
    out = [f"{r['name']}   (model.ledger {r['version']}, fingerprint {r['fingerprint'][:16]})", ""]
    out.append(f"Spend {_d(r['total'])} from {r['window'][0]} to {r['window'][1]}, {lg['events']} events")
    out.append(f"Ledger digest {lg['head'][:32]}... ({'verified' if lg['verified'] else 'FAILED VERIFICATION'})")
    tok = r["tokens"]
    out.append(f"Tokens: {tok['input']:,} input, {tok['output']:,} output, {tok['cached_read']:,} cached reads, {tok['cache_write']:,} cache writes")
    out.append("")
    out += _table("By team", r["by_team"], r["total"])
    out.append("")
    out += _table("By model", r["by_model"], r["total"])
    for key, rows in r["tags"].items():
        out.append("")
        out += _table(f"By {key}", rows, r["total"])
    if r["budgets"]:
        out.append("")
        out.append("Budgets")
        for b in r["budgets"]:
            flag = f"crossed on {b['breached_on']}" if b["breached_on"] else (f"on pace to cross on {b['projected_breach']}" if b["projected_breach"] else "holding")
            out.append(f"  {b['team']:<16} {b['period']}  {_d(b['spent'])} of {_d(b['budget'])} ({b['share']:.0%}), projected {_d(b['projected'])}: {flag}")
    if r["shared"]:
        out.append("")
        out.append("Shared costs allocated (to the cent)")
        for name, alloc in r["shared"]["allocations"].items():
            out.append(f"  {name}: " + ", ".join(f"{t} {_d(v)}" for t, v in alloc.items()))
        out.append("  fully loaded: " + ", ".join(f"{t} {_d(v)}" for t, v in r["shared"]["fully_loaded"].items()))
    if r["anomalies"]:
        out.append("")
        out.append("Spikes")
        for a in r["anomalies"]:
            who = ", ".join(f"{d['name']} +{_d(d['excess'])}" for d in a["drivers"][:3])
            out.append(f"  {a['day']}  {_d(a['cost'])} against a typical {_d(a['baseline'])}; driven by {who}")
    b = r["bridge"]
    if b:
        out.append("")
        g = "" if b["growth"] is None else f" ({b['growth']:+.1%})"
        out.append(f"Why spend changed: {_d(b['cost_a'])} to {_d(b['cost_b'])}, {_d(b['delta'])}{g}")
        for k, v in b["effects"].items():
            if Decimal(v) != 0:
                out.append(f"  {k:<14} {_d(v):>14}")
        out.append(f"  adds up exactly: {'yes' if b['reconciles'] else 'NO'}")
    rec = r["reconciliation"]
    if rec:
        out.append("")
        out.append(f"Invoice reconciliation: ledger {_d(rec['ledger_total'])}, invoice {_d(rec['invoice_total'])}, difference {_d(rec['variance'])}")
        out.append(f"  of which usage (tokens the ledger and invoice disagree on) {_d(rec['usage_variance'])}, rate (price per token) {_d(rec['rate_variance'])}")
        for x in rec["lines"]:
            out.append(f"  {x['model']:<20} ledger {_d(x['ledger']):>12} invoice {_d(x['invoice']):>12} usage {_d(x['usage_variance']):>10} rate {_d(x['rate_variance']):>10}")
    if r["gates"]:
        out.append("")
        out.append("Gates")
        for g_ in r["gates"]:
            out.append(f"  {'PASS' if g_['passed'] else 'FAIL'}  {g_['gate']}: {g_['detail']}")
        out.append("")
        out.append("RESULT: " + ("PASS" if r["passed"] else "FAIL"))
    return "\n".join(out) + "\n"
