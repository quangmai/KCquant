#!/usr/bin/env python3
"""Download free fundamental data into data/fundamentals/ (re-run any time to update).

    python scripts/fetch_fundamentals.py
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from kcquant import fundamentals as fu  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
cache = ROOT / "data" / "fundamentals"
cache.mkdir(parents=True, exist_ok=True)
jobs = {
    "CFTC COT (Coffee C)": lambda: fu.fetch_cftc(cache, start="2015-01-01"),
    "ICE Europe COT (Robusta)": lambda: fu.fetch_ice_cot(cache, range(2015, pd.Timestamp.today().year + 1)),
    "BCB PTAX USD/BRL": lambda: fu.fetch_ptax(cache, start="2015-01-01"),
    "Open-Meteo weather": lambda: fu.fetch_weather(cache, start="2000-01-01"),
}
for name, job in jobs.items():
    try:
        df = job()
        idx = df.index
        print(f"OK   {name:26s} {len(df):7,d} rows  {idx.min().date()} .. {idx.max().date()}")
        if "available_at" in df:
            lag = (df["available_at"] - df.index.tz_localize("UTC")).dt.days if df.index.tz is None else None
            if lag is not None:
                print(f"     publication lag (days): median {lag.median():.0f}, max {lag.max():.0f}")
        if "mm_net_pct_oi" in df:
            print(f"     latest managed-money net % OI: {df['mm_net_pct_oi'].iloc[-1]:+.1%} "
                  f"(as of {df.index[-1].date()})")
    except Exception as err:
        print(f"FAIL {name:26s} {type(err).__name__}: {str(err).splitlines()[0][:150]}")
