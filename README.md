# model.ledger

LLM spend accounting that engineering and finance can both read. A price book, per-team and per-model rollups, budget breach detection, daily series, and spike detection. Standard library only.

AI cost problems rarely announce themselves. One team ships a prompt that doubles its context, and the bill moves a month later. A ledger that attributes every call to a team and a model makes that visible the day it happens.

## Use

```python
from model_ledger import Ledger, Price, Usage

ledger = Ledger(
    prices={"small": Price(0.2, 0.6), "large": Price(3.0, 15.0)},
    budgets={"search": 50.0},
)
ledger.record(Usage("search", "large", input_tokens=2000, output_tokens=1000, day="2026-03-01"))

ledger.by_team()       # {'search': 21.0}
ledger.over_budget()   # {} until search passes 50.0
ledger.spikes()        # days far above the prior trend
```

## What it does

- **Costing.** Input and output tokens priced separately per model. An event for a model with no price raises `UnknownModel` instead of silently costing zero.
- **Rollups.** Total, by team, by model, and daily, optionally scoped to one team.
- **Budgets.** `over_budget()` returns each team past its limit with spend and budget side by side.
- **Spikes.** A day is flagged when it exceeds the mean of all prior days by `z` deviations. The deviation is floored at 10 percent of the mean so flat, healthy history does not produce false alarms.

## Tests

```
python -m unittest discover -s tests -t . -v
```

Part of a set of small, tested tools for running AI in production, alongside [mao-framework](https://github.com/AndMaverick/mao-framework). MIT licensed. Built by [AndMaverick LLC](https://www.andmaverick.com).

---

**Related reading:** [The Meter Is Running. Nobody Built the Architecture to Stop It.](https://blog.andmaverick.com/the-meter-is-running-nobody-built-the-architecture-to-stop-it?utm_source=github&utm_medium=repo&utm_campaign=model.ledger)

**If this is your problem.** If your AI spend is outrunning your results, that is an architecture problem, and it is solvable. I am happy to look at it with you. [Begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=model.ledger).
