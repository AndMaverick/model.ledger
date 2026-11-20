"""Command line: ``python -m model_ledger report ledger.json``.

Exit codes: 0 gates pass (or none set), 1 a gate fails, 2 the input is unusable."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from . import report, scenario
from ._version import __version__
from .money import MoneyError


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return scenario.loads(fh.read())
    except OSError as exc:
        raise MoneyError(f"cannot read {path}: {exc.strerror}") from exc


def _events(path: str | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return scenario.events_from_csv(fh.read())
    except OSError as exc:
        raise MoneyError(f"cannot read {path}: {exc.strerror}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="model-ledger", description="AI spend accounting that finance can audit.")
    p.add_argument("--version", action="version", version=f"model.ledger {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("report", help="full report with gates")
    r.add_argument("ledger")
    r.add_argument("--events", help="read events from this CSV instead of the file")
    r.add_argument("--json", action="store_true", help="print the result as JSON")
    d = sub.add_parser("digest", help="print the tamper-evident digest of the ledger (pin it in 'gates.chain_head')")
    d.add_argument("ledger")
    d.add_argument("--events")
    f = sub.add_parser("fingerprint", help="print the SHA-256 fingerprint of the input file")
    f.add_argument("ledger")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = _load(args.ledger)
        if args.command == "fingerprint":
            scenario.analyze(data)
            print(scenario.fingerprint(data))
            return 0
        events = _events(getattr(args, "events", None))
        if args.command == "digest":
            print(scenario.digest(data, events))
            return 0
        result = scenario.analyze(data, events)
    except MoneyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.to_json(result) if args.json else report.to_text(result), end="\n" if args.json else "")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
