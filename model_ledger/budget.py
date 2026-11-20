"""Budgets, burn rate, and the day a team will cross its line at the current pace."""
from __future__ import annotations

import calendar
import datetime as _dt
import math
from dataclasses import dataclass
from decimal import Decimal

from .ledger import AuditLedger
from .money import MoneyError, dec
from .pricing import parse_day

THRESHOLDS = (Decimal("0.5"), Decimal("0.8"), Decimal(1))


@dataclass(frozen=True)
class Budget:
    team: str                  # a team, or a parent ("eng" covers "eng/search")
    period: str                # YYYY-MM
    amount: Decimal


def make_budget(team: str, period: str, amount: object) -> Budget:
    if not isinstance(period, str) or len(period) != 7:
        raise MoneyError(f"budget {team}: period must look like 2026-03")
    parse_day(period + "-01", f"budget {team}.period")
    a = dec(amount, f"budget {team}.amount")
    if a <= 0:
        raise MoneyError(f"budget {team}: amount must be positive")
    return Budget(team, period, a)


@dataclass(frozen=True)
class BudgetStatus:
    team: str
    period: str
    budget: Decimal
    spent: Decimal
    share: Decimal                       # spent / budget
    days_elapsed: int
    days_in_period: int
    projected: Decimal                   # month-end spend at the average daily pace so far
    breached_on: str | None              # the day cumulative spend first reached the budget
    projected_breach: str | None         # the day it will, at the current pace, if it has not yet
    alerts: tuple[Decimal, ...]          # thresholds already passed

    @property
    def breached(self) -> bool:
        return self.breached_on is not None

    @property
    def will_breach(self) -> bool:
        return self.projected > self.budget


def status(ledger: AuditLedger, budget: Budget, as_of: str) -> BudgetStatus:
    """Where a budget stands as of a day: spend so far, pace, the day it was (or will be) crossed.

    The pace is the average over the days elapsed in the period so far, which is the simplest forecast a finance reader can check by hand."""
    parse_day(as_of, "as_of")
    year, month = int(budget.period[:4]), int(budget.period[5:7])
    days_in = calendar.monthrange(year, month)[1]
    first = f"{budget.period}-01"
    last = f"{budget.period}-{days_in:02d}"
    if as_of < first:
        elapsed = 0
    elif as_of > last:
        elapsed = days_in
    else:
        elapsed = int(as_of[8:10])
    end = min(as_of, last)
    entries = ledger.select(team=budget.team, start=first, end=end) if elapsed else []
    spent = sum((e.cost for e in entries), Decimal(0))
    daily: dict[str, Decimal] = {}
    for e in entries:
        daily[e.event.day] = daily.get(e.event.day, Decimal(0)) + e.cost
    running, breached_on = Decimal(0), None
    for day in sorted(daily):
        running += daily[day]
        if running >= budget.amount:
            breached_on = day
            break
    projected = spent / elapsed * days_in if elapsed else Decimal(0)
    projected_breach = None
    if breached_on is None and elapsed and spent > 0 and projected > budget.amount:
        pace = spent / elapsed
        need = math.ceil((budget.amount - spent) / pace)
        crossing = _dt.date.fromisoformat(f"{budget.period}-{elapsed:02d}") + _dt.timedelta(days=need)
        projected_breach = crossing.isoformat()
    share = spent / budget.amount
    return BudgetStatus(budget.team, budget.period, budget.amount, spent, share, elapsed, days_in, projected, breached_on,
                        projected_breach, tuple(t for t in THRESHOLDS if share >= t))
