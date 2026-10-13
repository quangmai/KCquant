#!/usr/bin/env python3
"""Download ~1 week of `statistics` for the 3 nearest KC contracts (cents) and
show which stat types make up the volume, so we know what we're paying for.

    python scripts/sample_statistics.py --start 2026-09-21 --end 2026-09-26
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import databento_dbn as dbn  # noqa: E402
import pandas as pd  # noqa: E402

from run_pipeline import load_dotenv  # noqa: E402
from kcquant.contracts import KC  # noqa: E402
from kcquant.data import DatabentoLoader  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--start", default="2026-09-21")
ap.add_argument("--end", default="2026-09-26")
a = ap.parse_args()
load_dotenv(ROOT / ".env")

loader = DatabentoLoader(KC, cache_dir=ROOT / "data" / "sample", max_cost_usd=5.0)
syms = [KC.databento_raw_symbol(*KC.parse(c)) for c in ["KCZ26", "KCH27", "KCK27"]]
df = loader._get("statistics", a.start, a.end, syms, label="SAMPLE").to_df().reset_index()
names = {getattr(dbn.StatType, k).value: k for k in dir(dbn.StatType) if k.isupper()}
df["stat"] = df["stat_type"].map(lambda x: names.get(int(x), str(x)))
print(f"{len(df):,} records over {df['ts_recv'].dt.date.nunique()} days, 3 contracts")
print("\nrecords by stat type:")
print(df["stat"].value_counts().to_string())
print("\nrecords per contract per day by stat type (median):")
per = df.groupby(["stat", "symbol", df["ts_recv"].dt.date]).size().groupby("stat").median()
print(per.sort_values(ascending=False).to_string())
