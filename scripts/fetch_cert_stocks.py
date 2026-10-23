#!/usr/bin/env python3
"""Download ICE Coffee "C" certified stock reports (one .xls per trading day).

URL pattern: https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_cert_stock_YYYYMMDD.xls
Files go to data/fundamentals/ice_cert_stock/. Already-downloaded days are skipped, so the
script can be re-run any time (e.g. daily) to add new reports.

    python scripts/fetch_cert_stocks.py                 # last 3 years up to today
    python scripts/fetch_cert_stocks.py --years 5
    python scripts/fetch_cert_stocks.py --start 2019-01-01 --end 2019-12-31
"""
import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from kcquant.contracts import KC  # noqa: E402

URL = "https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_cert_stock_{d:%Y%m%d}.xls"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
OLE2_MAGIC = b"\xd0\xcf\x11\xe0"   # legacy .xls signature


def fetch(day: pd.Timestamp, out: Path, session: requests.Session, retries: int = 3,
          timeout: float = 15.0, verbose: bool = True, blocks: int = 0):
    f = out / f"coffee_cert_stock_{day:%Y%m%d}.xls"
    if f.exists() and f.stat().st_size > 0:
        return day, "cached", f.stat().st_size
    url = URL.format(d=day)
    for attempt in range(retries):
        t0 = time.time()
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=False)
        except requests.RequestException as err:
            if verbose:
                print(f"    {day.date()}  {type(err).__name__} after {time.time() - t0:.0f}s", flush=True)
            if attempt == retries - 1:
                return day, f"error {type(err).__name__}", 0
            time.sleep(5)
            continue
        if verbose and r.status_code != 200:
            loc = r.headers.get("Location", "")
            print(f"    {day.date()}  HTTP {r.status_code} in {time.time() - t0:.1f}s {loc[:80]}", flush=True)
        if r.status_code in (301, 302, 303, 307, 308):
            return day, f"redirect {r.status_code}", 0
        if r.status_code == 200:
            body = r.content
            # ICE sometimes answers 200 with an HTML "not found" page: keep only real files
            if body[:4] == OLE2_MAGIC or body[:2] == b"PK" or b"<table" in body[:2000].lower():
                f.write_bytes(body)
                return day, "ok", len(body)
            return day, "not a spreadsheet", len(body)
        if r.status_code in (404, 403):
            return day, f"http {r.status_code}", 0
        if r.status_code == 429:
            # rate limited. ICE sends Retry-After (seconds until the block lifts): sleep exactly
            # that long and try the same day again - this doesn't use up a retry, so an
            # unattended run simply pauses through each block and continues.
            ra = r.headers.get("Retry-After", "")
            if ra.isdigit() and int(ra) <= 4 * 3600 and blocks < 20:
                resume = pd.Timestamp.now() + pd.Timedelta(seconds=int(ra) + 5)
                print(f"    rate limited at {day.date()}; ICE says wait {int(ra) // 60} min - "
                      f"sleeping until {resume:%H:%M} (Ctrl+C is safe; re-run resumes)", flush=True)
                time.sleep(int(ra) + 5)
                return fetch(day, out, session, retries, timeout, verbose, blocks + 1)
            wait = int(ra) if ra.isdigit() else 120 * (attempt + 1)
            if attempt < retries - 1:
                if verbose:
                    print(f"    rate limited (429); waiting {wait}s before retrying", flush=True)
                time.sleep(wait)
                continue
            return day, "http 429", 0
        if r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        return day, f"http {r.status_code}", 0
    return day, "gave up", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--probe", action="store_true",
                    help="only test a few dates across the period and report what the server returns")
    ap.add_argument("--stop-after", type=int, default=25,
                    help="stop after this many consecutive days without a file (server has no older data)")
    a = ap.parse_args()

    end = pd.Timestamp(a.end).normalize()
    start = pd.Timestamp(a.start) if a.start else end - pd.DateOffset(days=int(a.years * 365.25))
    # trading days on the ICE US calendar; holidays are skipped up front
    days = list(KC.tcal.business_days(start, end))[::-1]          # newest first
    out = ROOT / "data" / "fundamentals" / "ice_cert_stock"
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(days)} trading days {start.date()} .. {end.date()} -> {out}")

    if a.probe:      # one request per sample date, sequential, with full detail
        sample = [pd.Timestamp(x) for x in ["2026-07-15", "2026-01-14", "2025-05-01", "2024-06-04", "2023-10-10"]]
        with requests.Session() as s:
            for d in sample:
                t0 = time.time()
                try:
                    r = s.get(URL.format(d=d), headers=HEADERS, timeout=20, allow_redirects=False)
                    print(f"{d.date()}  HTTP {r.status_code}  {len(r.content):7d} bytes  {time.time() - t0:5.1f}s  "
                          f"type={r.headers.get('Content-Type', '')[:40]}  starts={r.content[:8]!r}  "
                          f"retry-after={r.headers.get('Retry-After', '-')}")
                except requests.RequestException as err:
                    print(f"{d.date()}  {type(err).__name__} after {time.time() - t0:.0f}s")
                time.sleep(2)
        return

    results, misses, limited = [], 0, 0
    with requests.Session() as s:
        for i, d in enumerate(days, 1):
            res = fetch(d, out, s)
            results.append(res)
            misses = 0 if res[1] in ("ok", "cached") else misses + 1
            limited = limited + 1 if res[1] == "http 429" else 0
            if limited >= 3:
                print(f"  still rate limited after backing off (at {d.date()}). Stopping - wait an hour or "
                      "two and re-run; downloaded files are kept and skipped next time.")
                break
            if res[1] != "cached":
                time.sleep(a.delay)
            if i % 25 == 0 or i == len(days):
                n_ok = sum(r[1] in ("ok", "cached") for r in results)
                print(f"  {i}/{len(days)} checked, {n_ok} files (newest-first; now at {d.date()})", flush=True)
            if misses >= a.stop_after:
                print(f"  {misses} consecutive days without a file - stopping at {d.date()}. "
                      "The server probably keeps no older reports at this address.")
                break

    results.sort()
    with open(out / "_download_log.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "status", "bytes"])
        for d, st, n in results:
            w.writerow([d.date(), st, n])
    by = pd.Series([r[1] for r in results]).value_counts()
    print("\nsummary:", by.to_dict())
    missing = [r[0].date() for r in results if r[1] not in ("ok", "cached")]
    if missing:
        print(f"{len(missing)} days without a file (holidays ICE didn't publish, or not on the server), "
              f"e.g. {[str(x) for x in missing[:8]]}")
    print("log:", out / "_download_log.csv")


if __name__ == "__main__":
    main()
