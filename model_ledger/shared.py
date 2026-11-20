"""Shared costs (platform fees, committed spend, an enterprise seat) allocated to teams to the last cent."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .ledger import AuditLedger
from .money import MoneyError, allocate, dec, quantize


@dataclass(frozen=True)
class Shared:
    name: str
    amount: Decimal
    basis: str                                   # "cost", "tokens" or "fixed"
    weights: tuple[tuple[str, Decimal], ...] = ()


def allocate_shared(ledger: AuditLedger, shared: list[Shared], *, start: str | None = None, end: str | None = None) -> dict[str, dict[str, Decimal]]:
    """Allocate each shared cost across the teams that used the ledger in the window. Returns {shared name: {team: cents}}.

    "cost" weights teams by what they spent, "tokens" by tokens consumed, "fixed" by the weights given. Each allocation sums exactly to
    the shared amount (rounded to the cent)."""
    out: dict[str, dict[str, Decimal]] = {}
    for s in shared:
        if s.basis == "fixed":
            weights = dict(s.weights)
        elif s.basis == "cost":
            weights = {t: v for t, v in ledger.group("team", start=start, end=end).items() if v > 0}
        elif s.basis == "tokens":
            w: dict[str, Decimal] = {}
            for e in ledger.select(start=start, end=end):
                w[e.event.team] = w.get(e.event.team, Decimal(0)) + Decimal(sum(e.event.tokens().values()))
            weights = {t: v for t, v in w.items() if v > 0}
        else:
            raise MoneyError(f"shared {s.name}: basis must be 'cost', 'tokens' or 'fixed'")
        if not weights:
            raise MoneyError(f"shared {s.name}: nobody to allocate to in this window")
        out[s.name] = allocate(s.amount, weights)
    return out


def fully_loaded(direct: dict[str, Decimal], allocations: dict[str, dict[str, Decimal]]) -> dict[str, Decimal]:
    """Direct spend plus allocated shared costs per team, rounded to the cent. Teams that only receive an allocation are included."""
    teams = set(direct) | {t for a in allocations.values() for t in a}
    return {t: quantize(direct.get(t, Decimal(0)), 2) + sum((a.get(t, Decimal(0)) for a in allocations.values()), Decimal(0)) for t in sorted(teams)}


def make_shared(name: str, amount: object, basis: str, weights: dict[str, object] | None = None) -> Shared:
    amt = dec(amount, f"shared {name}")
    if amt < 0:
        raise MoneyError(f"shared {name}: amount must not be negative")
    ws = tuple(sorted((k, dec(v, f"shared {name}.weights.{k}")) for k, v in (weights or {}).items()))
    if basis == "fixed" and not ws:
        raise MoneyError(f"shared {name}: basis 'fixed' needs weights")
    return Shared(name, amt, basis, ws)
