#!/usr/bin/env python3
"""Free cost breakdown (uses Databento's get_cost; downloads nothing).

    python scripts/databento_costs.py --start 2019-01-01
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

from run_pipeline import load_dotenv  # noqa: E402
from kcquant.contracts import SPECS  # noqa: E402
from kcquant.data import DatabentoLoader  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="KC")
ap.add_argument("--start", default="2019-01-01")
ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
a = ap.parse_args()
load_dotenv(ROOT / ".env")
loader = DatabentoLoader(SPECS[a.root], cache_dir=ROOT / "data" / "raw")

print("== statistics cost by year (all contracts) ==")
for y in range(pd.Timestamp(a.start).year, pd.Timestamp(a.end).year + 1):
    s, e = max(pd.Timestamp(a.start), pd.Timestamp(f"{y}-01-01")), min(pd.Timestamp(a.end), pd.Timestamp(f"{y + 1}-01-01"))
    print(f"{y}  ${loader._cost('statistics', s, e, loader.symbols(a.start, a.end)):8.2f}")

print("\n== statistics cost by contract, full range (top 12) ==", flush=True)
bd = loader.cost_breakdown(a.start, a.end)
print(bd.head(12).drop(columns="note").to_string(index=False))
print(f"... {len(bd)} contracts, total ${bd['cost_usd'].sum():.2f}")
bad = bd[bd["cost_usd"].isna()]
if len(bad):
    print(f"{len(bad)} contracts Databento couldn't price (no data in range?):")
    print(bad[["contract", "note"]].to_string(index=False))

print("\n== total cost if each contract is only fetched N months before delivery ==")
print(f"{'window':>8} {'statistics':>11} {'ohlcv-1d':>9} {'definition':>11} {'total':>9}")
for wm in [None, 15, 12, 9, 6]:
    sys.stdout.flush()
    c = loader.estimate_costs(a.start, a.end, window_months=wm)
    print(f"{str(wm or 'all'):>8} {c['statistics']:11.2f} {c['ohlcv-1d']:9.2f} {c['definition']:11.2f} {sum(c.values()):9.2f}")
