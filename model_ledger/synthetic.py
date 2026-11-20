"""Reproducible synthetic usage, for tests, benchmarks and the examples."""
from __future__ import annotations

import datetime as _dt
import random
from typing import Any


def daterange(start: str, end: str) -> list[str]:
    a, b = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    return [(a + _dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def usage(
    start: str,
    end: str,
    streams: list[dict[str, Any]],
    seed: int = 0,
) -> list[dict[str, Any]]:
    """One event per day per stream. A stream is {team, model, input, output, [cached], [growth], [weekend], [noise], [tags], [mode],
    [surge: {from, factor}]} where input and output are the typical daily tokens, growth is the compound daily growth rate, weekend is the
    share of weekday volume on Saturday and Sunday, and surge multiplies the volume from a date onward."""
    rng = random.Random(seed)
    out = []
    for i, day in enumerate(daterange(start, end)):
        wd = _dt.date.fromisoformat(day).weekday()
        for n, s in enumerate(streams):
            f = (1 + s.get("growth", 0.0)) ** i * (s.get("weekend", 0.4) if wd >= 5 else 1.0)
            surge = s.get("surge")
            if surge and day >= surge["from"]:
                f *= surge["factor"]
            f *= max(0.2, 1 + rng.gauss(0, s.get("noise", 0.06)))
            ev: dict[str, Any] = {
                "day": day, "team": s["team"], "model": s["model"], "mode": s.get("mode", "standard"),
                "input_tokens": int(s["input"] * f), "output_tokens": int(s["output"] * f), "id": f"{day}:{n}:{s['team']}:{s['model']}",
            }
            if s.get("cached"):
                ev["cached_read_tokens"] = int(s["cached"] * f)
            if s.get("tags"):
                ev["tags"] = dict(s["tags"])
            out.append(ev)
    return out
