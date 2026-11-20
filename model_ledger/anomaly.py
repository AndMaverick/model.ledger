"""Spike detection that a few bad days cannot fool, and attribution of what drove a spike."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .ledger import AuditLedger

MAD_SCALE = Decimal("1.4826")        # makes the median absolute deviation comparable to a standard deviation for bell-shaped data


def median(xs: list[Decimal]) -> Decimal:
    if not xs:
        raise ValueError("median of nothing")
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mad(xs: list[Decimal]) -> Decimal:
    m = median(xs)
    return median([abs(x - m) for x in xs])


@dataclass(frozen=True)
class Spike:
    day: str
    cost: Decimal
    baseline: Decimal                 # median of the prior days in the window
    threshold: Decimal
    excess: Decimal                   # cost minus baseline
    drivers: tuple[tuple[str, Decimal], ...]    # groups above their own typical level that day, largest first (not an exact partition: medians do not add)


def spikes(
    ledger: AuditLedger,
    *,
    z: Decimal | int | float = 4,
    window: int = 28,
    min_history: int = 7,
    floor: Decimal | float = Decimal("0.1"),
    team: str | None = None,
    driver_dimension: str = "team",
) -> list[Spike]:
    """Days whose cost exceeds the median of the previous ``window`` days by ``z`` robust deviations.

    The median and the median absolute deviation ignore the odd bad day, so a spike does not raise the bar for the next one the way a mean
    and standard deviation do. The deviation is floored at ``floor`` of the median so a very quiet history does not flag noise. Days with
    nothing recorded count as zero, which is what finance would see."""
    z, floor = Decimal(str(z)), Decimal(str(floor))
    daily = ledger.group("day", team=team)
    if not daily:
        return []
    import datetime as _dt
    first, last = _dt.date.fromisoformat(min(daily)), _dt.date.fromisoformat(max(daily))
    days = [(first + _dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    series = [daily.get(d, Decimal(0)) for d in days]
    out = []
    for i in range(min_history, len(days)):
        hist = series[max(0, i - window):i]
        base = median(hist)
        spread = max(MAD_SCALE * mad(hist), floor * base)
        threshold = base + z * spread
        if series[i] > threshold:
            out.append(Spike(days[i], series[i], base, threshold, series[i] - base, _drivers(ledger, days[i], hist_days=days[max(0, i - window):i], team=team, dimension=driver_dimension)))
    return out


def _drivers(ledger: AuditLedger, day: str, *, hist_days: list[str], team: str | None, dimension: str) -> tuple[tuple[str, Decimal], ...]:
    today = ledger.group(dimension, start=day, end=day, team=team)
    keys = set(today) | set(ledger.group(dimension, start=hist_days[0], end=hist_days[-1], team=team))
    rows = []
    for k in sorted(keys):
        hist = []
        for d in hist_days:
            hist.append(ledger.group(dimension, start=d, end=d, team=team).get(k, Decimal(0)))
        rows.append((k, today.get(k, Decimal(0)) - median(hist)))
    rows = [r for r in rows if r[1] > 0]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return tuple(rows)
