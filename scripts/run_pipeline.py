#!/usr/bin/env python3
"""End-to-end: load data -> roll -> decompose -> features -> outputs + chart.

Examples
  python scripts/run_pipeline.py --source synthetic
  DATABENTO_API_KEY=... python scripts/run_pipeline.py --source databento --start 2019-01-01
  python scripts/run_pipeline.py --source csv --path data/kc_daily.csv --rule oi
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from kcquant.contracts import SPECS  # noqa: E402
from kcquant.data import DatabentoLoader, SnapshotStatisticsLoader, load_csv, make_synthetic_panel, to_wide  # noqa: E402
from kcquant.decomposition import decompose_returns  # noqa: E402
from kcquant.features import curve_features  # noqa: E402
from kcquant.plotting import plot_overview  # noqa: E402
from kcquant.roll import FixedDaysBeforeFND, OpenInterestCrossover, RollConfig, build_continuous  # noqa: E402


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=value lines); doesn't override existing env vars."""
    import os
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="KC", choices=list(SPECS))
    ap.add_argument("--source", default="synthetic", choices=["synthetic", "databento", "csv"])
    ap.add_argument("--path", help="CSV/Parquet for --source csv")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--rule", default="fixed", choices=["fixed", "oi"])
    ap.add_argument("--days", type=int, default=10, help="fixed rule: bdays before FND")
    ap.add_argument("--cycle", default=None, help="roll cycle month codes, e.g. HNZ")
    ap.add_argument("--out", default=str(ROOT / "output"))
    ap.add_argument("--cost-only", action="store_true", help="databento: print cost estimate and exit")
    ap.add_argument("--max-cost", type=float, default=50.0, help="databento: abort above this USD per schema")
    ap.add_argument("--mode", default="snapshot", choices=["snapshot", "full"],
                    help="databento: 'snapshot' fetches only the daily end-of-day window (cheap); "
                         "'full' fetches the whole statistics feed")
    ap.add_argument("--window", default=None,
                    help="snapshot windows (exchange local), comma-separated; later ones are "
                         "fallbacks used only if earlier ones return no settle")
    ap.add_argument("--workers", type=int, default=30,
                    help="snapshot: parallel requests (lower it if Databento rate-limits)")
    ap.add_argument("--offline", action="store_true", help="databento snapshot: use cached data only")
    ap.add_argument("--window-months", type=int, default=None,
                    help="databento: fetch each contract only N months before delivery (cheaper)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    spec = SPECS[a.root]
    if a.source == "synthetic":
        panel = make_synthetic_panel(spec, a.start, a.end)
    elif a.source == "databento":
        if a.mode == "snapshot":
            loader = SnapshotStatisticsLoader(spec, cache_dir=ROOT / "data" / "raw", max_cost_usd=a.max_cost,
                                              windows=[tuple(w.split("-")) for w in a.window.split(",")] if a.window else None,
                                              workers=a.workers)
        else:
            loader = DatabentoLoader(spec, cache_dir=ROOT / "data" / "raw", max_cost_usd=a.max_cost,
                                     window_months=a.window_months)
        if a.cost_only:
            costs = loader.estimate_costs(a.start, a.end)
            n_days = costs.pop("_days_to_fetch", None)
            for k, v in costs.items():
                print(f"{k:22s} ${v:,.2f}")
            print(f"{'total':22s} ${sum(costs.values()):,.2f}  (already-cached data is not re-downloaded)")
            if n_days is not None:
                print(f"{n_days} days to fetch (one small request each)")
            return
        panel = loader.load(a.start, a.end, offline=a.offline) if a.mode == "snapshot" \
            else loader.load(a.start, a.end)
    else:
        panel = load_csv(a.path, spec)

    rule = FixedDaysBeforeFND(a.days) if a.rule == "fixed" else OpenInterestCrossover()
    res = build_continuous(panel, spec, RollConfig(rule=rule, roll_cycle=a.cycle))
    dec = decompose_returns(res, panel, spec)
    feats = curve_features(panel, spec)

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    tag = f"{a.root}_{a.source}_{a.rule}"
    panel.to_parquet(out / f"{tag}_panel.parquet")
    to_wide(panel).to_csv(out / f"{tag}_raw_curve.csv")
    res.daily.to_csv(out / f"{tag}_continuous.csv")
    res.rolls.to_csv(out / f"{tag}_rolls.csv")
    dec.to_csv(out / f"{tag}_decomposition.csv")
    feats.to_csv(out / f"{tag}_features.csv")

    rule_txt = f"roll {a.days} bdays before FND" if a.rule == "fixed" else "open-interest crossover"
    title = f"{a.root} continuous series & roll yield" + ("  —  SYNTHETIC DATA" if a.source == "synthetic" else "")
    sub = (f"{rule_txt} · {res.daily.index[0]:%b %Y} – {res.daily.index[-1]:%b %Y} · "
           f"{len(res.rolls)} rolls · source: {a.source}")
    png = plot_overview(res.daily, dec, feats, title, str(out / f"{tag}_overview.png"), sub,
                        price_unit=spec.price_unit)
    yrs = (dec.index[-1] - dec.index[0]).days / 365.25
    print(f"rolled {dec['cum_total_log'].iloc[-1]:+.3f}  spot {dec['cum_spot_log'].iloc[-1]:+.3f}  "
          f"roll yield {dec['cum_roll_yield_log'].iloc[-1]:+.3f} (log, {yrs:.1f}y)")
    print("wrote", png)


if __name__ == "__main__":
    main()
