#!/usr/bin/env python3
"""Find a root's Databento symbols and when its end-of-day statistics snapshot runs.

Step 1 (free): resolve candidate raw-symbol formats for the nearest contracts.
Step 2 (paid, asks first): download the FULL statistics feed for 2 days per year
(one winter, one summer, to catch daylight-saving effects) for the 3 nearest
contracts, then report - in exchange local time - when the official settle and
the OI publication arrive, and which seconds contain a settle+OI+volume
snapshot for all contracts.

    python scripts/probe_snapshot.py --root RC
    python scripts/probe_snapshot.py --root KC --yes
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import databento as db  # noqa: E402
import pandas as pd  # noqa: E402

from run_pipeline import load_dotenv  # noqa: E402
from kcquant.contracts import MONTH_TO_CODE, SPECS  # noqa: E402

STAT = {3: "settle", 9: "oi", 6: "volume", 1: "open", 4: "low", 5: "high", 13: "vwap", 2: "indic_open"}

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="KC", choices=list(SPECS))
ap.add_argument("--years", default="2019-2026")
ap.add_argument("--max-cost", type=float, default=5.0)
ap.add_argument("--yes", action="store_true")
a = ap.parse_args()
load_dotenv(ROOT / ".env")
spec = SPECS[a.root]
tz = spec.timezone
client = db.Historical()
out_dir = ROOT / "data" / "probe"
out_dir.mkdir(parents=True, exist_ok=True)

rng = client.metadata.get_dataset_range(dataset=spec.databento_dataset)
avail_lo = pd.Timestamp(rng["start"]).tz_convert(None)
avail_end = pd.Timestamp(rng["end"]).tz_convert(None)
print(f"{spec.databento_dataset}: {avail_lo.date()} .. {avail_end.date()}")


def nearest(d, n=3):
    meta = spec.contract_table(spec.contracts_between(d, d + pd.DateOffset(months=14)))
    return meta[meta["first_notice_day"] > d].head(n)


# ---- step 1: which symbol format does Databento use for this root? (free) ----
FORMATS = {
    "padded4 + FM + M00YY + !": lambda r, y, m: f"{r:<4}FM{MONTH_TO_CODE[m]}00{y % 100:02d}!",
    "root + space + FM...":     lambda r, y, m: f"{r} FM{MONTH_TO_CODE[m]}00{y % 100:02d}!",
    "padded4 + FM + MYY + !":   lambda r, y, m: f"{r:<4}FM{MONTH_TO_CODE[m]}{y % 100:02d}!",
}
ref_day = spec.tcal.rollback(avail_end - pd.Timedelta(days=7))
probe_contracts = nearest(ref_day)
print(f"\nresolving nearest contracts on {ref_day.date()}: {list(probe_contracts.index)}")
fmt_ok = None
for name, f in FORMATS.items():
    syms = [f(spec.exchange_root, r.year, r.month) for r in probe_contracts.itertuples()]
    res = client.symbology.resolve(dataset=spec.databento_dataset, symbols=syms, stype_in="raw_symbol",
                                   stype_out="instrument_id", start_date=ref_day.strftime("%Y-%m-%d"),
                                   end_date=(ref_day + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    found = [s for s in syms if res.get("result", {}).get(s)]
    print(f"  {name:28s} {syms[0]!r:20s} -> {len(found)}/{len(syms)} found")
    if len(found) == len(syms) and fmt_ok is None:
        fmt_ok = (name, f)
if fmt_ok is None:
    try:
        res = client.symbology.resolve(dataset=spec.databento_dataset, symbols=[f"{spec.exchange_root:<4}"],
                                       stype_in="parent", stype_out="instrument_id",
                                       start_date=ref_day.strftime("%Y-%m-%d"),
                                       end_date=(ref_day + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        n = sum(len(v) for v in res.get("result", {}).values())
        print(f"  parent {spec.exchange_root!r}: {n} instrument mappings; not_found={res.get('not_found')}")
    except Exception as err:
        print("  parent lookup failed:", str(err).splitlines()[0])
    sys.exit(f"\nNo raw-symbol format resolved for {spec.root} in {spec.databento_dataset}. "
             "Paste this output back - nothing was charged.")
print(f"-> using format: {fmt_ok[0]}")
if fmt_ok[0] != "padded4 + FM + M00YY + !":
    print("   NOTE: differs from ContractSpec.databento_raw_symbol - the loader needs updating.")
make_sym = fmt_ok[1]

# ---- step 2: snapshot timing over the years (paid) ----
y0, y1 = map(int, a.years.split("-"))


def probe_day(d: pd.Timestamp) -> pd.Timestamp:
    d = d + pd.Timedelta(days=(2 - d.weekday()) % 7)        # a Wednesday
    while not spec.tcal.is_business_day(d):
        d += pd.Timedelta(days=7)
    return d


days = []
for y in range(y0, y1 + 1):
    for md in ["01-15", "07-15"]:
        d = probe_day(pd.Timestamp(f"{y}-{md}"))
        if avail_lo + pd.Timedelta(days=1) <= d and d + pd.Timedelta(days=1) <= avail_end:
            days.append(d)
reqs = {d: dict(dataset=spec.databento_dataset, schema="statistics", stype_in="raw_symbol",
                symbols=[make_sym(spec.exchange_root, r.year, r.month) for r in nearest(d).itertuples()],
                start=d.strftime("%Y-%m-%d"), end=(d + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        for d in days}
path = lambda d: out_dir / f"probe_{spec.root}_{d:%Y%m%d}.dbn.zst"  # noqa: E731
todo = [d for d in days if not path(d).exists()]
cost = sum(client.metadata.get_cost(**reqs[d]) for d in todo)
print(f"\n{len(days)} probe days, {len(todo)} to download, cost ${cost:.2f}")
if cost > a.max_cost:
    sys.exit(f"above --max-cost ${a.max_cost}")
if todo and not a.yes and input("proceed? [y/N] ").strip().lower() != "y":
    sys.exit("aborted")
for d in todo:
    client.timeseries.get_range(path=str(path(d)), **reqs[d])

print(f"\nTimes are {tz} local. 'snapshots' = seconds holding settle+oi+volume for all probed contracts.")
print(f"{'date':10} {'tz':4} {'records':>8}  {'official settle':16} {'OI publ.':9} snapshots [records: stat types]")
seen = {}
for d in days:
    df = db.DBNStore.from_file(str(path(d))).to_df().reset_index()
    if df.empty:
        print(f"{d.date()} no data")
        continue
    df["loc"] = df["ts_recv"].dt.tz_convert(tz)
    df["sec"] = df["loc"].dt.floor("s")
    df["st"] = df["stat_type"].map(lambda x: STAT.get(int(x), str(int(x))))
    nsym = df["symbol"].nunique()
    fl = df[(df["st"] == "settle") & (df["stat_flags"].astype(int) & 1 > 0)]
    oi_ref = df[(df["st"] == "oi") & df["ts_ref"].notna()] if "ts_ref" in df else df.iloc[:0]
    snaps = []
    for sec, x in df.groupby("sec"):
        if {"settle", "oi", "volume"} <= set(x["st"]) and x[x["st"] == "settle"]["symbol"].nunique() >= nsym:
            snaps.append(f"{sec:%H:%M:%S}[{len(x)}: {','.join(sorted(set(x['st'])))}]")
            seen[f"{sec:%H:%M}"] = seen.get(f"{sec:%H:%M}", 0) + 1
    print(f"{d.date()} {df['loc'].iloc[-1].tzname():4} {len(df):8,d}  "
          f"{','.join(sorted(set(fl['loc'].dt.strftime('%H:%M')))) or '-':16} "
          f"{','.join(sorted(set(oi_ref['loc'].dt.strftime('%H:%M')))) or '-':9} "
          f"{' '.join(snaps) or 'NONE'}")
print("\nsnapshot minute -> number of probe days:", dict(sorted(seen.items())))
print("records per hour (all probe days), to judge window cost:")
allx = pd.concat([db.DBNStore.from_file(str(path(d))).to_df().reset_index() for d in days], ignore_index=True)
print(allx["ts_recv"].dt.tz_convert(tz).dt.hour.value_counts().sort_index().to_dict())
