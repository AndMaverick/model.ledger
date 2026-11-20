"""The ledger: validated usage events, exact costs, a tamper-evident hash chain and rollups along any dimension."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .money import MoneyError, canonical
from .pricing import CLASSES, PriceBook, parse_day

GENESIS = "0" * 64


@dataclass(frozen=True)
class Event:
    """One metered call, or one batch of identical calls, attributed to a team and a model on a day."""

    day: str
    team: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_read_tokens: int = 0
    cache_write_tokens: int = 0
    mode: str = "standard"
    tags: tuple[tuple[str, str], ...] = ()
    id: str | None = None

    def tag(self, key: str) -> str | None:
        return dict(self.tags).get(key)

    def tokens(self) -> dict[str, int]:
        return {"input": self.input_tokens, "output": self.output_tokens, "cached_read": self.cached_read_tokens, "cache_write": self.cache_write_tokens}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "day": self.day, "team": self.team, "model": self.model, "mode": self.mode,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cached_read_tokens": self.cached_read_tokens, "cache_write_tokens": self.cache_write_tokens,
        }
        if self.tags:
            d["tags"] = dict(self.tags)
        if self.id is not None:
            d["id"] = self.id
        return d


EVENT_KEYS = {"day", "team", "model", "mode", "input_tokens", "output_tokens", "cached_read_tokens", "cache_write_tokens", "tags", "id"}


def make_event(raw: dict[str, Any], where: str = "event") -> Event:
    if not isinstance(raw, dict):
        raise MoneyError(f"{where}: expected an object")
    extra = sorted(set(raw) - EVENT_KEYS)
    if extra:
        raise MoneyError(f"{where}: unknown keys {extra}")
    for k in ("day", "team", "model", "input_tokens", "output_tokens"):
        if k not in raw:
            raise MoneyError(f"{where}: missing {k}")
    day = parse_day(raw["day"], f"{where}.day")
    for k in ("team", "model"):
        if not isinstance(raw[k], str) or not raw[k].strip():
            raise MoneyError(f"{where}.{k}: must be a non-empty string")
    nums = {}
    for k in ("input_tokens", "output_tokens", "cached_read_tokens", "cache_write_tokens"):
        v = raw.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise MoneyError(f"{where}.{k}: a non-negative whole number of tokens")
        nums[k] = v
    mode = raw.get("mode", "standard")
    if not isinstance(mode, str) or not mode:
        raise MoneyError(f"{where}.mode: must be a non-empty string")
    tags_raw = raw.get("tags", {})
    if not isinstance(tags_raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags_raw.items()):
        raise MoneyError(f"{where}.tags: must map text to text")
    eid = raw.get("id")
    if eid is not None and (not isinstance(eid, str) or not eid):
        raise MoneyError(f"{where}.id: must be a non-empty string")
    return Event(day, raw["team"], raw["model"], nums["input_tokens"], nums["output_tokens"], nums["cached_read_tokens"], nums["cache_write_tokens"],
                 mode, tuple(sorted(tags_raw.items())), eid)


def entry_hash(prev: str, event: Event, cost: Decimal) -> str:
    body = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{prev}\n{body}\n{canonical(cost)}".encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Entry:
    event: Event
    cost: Decimal
    parts: dict[str, Decimal]
    hash: str


class DuplicateEvent(ValueError):
    """An event id was recorded twice."""


def month_of(day: str) -> str:
    return day[:7]


@dataclass
class AuditLedger:
    book: PriceBook
    entries: list[Entry] = field(default_factory=list)
    _ids: set[str] = field(default_factory=set)

    @property
    def head(self) -> str:
        """The hash of the whole history. Changing, removing, adding or reordering any event changes it."""
        return self.entries[-1].hash if self.entries else GENESIS

    def record(self, event: Event, on_duplicate: str = "error") -> Decimal | None:
        """Price an event and append it. An event carrying an id that is already in the ledger is refused (or skipped with
        ``on_duplicate="skip"``), because the same call counted twice is the commonest way a ledger drifts from the invoice."""
        if on_duplicate not in {"error", "skip"}:
            raise ValueError("on_duplicate must be 'error' or 'skip'")
        if event.id is not None and event.id in self._ids:
            if on_duplicate == "skip":
                return None
            raise DuplicateEvent(event.id)
        parts = self.book.cost_parts(event.model, event.day, event.mode, event.input_tokens, event.output_tokens,
                                     event.cached_read_tokens, event.cache_write_tokens)
        cost = sum(parts.values(), Decimal(0))
        self.entries.append(Entry(event, cost, parts, entry_hash(self.head, event, cost)))
        if event.id is not None:
            self._ids.add(event.id)
        return cost

    def record_many(self, events: Iterable[Event], on_duplicate: str = "error") -> int:
        n = 0
        for e in events:
            if self.record(e, on_duplicate) is not None:
                n += 1
        return n

    def verify(self) -> bool:
        """Recompute every cost from the price book and every link of the chain. False if anything was altered after recording."""
        prev = GENESIS
        for e in self.entries:
            parts = self.book.cost_parts(e.event.model, e.event.day, e.event.mode, e.event.input_tokens, e.event.output_tokens,
                                         e.event.cached_read_tokens, e.event.cache_write_tokens)
            cost = sum(parts.values(), Decimal(0))
            if cost != e.cost or entry_hash(prev, e.event, cost) != e.hash:
                return False
            prev = e.hash
        return True

    def select(self, *, team: str | None = None, model: str | None = None, start: str | None = None, end: str | None = None,
               tags: dict[str, str] | None = None) -> list[Entry]:
        """Entries matching all filters. ``team`` matches that team and everything below it ("eng" includes "eng/search")."""
        out = []
        for e in self.entries:
            ev = e.event
            if team is not None and not (ev.team == team or ev.team.startswith(team + "/")):
                continue
            if model is not None and ev.model != model:
                continue
            if start is not None and ev.day < start:
                continue
            if end is not None and ev.day > end:
                continue
            if tags and any(ev.tag(k) != v for k, v in tags.items()):
                continue
            out.append(e)
        return out

    def total(self, **filters: Any) -> Decimal:
        return sum((e.cost for e in self.select(**filters)), Decimal(0))

    def group(self, dimension: str, **filters: Any) -> dict[str, Decimal]:
        """Cost by "team", "model", "day", "month", "mode" or "tag:<key>" (events without the tag fall under "(none)")."""
        out: dict[str, Decimal] = defaultdict(Decimal)
        for e in self.select(**filters):
            out[_key(e.event, dimension)] += e.cost
        return dict(sorted(out.items()))

    def tokens(self, **filters: Any) -> dict[str, int]:
        out = dict.fromkeys(CLASSES, 0)
        for e in self.select(**filters):
            for c, n in e.event.tokens().items():
                out[c] += n
        return out

    def class_costs(self, **filters: Any) -> dict[tuple[str, str], tuple[int, Decimal]]:
        """Tokens and cost per (model, token class), the unit the bridge and the reconciliation work in."""
        tok: dict[tuple[str, str], int] = defaultdict(int)
        cost: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for e in self.select(**filters):
            toks = e.event.tokens()
            for c in CLASSES:
                tok[(e.event.model, c)] += toks[c]
                cost[(e.event.model, c)] += e.parts[c]
        return {k: (tok[k], cost[k]) for k in sorted(tok)}

    def team_tree(self, **filters: Any) -> dict[str, Decimal]:
        """Every team and every ancestor, with its own cost plus everything beneath it ("eng/search" rolls up into "eng")."""
        out: dict[str, Decimal] = defaultdict(Decimal)
        for e in self.select(**filters):
            parts = e.event.team.split("/")
            for i in range(1, len(parts) + 1):
                out["/".join(parts[:i])] += e.cost
        return dict(sorted(out.items()))


def _key(ev: Event, dimension: str) -> str:
    if dimension == "team":
        return ev.team
    if dimension == "model":
        return ev.model
    if dimension == "day":
        return ev.day
    if dimension == "month":
        return month_of(ev.day)
    if dimension == "mode":
        return ev.mode
    if dimension.startswith("tag:"):
        return ev.tag(dimension[4:]) or "(none)"
    raise ValueError(f"unknown dimension {dimension!r}")
