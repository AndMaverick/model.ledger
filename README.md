# model.ledger

AI spend accounting that engineering and finance can both read, and an auditor can check. Every call is priced in exact decimals against a dated price book and written to a hash chained ledger, so any later change to the history is detectable. Shared costs are allocated to the cent, budgets are projected, spikes are flagged robustly, a change in spend is explained as a price, volume and mix bridge that adds up exactly, and the ledger is reconciled to the provider invoice with every dollar of difference assigned to a cause. Standard library only. Python 3.10 to 3.13.

AI cost problems rarely announce themselves. One team ships a prompt that doubles its context, and the bill moves a month later. A ledger that attributes every call to a team and a model makes that visible the day it happens, and one that reconciles to the invoice makes it something finance will sign.

## Use

```
python -m model_ledger report examples/platform_team.json     # exits 0: every gate passes
python -m model_ledger report examples/runaway_launch.json    # exits 1: six gates fail, each with its reason
python -m model_ledger digest examples/platform_team.json     # the tamper-evident digest of the ledger
```

Or from code:

```python
from model_ledger import AuditLedger, make_event, parse_book

book = parse_book({"large": {"input": 3.0, "output": 15.0, "cached_read": 0.3}}, modes={"batch": 0.5})
ledger = AuditLedger(book)
ledger.record(make_event({"day": "2026-10-01", "team": "eng/search", "model": "large", "input_tokens": 2_000_000, "output_tokens": 400_000, "id": "req-1"}))

ledger.total()          # Decimal('12.0')
ledger.group("team")    # {'eng/search': Decimal('12.0')}
ledger.verify()         # True; False the moment any recorded event or cost is altered
ledger.head             # the digest of the whole history: pin it, and any later edit shows
```

The 0.1 API (`Ledger`, `Price`, `Usage`, `UnknownModel`) is unchanged and still exported:

```python
from model_ledger import Ledger, Price, Usage

ledger = Ledger(prices={"small": Price(0.2, 0.6), "large": Price(3.0, 15.0)}, budgets={"search": 50.0})
ledger.record(Usage("search", "large", input_tokens=2000, output_tokens=1000, day="2026-03-01"))
ledger.by_team()       # {'search': 21.0}
```

## What it does

- **Exact costing.** Prices are per million tokens and held as decimals, never floats. Rates are tied to the date they take effect, so a price cut on 1 October does not rewrite September. A tier for long context applies to the whole request when input, cached reads and cache writes together pass its threshold. Cache classes default to the input rate. Modes such as batch are multipliers you configure; only `standard` is built in. A model with no price on the day raises an error instead of costing zero.
- **Tamper evidence.** Each entry hashes the previous entry, the event and the exact cost. Changing, dropping, adding or reordering any event changes the head. Event ids are unique: a call counted twice, the commonest way a ledger drifts from the invoice, is refused (or skipped on request).
- **Teams as a tree.** `eng/search` and `eng/support-bot` roll up to `eng`. Spend with no team lands in `unattributed`, and a gate caps its share.
- **Shared costs.** A commitment or platform cost is spread across teams by cost, tokens or fixed weights using allocation by largest remainder: the shares add to the total to the cent and no share is more than one cent from its exact proportion, which is the best any split into whole cents can do.
- **Budgets.** Spend against budget by team and month, a linear pace projection, and the day a budget was crossed.
- **Spikes.** A day is flagged when it exceeds the median of the previous 28 days by `z` robust deviations (median absolute deviation, scaled, floored at 10 percent of the median). A spike does not raise the bar for the next one the way a mean and standard deviation do. Each flagged day names the teams above their own typical level.
- **Why spend changed.** Between two periods, the change is split into volume, mix, rate, new and discontinued effects that add up to the change with no remainder, computed in exact fractions.
- **Invoice reconciliation.** Per model, the difference between invoice and ledger is split into usage variance (tokens the invoice billed that the ledger did not record, priced at the ledger's rates) and rate variance (the provider charged a different price for the tokens it billed). The two parts add up to the whole difference.
- **Gates.** `chain_head`, `no_budget_breach`, `no_projected_breach`, `max_spike_days`, `max_unattributed_share`, `max_invoice_variance`, `max_growth`. The CLI exits 0 when all pass, 1 when any fails, 2 on bad input.

## Sample output

From `python -m model_ledger report examples/runaway_launch.json` (synthetic usage, illustrative rates):

```
Budgets
  product          2026-10  23,144.96 of 18,000.00 (129%), projected 23,144.96: crossed on 2026-10-27
  eng              2026-10  5,908.82 of 26,000.00 (23%), projected 5,908.82: holding

Spikes
  2026-10-19  1,326.97 against a typical 775.00; driven by product/assist +561.50, unattributed +1.39, eng/search +0.71
  2026-10-22  1,988.67 against a typical 781.55; driven by product/assist +1,195.06, eng/support-bot +7.73, unattributed +6.01

Why spend changed: 20,675.34 to 32,096.17, 11,420.83 (+55.2%)
  volume               3,219.67
  mix                  8,468.94
  rate                  -267.79
  adds up exactly: yes

Invoice reconciliation: ledger 32,096.17, invoice 32,580.50, difference 484.33
  of which usage (tokens the ledger and invoice disagree on) 126.00, rate (price per token) 358.33
  embedding            ledger     1,119.95 invoice     1,245.95 usage     126.00 rate       0.00
  frontier-large       ledger    29,861.60 invoice    30,219.94 usage       0.00 rate     358.34

Gates
  FAIL  no_budget_breach: crossed: product 2026-10 on 2026-10-27
  FAIL  max_spike_days: 7 spike days, allowed 0
  FAIL  max_unattributed_share: 8.5% of spend is unattributed, allowed 5.0%
  FAIL  max_invoice_variance: largest difference between ledger and invoice is 484.33, allowed 1.00
  FAIL  max_growth: spend grew 55.2%, allowed 25.0%
RESULT: FAIL
```

The two defects planted in that file are found: three days of embedding usage that never reached the ledger ($126.00, all usage variance) and a 1.2 percent effective rate error on the large model ($358.33, all rate variance). The launch itself shows up as a new agent mode feature on the large model, which is why mix, not volume, carries most of the growth.

## File format

One JSON file. Unknown keys, duplicate keys, NaN and Infinity are rejected, so a typo cannot silently weaken a check.

```
{ "name": "...",
  "prices": { "<model>": { "input": 3.0, "output": 15.0, "cached_read": 0.3, "cache_write": 3.75,
                           "long_context": { "threshold": 200000, "input": 6.0, "output": 22.5 } } },
  "modes": { "batch": 0.5 },
  "events": [ { "day": "2026-10-01", "team": "eng/search", "model": "<model>", "input_tokens": 1000, "output_tokens": 200, "id": "optional-unique", "tags": { "feature": "x" } } ],
  "budgets": [ { "team": "eng", "period": "2026-10", "amount": 26000 } ],
  "shared": [ { "name": "platform commitment", "amount": "1500.00", "basis": "cost" } ],
  "window": ["2026-10-01", "2026-10-31"],  "compare": { "a": [...], "b": [...] },
  "invoice": [ { "model": "<model>", "input_tokens": 0, "output_tokens": 0, "amount": "0.00" } ],
  "anomaly": { "z": 4, "window": 28 },
  "gates": { "chain_head": "<digest>", "no_budget_breach": true, "max_spike_days": 0, "max_unattributed_share": 0.05 } }
```

Events can instead come from a CSV (`--events usage.csv`, columns `day,team,model,input_tokens,output_tokens` plus optional `mode`, `id`, `cached_read_tokens`, `cache_write_tokens` and `tag:<key>`). `python -m model_ledger fingerprint file.json` prints the SHA-256 of the input, and `digest` prints the ledger head to pin in `gates.chain_head`.

## How it is checked

`python examples/evaluate.py` compares the library with independent implementations (`tests/oracles.py`, which share no code with it) on random inputs with fixed seeds. This is the output of a run:

```
cost vs hand calculation: 5000 of 5000 requests exactly equal (2181 of them in the long-context tier)
allocation: 3000 of 3000 splits add to the cent, 3000 of 3000 match the brute-force optimum, 3000 of 3000 within one cent of exact
price-volume-mix bridge: 300 of 300 ledgers reconcile exactly, 300 of 300 total change equals the oracle's
invoice reconciliation: planted missing usage recovered exactly in 200 of 200 trials, unplanted lines clean in 200 of 200
tamper evidence: 100 of 100 one-token edits, 200 of 200 drops or swaps and 100 of 100 in-place edits detected
spike detection: 0 false alarms in 16600 clean days over 200 runs, 200 of 200 planted 2x weekday spikes found
ingest speed on this machine (varies): 82,452 events per second
```

The last line depends on the machine; the rest is deterministic. The test suite (87 tests) runs on Python 3.10 to 3.13 in CI, with ruff and strict mypy, and includes a check that output is identical byte for byte across different `PYTHONHASHSEED` values.

```
python -m unittest discover -s tests -t . -v
```

## Honest limits

- The spike result above is on synthetic series with 6 percent daily noise. A real series with promotions, outages or month end batch jobs will behave differently, so tune `z` and the window on your own history. Measured on the same series, a planted weekday spike is found in 200 of 200 runs at 2x, 169 of 200 at 1.5x and 48 of 200 at 1.3x, by design: a small bump inside normal noise is not a spike.
- The bridge compares the two periods as given. If they differ in length, volume carries that difference; normalize the windows first if you want a comparison per day.
- Spike drivers name the groups above their own typical level that day. Medians do not add, so the drivers are not an exact partition of the excess.
- The budget projection is a linear pace (spend so far over days so far). It overstates a month with a spike near the start and understates one with a launch near the end.
- Reconciliation assigns each difference to usage or rate, but cannot say why. A rate variance can be a stale price book, a discount, a commitment or provider rounding; that is for a person to decide.
- The example rates are illustrative, not any provider's price list. Load your own price book and check it against your contract before relying on a number.
- The ledger shows tampering after the fact. It does not prevent it, and it does not authenticate who wrote the events: pin the head somewhere the writer cannot edit.

Part of a set of small, tested tools for running AI in production, alongside [mao-framework](https://github.com/AndMaverick/mao-framework). MIT licensed. Built by [AndMaverick LLC](https://www.andmaverick.com).

---

**Read the thinking:** [The Meter Is Running. Nobody Built the Architecture to Stop It.](https://blog.andmaverick.com/the-meter-is-running-nobody-built-the-architecture-to-stop-it?utm_source=github&utm_medium=repo&utm_campaign=model.ledger)

**If this is your problem.** If your AI spend is outrunning your results, that is an architecture problem, and it is solvable. I am happy to look at it with you. [Begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=model.ledger).
