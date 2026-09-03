# ustrade — US-stock strategy lab and paper-trading system (showcase snapshot)

> 한국어: [README.ko.md](README.ko.md)

## What this is

A personal system for developing, validating, and running US-equity trading
strategies. It has two halves: a **backtest lab** where a strategy is defined
once (`generate_signals`/`generate_weights`) and run through three
independent engines (a vectorized runner, `backtrader`, `vectorbt`) so results
can be cross-checked instead of trusted from a single implementation, and a
**live loop** (selection → risk overlay → guardrail → execution) that runs
unattended against a paper broker and, if ever enabled, a real broker adapter
for Toss Securities' Open API (US stocks). Real-money trading is **not**
enabled in this build; the system has been running unattended paper trading
only, on a VM, since 2026-08-09. This repo exists to show how the author
works with Claude Code on a multi-month project, not to sell a strategy.

## Performance caveat — lower your expectations

The Sharpe/MDD/return numbers that appear in [README.ko.md](README.ko.md) and
in the backtest tooling are **hypothetical, in-sample backtest results**, not
a live track record and not a promise of future returns.

- **Upward-biased.** Universes are static lists of *today's* constituents, so
  delisted/failed tickers are silently absent (survivorship bias). Slippage,
  bid/ask spread, and taxes (US capital gains, dividend withholding) are not
  modeled. Realized net returns would be materially lower than reported
  figures.
- **Selection bias compounds it**, especially for the `sp100` universe:
  today's index members are yesterday's winners, so a momentum backtest over
  that list leaks future information into the past. Concentrated portfolios
  (top-3) amplify this into non-repeatable numbers.
- Paper/small-size verification is required before any real trade, and the
  system treats "unattended automation + a bug" as equivalent to a real loss
  — the guardrail layer (below) is the last line of defense, not the first.

The value of this project is the *methodology* it produced (parameter
overfitting is measurable via train/test rank correlation, volatility
targeting is the cheapest way to cut drawdown, stop-losses hurt a momentum
strategy) — not an absolute-return claim.

## How Claude Code is used

The author (a chemical engineering undergraduate, not a professional
developer) designed and iterated this whole system with Claude Code as the
primary development partner: architecture decisions, implementation, test
suites (23 suite files under `tests/`), debugging production incidents (e.g. a
Windows Task Scheduler env-variable caching bug that silently disabled a
fundamentals filter for three days — documented in `README.ko.md`), the
"desk" memory structure described below, adversarial dual-team code reviews
before merging risk-relevant changes, deploy scripts, and VM operations for
the unattended paper-trading run. Commits carry `Co-Authored-By: Claude`
trailers.

One concrete artifact of that review process:
`archive/reviews/20260622_122457_dualteam/FINAL_SYNTHESIS.md` — two adversarial
review teams (one framed as a red team hunting for order-path, guardrail, and
silent-failure bugs) independently audit a change and their findings are
synthesized before anything ships to the live-trading path.

## Architecture

```
data.py                 yfinance download + CSV cache
universe.py              stock baskets (megacap/tech/diversified/sp100/sp500/growth)
strategies/              generate_signals(df) [single-ticker] or
                         generate_weights(panel) [portfolio] — one definition
engines/
  simple_runner.py       lightweight vectorized backtest
  bt_runner.py            backtrader (event-driven, closest to live)
  vbt_runner.py            vectorbt (fast parameter sweeps)
  portfolio_runner.py     multi-asset: drift, turnover, fees, benchmark
  risk_runner.py          risk overlay: regime filter, vol targeting, stop-loss
broker/
  base.py                 BaseBroker ABC + data models (Order/Position/Quote)
  paper.py                 PaperBroker — simulated fills, exercises the live path
  toss.py                  TossBroker — Toss Securities Open API adapter (US-only)
  managed.py               ManagedBroker — a "managed sleeve" that keeps the bot's
                            trades from touching pre-existing holdings in the account
  guardrail.py             KillSwitch — unattended safety limits (persisted state)
live_engine.py             run_once(): selection -> risk -> guardrail -> execution
run_live.py                 daily entry point (cron/Task Scheduler), journals + alerts
run_exit.py                 intraday exit checks (trend-break / stop-loss rules)
personas.py                 10 paper-trading personas (strategy presets, see below)
sweep.py / walkforward.py   overfitting diagnostics (train/test grid, rolling OOS)
backtest_risk.py            risk-layer ablation studies
eval_factor.py               cross-sectional factor IC validation
tools/                       deploy scripts, VM watchdogs, sp500 refresh
dashboard/                   local web dashboard for run history / NAV
```

`personas.py` defines 10 paper-trading configurations used to compare
investing philosophies against the same starting capital: 7 base personas
(`buffett`, `wood`, `oneil`, `canslim_rdcf`, `livermore`, `chartist`,
`livermore_swing`) plus 3 controlled-comparison variants (`buffett_v2`,
`livermore_ctl`, `chartist_ctl`) that isolate a rule change or a
curation-bias effect from the base persona.

## The house/desk structure — memory that outlives sessions

Skills and scripts are stateless: running a screener 100 times doesn't make
the 101st run remember what the first 100 learned. This repo is organized as
one operating "house" with five desks (`desks/research`, `strategy`, `risk`,
`execution`, `performance`), each owning a `soul.md` (what it owns, what
counts as done, what escalates to a human), a `goals.md`, and an
append-only `memory.md`.

The rule that makes this useful: **no desk grades its own work.** Research
output is reviewed by strategy, strategy by risk, risk by performance — and a
rejection is written back to the author desk's `memory.md` with a reason.
An un-recorded rejection just repeats. See `desks/README.md` for the full
rationale and the review-pair table. `HOUSE.md` is the compiled operating
rulebook (universe definitions, risk limits, kill-switch rules, current
experiment status) that gets read before every trading session instead of
re-deriving limits from code each time.

`docs/paper-trading-dod.md` is a finite, checkable definition-of-done list
that the paper-trading environment had to clear before the current 12-week
experiment was allowed to start — each item needs a stated verification
method, not just a completion claim, and adding an item to the list counts
as delaying completion, not as free scope.

## Status

Paper trading only, on a VM, per `HOUSE.md` §1/§B (v2.2): **canslim**
selection over the **sp100** universe, top_n = 5, from a fresh $100,000
paper book, 12-week window 2026-08-09 → 2026-11-01. No real capital is at
risk and the Toss broker adapter has never been switched to live orders. An
hourly "deadman" watch (`tools/paper_watch.py`) pages if the daily run
doesn't show up within its window. The judgment framework separates an
**operating gate** (no-show rate, kill-switch trips, journal integrity), a
**risk gate** (max drawdown ≤ 20%), and a **performance verdict**
(t-statistic vs. SPY, with an "undecided, extend the window" branch) — it is
explicit that this experiment cannot establish statistical significance for
the strategy's edge in any practical timeframe.

This repo is a **single-commit snapshot** of a private repository (195
commits, 2026-06-24 → 2026-08-28) taken for this application; it has no
further commit history. Secrets were never committed (`.gitignore` excludes
`*.key`/`.env`/`*.pem`, plus runtime state/log/cache dirs; the history was
scanned before publishing), and two operational identifiers (the VM's
hostname and Tailscale IP) were replaced with `<vm-host>` /
`<vm-tailscale-ip>` throughout. Not included: trained models, proprietary
data feeds, account credentials, or a working FMP API key (historical
fundamentals backtesting needs one and isn't runnable from this snapshot),
or point-in-time index constituent data (removing survivorship/selection
bias would need a paid data source, out of scope for a personal project).

## Running it

```bash
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt

python backtest.py --ticker AAPL --strategy ma_cross --engine simple
python backtest.py --ticker NVDA --strategy momentum --engine all --start 2018-01-01
```

23 test suite files live under `tests/` (`tests_stage1.py` … `tests_stage8.py`,
plus feature suites like `tests_canslim.py`, `tests_toss.py`,
`tests_managed.py`). They use a `tests_*` prefix so pytest's default
`test_*` collection skips them; `test_suites.py` at the repo root
parametrizes over all of them and is the actual pytest entry point
(`pyproject.toml` pins `python_files = ["test_suites.py"]`):

```bash
pip install -e ".[dev]"
pytest -q
```

## Korean documentation

Full design rationale, backtest tooling walkthrough, and the Toss
live-trading checklist: [README.ko.md](README.ko.md).

## License

MIT — see [LICENSE](LICENSE).
