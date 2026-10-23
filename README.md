# kcquant — coffee futures research (ICE KC → RC)

Steps 4–5 of the plan: contract-level data → roll engine → return decomposition → curve features,
with point-in-time (PIT) discipline throughout.

```
src/kcquant/
  contracts.py      specs (KC, RC), ICE holiday calendars, FND / LND / LTD rules
  data.py           canonical panel, Databento loader (+cache), CSV loader, synthetic generator
  pit.py            decision times, as-of lookups, align_asof, assert_point_in_time
  roll.py           roll engine: fixed-days-before-FND or OI crossover; chained returns; ratio back-adjust
  decomposition.py  rolled return = spot return + roll yield
  features.py       c1–c2 spread, annualised slope, OLS curve slope, contango/backwardation flag
  plotting.py       overview chart
scripts/run_pipeline.py   end-to-end run → output/
tests/                    pytest suite (30 tests)
```

## Quick start

```bash
pip install -e ".[databento,dev]"
pytest
python scripts/run_pipeline.py --source synthetic            # demo, no API key needed
export DATABENTO_API_KEY=db-...
python scripts/probe_snapshot.py                              # ~$2: when does the daily snapshot run?
python scripts/run_pipeline.py --source databento --start 2019-01-01 --cost-only
python scripts/run_pipeline.py --source databento --start 2019-01-01 --rule fixed --days 10
python scripts/run_pipeline.py --source databento --rule oi
python scripts/run_pipeline.py --source csv --path my_kc.csv  # trade_date,contract,settle,volume,open_interest
```

## Data (Databento)

* Dataset `IFUS.IMPACT` (ICE Futures US; history from 2018-12-23). Robusta will be `IFEU.IMPACT`.
* Outrights are requested by explicit raw symbol, e.g. `KC  FMH0026!` (root padded to 4 chars),
  so we don't depend on ICE parent-symbol conventions.
* **Default: snapshot mode** (`--mode snapshot`). The full `statistics` feed is ~19,000 records per
  contract per day (intraday high/low/VWAP/volume and indicative-open updates); we use 3. Databento
  also sends an end-of-day snapshot (observed 18:00 New York) with settle, OI, cleared volume, open,
  high, low and VWAP, so the loader requests only an 18:00–19:00 window per weekday — ~0.04% of the
  records. Verified on a sample week: identical to the exchange-dated settles and OI. Check the snapshot
  time across history first with `scripts/probe_snapshot.py`. Cached per month in `data/raw/`; re-runs
  fetch only new days. Values are stamped 18:00 (conservative: the official settle is published
  ~12:35–12:50 NY, so features from day t are only used from day t+1's decision).
* `--mode full` downloads the whole feed (~$40 per year of KC history).
* `statistics` schema: settlement (stat_type 3), cleared volume (6), open interest (9).
  Volume falls back to `ohlcv-1d` if cleared volume is absent. `definition` is used to cross-check
  the computed last trading days against the exchange's `expiration`.
* Raw DBN files are cached in `data/raw/`; a cost estimate is checked against `max_cost_usd` before
  each download.
* **Verify on the first real pull** (couldn't be tested without an API key):
  that `stat_flags` bit 0 marks the official settle for IFUS, that `ts_ref` carries the session
  date for settle/OI, and that `validate_expirations` reports no mismatches.

Canonical panel: one row per `trade_date × contract` with `settle, volume, open_interest` and
`settle_available_at, volume_available_at, oi_available_at` (UTC).

## Point-in-time rules

* Every value has an `available_at`. From Databento that's `ts_recv` of the record actually used
  (for a settle: the first time the *official* value was seen, never the earlier preliminary).
  For CSV/synthetic data: settle at 14:00 NY on the trade date, OI at 10:00 NY the **next** business day.
* Roll decisions on day t are made at `decision_time` (default 13:00 NY, before the 13:30 close) and
  executed at that day's settle. They may only use data available by then, so the OI rule sees
  OI from t−1 at the latest.
* `held[t] = position after settle t−1`; `r[t] = S_held(t)/S_held(t−1) − 1`, always from one contract.
* Derived rows (returns, features) carry `available_at = max(inputs)`. Join them to trades
  with `pit.align_asof`, never by date.
* `backadj_price` is **not PIT** (it is rescaled by later rolls). It's there for charts only; use
  `tr_index` / `return` for research.
* Tests check truncation invariance: adding data after date T never changes results up to T.

## Conventions

* KC: H K N U Z, 37,500 lb, US cents/lb → $375 per 1.00 c/lb. FND = 7 business days before the first
  business day of the delivery month; LTD = 1 business day before LND.
* Curve positions on day t: contracts with FND > t, ordered by expiry (c1, c2, …).
* `slope_12_ann = ln(F2/F1)/(T2−T1)`, > 0 = contango. `carry_12_ann = −slope`. `curve_flag`: +1 contango,
  −1 backwardation, 0 flat (|slope| ≤ 1%/yr).
* Spot proxy for the decomposition: unadjusted price of c1. `roll_yield = total − spot` (log, additive).
* The holiday calendar is rule-based (an approximation). Add ad-hoc closures with `extra_holidays`
  or pin dates with `fnd_overrides` / `ltd_overrides`.

## Robusta (RC)

`contracts.RC`: months F H K N U X, 10 t, USD/t, London calendar, FND = 4th business day before the
first business day of the delivery month, decisions at 17:00 London (close 17:30). Databento symbols
(`RC  FMX0026!`) and snapshot windows verified by probe: 23:00 London snapshot, fallback 17:29:30–18:00
(the official settle arrives ~17:35–17:50, after the close), wide window for early closes. Run:

```bash
python scripts/run_pipeline.py --root RC --source databento --start 2019-01-01 --cost-only
python scripts/run_pipeline.py --root RC --source databento --start 2019-01-01 --rule fixed --days 5
python scripts/check_databento.py --root RC
```

## Signals & backtest (research)

```bash
python scripts/run_backtest.py            # uses cached data only; writes output/backtest/
```

* `signals.py` — carry (`carry_12_ann`, full size at 10%/yr), trend (sign of 3/6/12-month rolled
  return), KC/RC price-ratio z-score (1-year window, mean reversion). Parameters fixed in advance.
* `backtest.py` — decision at `spec.decision_time` on day d uses only data published by then
  (`pit.align_asof`), traded at d's settle, earns r[d+1]. EWMA vol targeting (10%/sleeve, cap 3x),
  costs = 1 tick/side + $2.50/contract, rolls pay both legs. Tests prove the position can't precede
  the signal's publication and that results up to T don't change when later data is added.
* Output: metrics, sub-period Sharpe, annual returns, robustness grid (parameters + cost stress),
  latest signals, daily P&L per sleeve, chart.
* First result (2019-01 .. 2026-10): no signal beats vol-targeted buy-and-hold; portfolio Sharpe
  0.14 ± 0.36. Treat everything here as research, not as trading recommendations.

## Fundamental data (research)

```bash
python scripts/fetch_fundamentals.py      # free sources, ~1-2 min; re-run to update
python scripts/run_backtest.py            # now also tests the fundamental hypotheses
```

Hypotheses, signal definitions and the pass bar were written down first:
`docs/fundamental_hypotheses.md` (H1-H5). `fundamentals.py` stamps every value with its publication time:

| Source | Refers to | Usable from |
|---|---|---|
| CFTC COT, Coffee C (disaggregated, futures only) | Tuesday positions | Friday 15:30 NY; federal-holiday weeks +1 business day; 2018-19 and 2025 shutdown catch-up dates hard-coded |
| ICE Futures Europe COT, Robusta | Tuesday positions | Friday 18:30 London (next trading day if closed) |
| BCB PTAX USD/BRL | that day | 13:30 São Paulo same day |
| Open-Meteo ERA5 weather (3 arabica + 3 robusta locations) | that day | 7 days later (ERA5 lag; small later revisions accepted) |

ICE Europe COT: the downloader tries `ice.com/publicdocs/futures/COTHist<year>.csv`. If that fails, download
the Commitments of Traders CSV history from the ICE Report Center and drop the files into
`data/fundamentals/ice_cot/`; the parser finds the robusta futures-only rows and the columns by name.

Verdicts compare each signal against buy-and-hold of the same market (alpha), require it in 2 of 3
sub-periods, and use a 2-standard-error bar because five hypotheses are tested at once.

### Certified warehouse stocks (H6, H8)

```bash
python scripts/fetch_cert_stocks.py          # daily ICE Coffee "C" stock reports -> data/fundamentals/ice_cert_stock/
```
`fundamentals.parse_cert_stock` reads certified, transition, passed/failed today, pending grading and
rebagging bags (plus origin x port detail); each report is usable 15 min after its "As of" time
(~13:15-13:25 NY), i.e. from the next session's decision. Hypotheses H6 and H8 are registered in
`docs/fundamental_hypotheses.md`; `run_backtest.py` tests them automatically once the reports exist.
ICE rate-limits this folder (HTTP 429 with Retry-After); the downloader sleeps through blocks and resumes.
