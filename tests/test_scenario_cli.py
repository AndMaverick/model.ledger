import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from model_ledger import MoneyError, analyze, cli, events_from_csv, fingerprint, loads
from model_ledger.report import to_text
from model_ledger.scenario import build_ledger, digest

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"


def load(name):
    return loads((EX / name).read_text(encoding="utf-8"))


class TestLoading(unittest.TestCase):
    def test_strict_json(self):
        for text in ('{"a": 1, "a": 2}', "[1]", "nope", '{"a": NaN}'):
            with self.subTest(text=text), self.assertRaises(MoneyError):
                loads(text)

    def test_csv_round_trip_and_errors(self):
        text = "day,team,model,input_tokens,output_tokens,mode,id,tag:feature\n2026-03-01,a,m,1000,500,batch,e1,search\n2026-03-02,b,m,2000,,,,\n"
        rows = events_from_csv(text)
        self.assertEqual(rows[0], {"day": "2026-03-01", "team": "a", "model": "m", "input_tokens": 1000, "output_tokens": 500, "mode": "batch", "id": "e1", "tags": {"feature": "search"}})
        self.assertEqual(rows[1], {"day": "2026-03-02", "team": "b", "model": "m", "input_tokens": 2000})
        for bad in ("day,team\n1,2\n", "day,team,model,input_tokens,output_tokens,colour\n", "day,team,model,input_tokens,output_tokens\n2026-03-01,a,m,x,1\n"):
            with self.subTest(bad=bad[:30]), self.assertRaises(MoneyError):
                events_from_csv(bad)


class TestAnalysis(unittest.TestCase):
    def test_platform_team_passes_and_is_pinned(self):
        r = analyze(load("platform_team.json"))
        self.assertTrue(r["passed"], [g for g in r["gates"] if not g["passed"]])
        self.assertTrue(r["ledger"]["verified"])
        self.assertEqual({g["gate"] for g in r["gates"]}, {"chain_head", "no_budget_breach", "no_projected_breach", "max_spike_days", "max_unattributed_share", "max_invoice_variance", "max_growth"})

    def test_runaway_launch_fails_on_every_gate_but_the_chain(self):
        r = analyze(load("runaway_launch.json"))
        self.assertFalse(r["passed"])
        self.assertEqual({g["gate"] for g in r["gates"] if not g["passed"]}, {g["gate"] for g in r["gates"]})

    def test_runaway_reconciliation_recovers_the_planted_defects(self):
        rec = analyze(load("runaway_launch.json"))["reconciliation"]
        lines = {x["model"]: x for x in rec["lines"]}
        self.assertEqual(D(lines["embedding"]["usage_variance"]), D("126.000000"))           # three silent days of embedding usage at 0.10 per million
        self.assertLess(abs(D(lines["embedding"]["rate_variance"])), D("0.005"))              # the invoice is in whole cents
        self.assertLess(abs(D(lines["frontier-small"]["variance"])), D("0.005"))
        self.assertAlmostEqual(float(D(lines["frontier-large"]["rate_variance"]) / D(lines["frontier-large"]["ledger"])), 0.012, places=5)
        self.assertTrue(rec["explained"])

    def test_tampering_with_one_event_breaks_the_pinned_digest(self):
        d = load("platform_team.json")
        self.assertTrue(analyze(d)["passed"])
        d["events"][100]["input_tokens"] += 1
        r = analyze(d)
        self.assertFalse(r["passed"])
        failed = [g for g in r["gates"] if not g["passed"]]
        self.assertEqual([g["gate"] for g in failed], ["chain_head"])

    def test_dropping_or_reordering_events_changes_the_digest(self):
        d = load("platform_team.json")
        head = digest(d)
        dropped = copy.deepcopy(d)
        del dropped["events"][5]
        swapped = copy.deepcopy(d)
        swapped["events"][3], swapped["events"][4] = swapped["events"][4], swapped["events"][3]
        self.assertNotEqual(digest(dropped), head)
        self.assertNotEqual(digest(swapped), head)

    def test_group_totals_add_up(self):
        led = build_ledger(load("platform_team.json"))
        for key in ("team", "model"):
            self.assertEqual(sum(led.group(key).values(), D(0)), led.total())              # exact inside the ledger
        r = analyze(load("platform_team.json"))
        total = D(r["total"])
        for key in ("by_team", "by_model"):
            rows = r[key]
            self.assertLessEqual(abs(sum(D(v) for v in rows.values()) - total), D("0.0000005") * len(rows))   # shown to 6 places
        for rows in r["tags"].values():
            self.assertLessEqual(abs(sum(D(v) for v in rows.values()) - total), D("0.0000005") * len(rows))
        self.assertEqual(r["shared"]["sums_exact"], True)
        loaded = sum(D(v) for v in r["shared"]["fully_loaded"].values())
        self.assertAlmostEqual(float(loaded), float(total) + 1500.0, delta=0.03)             # per-team rounding of direct spend

    def test_bridge_in_the_result_reconciles(self):
        for name in ("platform_team.json", "runaway_launch.json"):
            b = analyze(load(name))["bridge"]
            self.assertTrue(b["reconciles"])
            self.assertAlmostEqual(sum(float(v) for v in b["effects"].values()), float(b["delta"]), places=4)

    def test_reproducible(self):
        a, b = analyze(load("runaway_launch.json")), analyze(load("runaway_launch.json"))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_csv_events_override(self):
        d = load("platform_team.json")
        text = "day,team,model,input_tokens,output_tokens\n2026-10-01,a,embedding,1000000,0\n2026-10-02,a,embedding,2000000,0\n"
        small = {k: v for k, v in d.items() if k in {"prices", "modes", "name"}}
        r = analyze(small, events_from_csv(text))
        self.assertEqual(r["total"], "0.300000")

    def test_unknown_model_in_events_is_an_input_error(self):
        d = load("platform_team.json")
        d["events"].append({"day": "2026-10-31", "team": "a", "model": "ghost", "input_tokens": 1, "output_tokens": 1})
        with self.assertRaises(KeyError):
            analyze(d)

    def test_bad_inputs(self):
        base = load("platform_team.json")
        edits = {
            "unknown key": lambda d: d.update(extra=1),
            "no events": lambda d: d.update(events=[]),
            "bad window": lambda d: d.update(window=["2026-10-31", "2026-10-01"]),
            "bad budget": lambda d: d.update(budgets=[{"team": "a"}]),
            "unknown gate": lambda d: d["gates"].update(nope=1),
            "bad chain head": lambda d: d["gates"].update(chain_head="abc"),
            "bad spike gate": lambda d: d["gates"].update(max_spike_days=-1),
            "invoice gate without invoice": lambda d: (d.pop("invoice"), None),
            "growth gate without compare": lambda d: (d.pop("compare"), None),
            "bad shared": lambda d: d.update(shared=[{"name": "x", "amount": "1"}]),
            "bad anomaly key": lambda d: d.update(anomaly={"zed": 3}),
            "bad compare": lambda d: d.update(compare={"a": ["2026-09-01", "2026-09-30"]}),
        }
        for label, edit in edits.items():
            d = copy.deepcopy(base)
            edit(d)
            with self.subTest(label), self.assertRaises((MoneyError, ValueError, KeyError)):
                analyze(d)

    def test_text_report_content_and_no_em_dash(self):
        text = to_text(analyze(load("runaway_launch.json")))
        for needle in ("RESULT: FAIL", "Spikes", "Why spend changed", "Invoice reconciliation", "crossed on", "adds up exactly: yes", "By feature"):
            self.assertIn(needle, text)
        self.assertNotIn(chr(0x2014), text)
        self.assertNotIn("-0.00", text)

    def test_fingerprint_stable_and_sensitive(self):
        a = load("platform_team.json")
        reordered = json.loads(json.dumps(dict(reversed(list(a.items())))))
        self.assertEqual(fingerprint(a), fingerprint(reordered))
        b = copy.deepcopy(a)
        b["events"][0]["output_tokens"] += 1
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_build_ledger_is_exact_and_verifies(self):
        led = build_ledger(load("runaway_launch.json"))
        self.assertTrue(led.verify())
        self.assertEqual(led.total(), sum(e.cost for e in led.entries))


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_cli("report", str(EX / "platform_team.json"))[0], 0)
        self.assertEqual(self.run_cli("report", str(EX / "runaway_launch.json"))[0], 1)
        code, _, err = self.run_cli("report", str(EX / "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_bad_file_and_unknown_model_are_exit_2(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"prices": {"m": {"input": 1, "output": 1}}, "events": [{"day": "2026-01-01", "team": "a", "model": "ghost", "input_tokens": 1, "output_tokens": 1}]}, fh)
        try:
            code, _, err = self.run_cli("report", fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(code, 2)
        self.assertIn("ghost", err)

    def test_json_digest_fingerprint_and_csv(self):
        code, out, _ = self.run_cli("report", str(EX / "platform_team.json"), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        code, dig, _ = self.run_cli("digest", str(EX / "platform_team.json"))
        self.assertEqual(dig.strip(), data["ledger"]["head"])
        code, fp, _ = self.run_cli("fingerprint", str(EX / "platform_team.json"))
        self.assertEqual(fp.strip(), fingerprint(load("platform_team.json")))
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            fh.write("day,team,model,input_tokens,output_tokens\n2026-10-01,a,embedding,1000000,0\n")
        try:
            small = {k: v for k, v in load("platform_team.json").items() if k in {"prices", "modes", "name"}}
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as jf:
                json.dump(small, jf)
            try:
                code, out, _ = self.run_cli("report", jf.name, "--events", fh.name, "--json")
            finally:
                os.unlink(jf.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["total"], "0.100000")

    def test_module_entry_point_is_deterministic_across_hash_seeds(self):
        outs = []
        for hash_seed in ("0", "7", "4242"):
            env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(ROOT)}
            p = subprocess.run([sys.executable, "-m", "model_ledger", "report", str(EX / "runaway_launch.json"), "--json"],
                               capture_output=True, text=True, env=env, cwd=ROOT, check=False)
            self.assertEqual(p.returncode, 1, p.stderr)
            outs.append(p.stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])


if __name__ == "__main__":
    unittest.main()
