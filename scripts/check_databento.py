#!/usr/bin/env python3
"""Inspect the cached Databento files and check the loader's assumptions.

Run after a databento pull, then paste the whole output back for review:
    python scripts/check_databento.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import databento as db  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from kcquant.contracts import SPECS  # noqa: E402
import argparse  # noqa: E402
from kcquant.data import normalize_databento_statistics, raw_symbol_to_contract, validate_expirations  # noqa: E402

RAW = ROOT / "data" / "raw"
_ap = argparse.ArgumentParser()
_ap.add_argument("--root", default="KC", choices=list(SPECS))
SPEC = SPECS[_ap.parse_args().root]
KC = SPEC   # (name kept from the KC-only version)
pd.set_option("display.width", 160, "display.max_columns", 20)


def latest(schema):
    files = sorted(RAW.glob(f"{SPEC.root}_{schema}_*.dbn.zst"))
    if not files:
        sys.exit(f"no cached {schema} file in {RAW} - run the databento pipeline first")
    print(f"[{schema}] {files[-1].name}")
    return db.DBNStore.from_file(str(files[-1])).to_df()


def ny_hhmm(s):
    return pd.to_datetime(s, utc=True).dt.tz_convert(SPEC.timezone).dt.strftime("%H:%M")


snap = sorted(RAW.glob(f"{SPEC.root}_statsnap_*.parquet"))
if snap:
    print(f"[statistics] {len(snap)} snapshot month files ({snap[0].name} .. {snap[-1].name})")
    stats = pd.concat([pd.read_parquet(f) for f in snap], ignore_index=True)
else:
    stats = latest("statistics").reset_index()
print("\n== 1. symbols ==")
print("sample raw symbols:", stats["symbol"].drop_duplicates().head(8).tolist())
mapped = stats["symbol"].map(lambda s: raw_symbol_to_contract(str(s), KC))
print(f"rows mapped to outrights: {mapped.notna().mean():.1%}; contracts: {mapped.nunique()}")

print("\n== 2. stat types present (rows) ==")
print(stats.groupby(["stat_type", "update_action"]).size().to_string())

for st, name in [(3, "settlement"), (9, "open interest"), (6, "cleared volume")]:
    sub = stats[stats["stat_type"] == st]
    if sub.empty:
        print(f"\n== {name}: NONE ==")
        continue
    print(f"\n== 3. {name} (stat_type {st}) ==")
    ref = pd.to_datetime(sub["ts_ref"], utc=True, errors="coerce") if "ts_ref" in sub else pd.Series(pd.NaT)
    ok_ref = ref.notna() & (ref > pd.Timestamp("1990-01-01", tz="UTC"))
    print(f"ts_ref populated: {ok_ref.mean():.1%}")
    if ok_ref.any():
        print("ts_ref time-of-day (UTC):", ref[ok_ref].dt.strftime("%H:%M").value_counts().head(3).to_dict())
        lag = (pd.to_datetime(sub.loc[ok_ref, "ts_recv"], utc=True).dt.normalize() - ref[ok_ref].dt.normalize()).dt.days
        print("days from ts_ref date to ts_recv date:", lag.value_counts().sort_index().head(6).to_dict())
    print("stat_flags:", sub["stat_flags"].value_counts().to_dict())
    print("ts_recv local time (top):", ny_hhmm(sub["ts_recv"]).value_counts().head(6).to_dict())
    per = sub.assign(c=mapped).groupby(["c", ref.dt.date if ok_ref.any() else sub["ts_event"]]).size()
    print("records per contract-day:", per.value_counts().sort_index().head(6).to_dict())
    if st == 3:
        g = sub.assign(c=mapped, d=ref.dt.date).groupby(["c", "d"])
        multi = g.filter(lambda x: x["price"].nunique() > 1)
        print(f"contract-days with >1 distinct settle value: {multi.groupby(['c', 'd']).ngroups}")
        if len(multi):
            print(multi[["ts_recv", "symbol", "price", "stat_flags"]].head(8).to_string())

print("\n== 4. normalised panel ==")
ohlcv = None if snap else latest("ohlcv-1d")
panel = normalize_databento_statistics(stats, KC, ohlcv=ohlcv)
print(f"rows {len(panel)}, contracts {panel['contract'].nunique()}, "
      f"{panel['trade_date'].min().date()} .. {panel['trade_date'].max().date()}")
print("missing:", panel[["settle", "volume", "open_interest"]].isna().mean().round(3).to_dict())
print("settle range:", panel["settle"].min(), "-", panel["settle"].max(), f"({SPEC.price_unit})")
td = panel["trade_date"].drop_duplicates()
odd = td[~td.map(KC.tcal.is_business_day)]
print("trade dates that the calendar says are holidays/weekends:", [d.date() for d in odd][:10])
cal_days = KC.tcal.business_days(td.min(), td.max())
print("calendar business days with no data:", [d.date() for d in cal_days.difference(td)][:15])
last = panel[panel["trade_date"] == panel["trade_date"].max()].sort_values("contract")
print("latest curve:\n", last[["trade_date", "contract", "settle", "volume", "open_interest"]].to_string(index=False))

print("\n== 5. calendar vs exchange expirations ==")
try:
    mism = validate_expirations(latest("definition"), KC)
    print("no mismatches" if mism.empty else mism.to_string())
except SystemExit as e:
    print(e)
