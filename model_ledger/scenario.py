"""Ledger files: strict loading, a fingerprint that pins the result, the full analysis and the gates."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from decimal import Decimal
from fractions import Fraction
from typing import Any

from ._version import __version__
from .anomaly import spikes
from .bridge import bridge, money
from .budget import make_budget, status
from .ledger import AuditLedger, make_event
from .money import MoneyError, dec, fmt, quantize
from .pricing import parse_book, parse_day
from .reconcile import parse_invoice, reconcile
from .shared import allocate_shared, fully_loaded, make_shared

METHOD = "exact-decimal+hash-chain-1"
TOP_KEYS = {"name", "prices", "modes", "events", "budgets", "shared", "as_of", "window", "compare", "invoice", "anomaly", "gates"}
GATE_KEYS = {"chain_head", "no_budget_breach", "no_projected_breach", "max_spike_days", "max_unattributed_share", "max_invoice_variance", "max_growth"}
UNATTRIBUTED = "unattributed"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise MoneyError(f"duplicate key {k!r} in the file")
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise MoneyError(f"{name} is not allowed")


def loads(text: str) -> dict[str, Any]:
    """Parse JSON strictly. A repeated key is an error, because the second silently overriding the first is how a number goes wrong."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise MoneyError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MoneyError("the file must be a JSON object")
    return data


def fingerprint(data: dict[str, Any]) -> str:
    canon = json.dumps({"input": data, "version": __version__, "method": METHOD}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


CSV_COLUMNS = ["day", "team", "model", "input_tokens", "output_tokens", "cached_read_tokens", "cache_write_tokens", "mode", "id"]


def events_from_csv(text: str) -> list[dict[str, Any]]:
    """Read events from CSV. Required columns: day, team, model, input_tokens, output_tokens. Optional: cached_read_tokens,
    cache_write_tokens, mode, id, and any column named ``tag:<key>``. Blank cells take the default. A bad number is an error."""
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    missing = [c for c in ("day", "team", "model", "input_tokens", "output_tokens") if c not in cols]
    if missing:
        raise MoneyError(f"csv: missing columns {missing}")
    unknown = [c for c in cols if c not in CSV_COLUMNS and not c.startswith("tag:")]
    if unknown:
        raise MoneyError(f"csv: unknown columns {unknown}")
    out = []
    for n, row in enumerate(reader, start=2):
        ev: dict[str, Any] = {}
        tags = {}
        for c in cols:
            v = (row.get(c) or "").strip()
            if c.startswith("tag:"):
                if v:
                    tags[c[4:]] = v
            elif v:
                if c.endswith("_tokens"):
                    try:
                        ev[c] = int(v)
                    except ValueError as exc:
                        raise MoneyError(f"csv line {n}: {c} {v!r} is not a whole number") from exc
                else:
                    ev[c] = v
        if tags:
            ev["tags"] = tags
        out.append(ev)
    return out


def _window(raw: Any, name: str, default: tuple[str, str]) -> tuple[str, str]:
    if raw is None:
        return default
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise MoneyError(f"{name}: two dates, [start, end]")
    a, b = parse_day(raw[0], f"{name}[0]"), parse_day(raw[1], f"{name}[1]")
    if a > b:
        raise MoneyError(f"{name}: start is after end")
    return a, b


def build_ledger(data: dict[str, Any], events: list[dict[str, Any]] | None = None) -> AuditLedger:
    book = parse_book(data.get("prices"), data.get("modes"))
    ledger = AuditLedger(book)
    raw = events if events is not None else data.get("events")
    if not isinstance(raw, list) or not raw:
        raise MoneyError("events: need at least one event")
    for k, e in enumerate(raw):
        ledger.record(make_event(e, f"events[{k}]"))
    return ledger


def analyze(data: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the whole analysis and return a JSON-ready result with the gate verdicts. ``events`` overrides the file's events (for CSV input)."""
    extra = sorted(set(data) - TOP_KEYS)
    if extra:
        raise MoneyError(f"unknown keys {extra}")
    ledger = build_ledger(data, events)
    days = sorted({e.event.day for e in ledger.entries})
    start, end = _window(data.get("window"), "window", (days[0], days[-1]))
    as_of = parse_day(data["as_of"], "as_of") if "as_of" in data else days[-1]
    gates = _gates(data.get("gates"))
    total = ledger.total(start=start, end=end)
    result: dict[str, Any] = {
        "name": data.get("name", "ledger"),
        "version": __version__,
        "method": METHOD,
        "fingerprint": fingerprint(data),
        "ledger": {
            "events": len(ledger.entries),
            "head": ledger.head,
            "verified": ledger.verify(),
            "first_day": days[0],
            "last_day": days[-1],
            "price_book": ledger.book.to_spec(),
        },
        "window": [start, end],
        "total": _m(total),
        "tokens": ledger.tokens(start=start, end=end),
        "by_team": {k: _m(v) for k, v in ledger.group("team", start=start, end=end).items()},
        "by_model": {k: _m(v) for k, v in ledger.group("model", start=start, end=end).items()},
        "by_month": {k: _m(v) for k, v in ledger.group("month").items()},
        "team_tree": {k: _m(v) for k, v in ledger.team_tree(start=start, end=end).items()},
        "tags": _tags(ledger, start, end),
        "budgets": [],
        "shared": None,
        "anomalies": [],
        "bridge": None,
        "reconciliation": None,
    }
    for k, b in enumerate(data.get("budgets") or []):
        if not isinstance(b, dict) or set(b) != {"team", "period", "amount"}:
            raise MoneyError(f"budgets[{k}]: expected team, period and amount")
        s = status(ledger, make_budget(b["team"], b["period"], b["amount"]), as_of)
        result["budgets"].append({
            "team": s.team, "period": s.period, "budget": _m(s.budget), "spent": _m(s.spent), "share": float(s.share),
            "days_elapsed": s.days_elapsed, "days_in_period": s.days_in_period, "projected": _m(s.projected),
            "breached_on": s.breached_on, "projected_breach": s.projected_breach, "alerts": [float(a) for a in s.alerts],
        })
    result["shared"] = _shared(data.get("shared"), ledger, start, end)
    an = data.get("anomaly") or {}
    if not isinstance(an, dict) or set(an) - {"z", "window", "min_history", "floor", "driver_dimension"}:
        raise MoneyError("anomaly: z, window, min_history, floor and driver_dimension only")
    for sp in spikes(ledger, **an):
        result["anomalies"].append({
            "day": sp.day, "cost": _m(sp.cost), "baseline": _m(sp.baseline), "threshold": _m(sp.threshold), "excess": _m(sp.excess),
            "drivers": [{"name": n, "excess": _m(x)} for n, x in sp.drivers[:5]],
        })
    cmp = data.get("compare")
    if cmp is not None:
        if not isinstance(cmp, dict) or set(cmp) != {"a", "b"}:
            raise MoneyError("compare: give windows 'a' and 'b', each [start, end]")
        br = bridge(ledger, _window(cmp["a"], "compare.a", ("", "")), _window(cmp["b"], "compare.b", ("", "")))
        result["bridge"] = {
            "a": cmp["a"], "b": cmp["b"], "cost_a": _m(br.cost_a), "cost_b": _m(br.cost_b), "delta": _m(br.delta),
            "growth": float(br.delta / br.cost_a) if br.cost_a else None,
            "effects": {k: _m(v) for k, v in br.effects().items()}, "reconciles": br.reconciles,
            "lines": [{"model": x.model, "class": x.cls, "kind": x.kind, "tokens_a": x.tokens_a, "tokens_b": x.tokens_b,
                       "volume": _m(x.volume), "rate": _m(x.rate)} for x in br.lines],
        }
    if data.get("invoice") is not None:
        rec = reconcile(ledger, parse_invoice(data["invoice"]), start, end)
        result["reconciliation"] = {
            "ledger_total": _m(rec.ledger_total), "invoice_total": _m(rec.invoice_total), "variance": _m(rec.variance),
            "usage_variance": _m(rec.usage_variance), "rate_variance": _m(rec.rate_variance), "explained": rec.explained,
            "lines": [{"model": x.model, "ledger": _m(x.ledger_cost), "invoice": _m(x.invoice_amount), "variance": _m(x.variance),
                       "usage_variance": _m(x.usage_variance), "rate_variance": _m(x.rate_variance), "token_difference": x.token_difference}
                      for x in rec.lines],
        }
    result["gates"] = _evaluate(gates, result, total, ledger, start, end)
    result["passed"] = all(g["passed"] for g in result["gates"])
    return result


def _m(x: Decimal | Fraction, places: int = 6) -> str:
    d = Decimal(x.numerator) / Decimal(x.denominator) if isinstance(x, Fraction) else x
    return str(quantize(d, places))


def _tags(ledger: AuditLedger, start: str, end: str) -> dict[str, dict[str, str]]:
    keys = sorted({k for e in ledger.select(start=start, end=end) for k, _ in e.event.tags})
    return {k: {n: _m(v) for n, v in ledger.group(f"tag:{k}", start=start, end=end).items()} for k in keys}


def _shared(raw: Any, ledger: AuditLedger, start: str, end: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise MoneyError("shared: a list of shared costs")
    items = []
    for k, s in enumerate(raw):
        if not isinstance(s, dict) or set(s) - {"name", "amount", "basis", "weights"} or {"name", "amount", "basis"} - set(s):
            raise MoneyError(f"shared[{k}]: expected name, amount, basis and (for 'fixed') weights")
        items.append(make_shared(s["name"], s["amount"], s["basis"], s.get("weights")))
    alloc = allocate_shared(ledger, items, start=start, end=end)
    loaded = fully_loaded(ledger.group("team", start=start, end=end), alloc)
    return {
        "allocations": {n: {t: str(v) for t, v in a.items()} for n, a in alloc.items()},
        "fully_loaded": {t: str(v) for t, v in loaded.items()},
        "sums_exact": all(sum(a.values(), Decimal(0)) == quantize(next(s.amount for s in items if s.name == n), 2) for n, a in alloc.items()),
    }


def _gates(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MoneyError("gates: must be an object")
    extra = sorted(set(raw) - GATE_KEYS)
    if extra:
        raise MoneyError(f"gates: unknown keys {extra}")
    out: dict[str, Any] = {}
    for k in ("no_budget_breach", "no_projected_breach"):
        if k in raw:
            if not isinstance(raw[k], bool):
                raise MoneyError(f"gates.{k}: true or false")
            out[k] = raw[k]
    if "chain_head" in raw:
        h = raw["chain_head"]
        if not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise MoneyError("gates.chain_head: a 64-character lowercase hex SHA-256")
        out["chain_head"] = h
    if "max_spike_days" in raw:
        v = raw["max_spike_days"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise MoneyError("gates.max_spike_days: a non-negative whole number")
        out["max_spike_days"] = v
    for k in ("max_unattributed_share", "max_invoice_variance", "max_growth"):
        if k in raw:
            out[k] = dec(raw[k], f"gates.{k}")
            if out[k] < 0:
                raise MoneyError(f"gates.{k}: must not be negative")
    return out


def _evaluate(g: dict[str, Any], r: dict[str, Any], total: Decimal, ledger: AuditLedger, start: str, end: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"gate": name, "passed": ok, "detail": detail})

    if "chain_head" in g:
        ok = r["ledger"]["head"] == g["chain_head"]
        add("chain_head", ok, "ledger matches the pinned digest" if ok else f"ledger digest is {r['ledger']['head'][:16]}..., pinned {g['chain_head'][:16]}...")
    if g.get("no_budget_breach"):
        hit = [f"{b['team']} {b['period']} on {b['breached_on']}" for b in r["budgets"] if b["breached_on"]]
        add("no_budget_breach", not hit, "no budget has been crossed" if not hit else "crossed: " + "; ".join(hit))
    if g.get("no_projected_breach"):
        hit = [f"{b['team']} {b['period']} (projected {fmt(Decimal(b['projected']))}, budget {fmt(Decimal(b['budget']))})" for b in r["budgets"] if Decimal(b["projected"]) > Decimal(b["budget"])]
        add("no_projected_breach", not hit, "every budget holds at the current pace" if not hit else "on pace to exceed: " + "; ".join(hit))
    if "max_spike_days" in g:
        n = len(r["anomalies"])
        add("max_spike_days", n <= g["max_spike_days"], f"{n} spike day{'s' if n != 1 else ''}, allowed {g['max_spike_days']}")
    if "max_unattributed_share" in g:
        un = ledger.total(team=UNATTRIBUTED, start=start, end=end)
        share = un / total if total else Decimal(0)
        add("max_unattributed_share", share <= g["max_unattributed_share"], f"{share:.1%} of spend is unattributed, allowed {g['max_unattributed_share']:.1%}")
    if "max_invoice_variance" in g:
        rec = r["reconciliation"]
        if rec is None:
            raise MoneyError("gates.max_invoice_variance needs an invoice")
        worst = max([abs(Decimal(rec["variance"]))] + [abs(Decimal(x["variance"])) for x in rec["lines"]])
        add("max_invoice_variance", worst <= g["max_invoice_variance"], f"largest difference between ledger and invoice is {fmt(worst)}, allowed {fmt(g['max_invoice_variance'])}")
    if "max_growth" in g:
        b = r["bridge"]
        if b is None:
            raise MoneyError("gates.max_growth needs a compare")
        growth = b["growth"]
        add("max_growth", growth is None or Decimal(str(growth)) <= g["max_growth"], "no base period" if growth is None else f"spend grew {growth:.1%}, allowed {g['max_growth']:.1%}")
    return out


def digest(data: dict[str, Any], events: list[dict[str, Any]] | None = None) -> str:
    return build_ledger(data, events).head


__all__ = ["analyze", "build_ledger", "digest", "events_from_csv", "fingerprint", "loads", "money"]
