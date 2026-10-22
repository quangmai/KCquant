"""KC Coffee 'C' Market & Physical Inventory Monitor.

Tracks:
  1. KC Outright Futures Prices: KCZ26 (Dec 26), KCH27 (Mar 27), KCK27 (May 27).
  2. Calendar Spreads:
       - Z/H Spread: KCZ26 - KCH27
       - H/K Spread: KCH27 - KCK27
  3. ICE Physical Certified Warehouse Inventory:
       - Certified Warehouse Stocks (and daily change)
       - Pending Grading Queue (and daily change)
       - Today's Grading Activity (passed / failed, day's pass rate)
       - 7-Day Rolling Grading Pass Rate (and week-over-week change)
       - Total Pipeline (Certified + Pending)

Usage:
  python scripts/monitor_kc.py
  python scripts/monitor_kc.py --settle-only
  python scripts/monitor_kc.py --z-price 305.0 --h-price 292.0 --k-price 287.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd
import requests

from kcquant.contracts import KC
from kcquant.fundamentals import load_cert_stocks
from kcquant.quotes import get_latest_kc_curve_prices
from fetch_cert_stocks import fetch


def download_latest_reports(stock_dir: Path, days_back: int = 5) -> list[pd.Timestamp]:
    """Attempt to download recent daily reports up to today."""
    stock_dir.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp.now().normalize()
    recent_bdays = pd.date_range(end=today, periods=days_back, freq=KC.cal.bday)
    session = requests.Session()
    downloaded = []

    for day in recent_bdays:
        xls_file = stock_dir / f"coffee_cert_stock_{day:%Y%m%d}.xls"
        if not xls_file.exists():
            day_res, status, size = fetch(day, stock_dir, session, retries=2, verbose=False)
            if status == "ok":
                downloaded.append(day)
    return downloaded


def evaluate_market_data(prices: dict, cert_df: pd.DataFrame) -> dict:
    """Combine futures curve prices, calendar spreads, and certified inventory pipeline."""
    latest_cert_date = cert_df.index[-1]
    latest_cert = float(cert_df.loc[latest_cert_date, "certified"])
    latest_pending = float(cert_df.loc[latest_cert_date, "pending"])
    passed_today = float(cert_df.loc[latest_cert_date, "passed"])
    failed_today = float(cert_df.loc[latest_cert_date, "failed"])
    total_graded_today = passed_today + failed_today

    prev_cert = float(cert_df.iloc[-2]["certified"]) if len(cert_df) >= 2 else latest_cert
    prev_pending = float(cert_df.iloc[-2]["pending"]) if len(cert_df) >= 2 else latest_pending
    delta_cert = latest_cert - prev_cert
    delta_pending = latest_pending - prev_pending

    # Day's pass rate
    pass_rate_today = (passed_today / total_graded_today * 100.0) if total_graded_today > 0 else None

    # Last active grading session
    active_graded = cert_df[(cert_df["passed"] + cert_df["failed"]) > 0]
    if len(active_graded) > 0:
        last_active_row = active_graded.iloc[-1]
        last_grading_date = active_graded.index[-1].strftime("%b %d")
        last_grading_p = int(last_active_row["passed"])
        last_grading_f = int(last_active_row["failed"])
        last_grading_tot = last_grading_p + last_grading_f
        last_grading_rate = (last_grading_p / last_grading_tot * 100.0) if last_grading_tot > 0 else 0.0
    else:
        last_grading_date = "N/A"
        last_grading_p = 0
        last_grading_f = 0
        last_grading_rate = 0.0

    # 7-day rolling pass rate
    p_roll = cert_df["passed"].tail(7).sum()
    f_roll = cert_df["failed"].tail(7).sum()
    tot_roll = p_roll + f_roll
    pass_rate_7d = (p_roll / tot_roll * 100.0) if tot_roll > 0 else 0.0

    # 7-day change vs previous week
    if len(cert_df) >= 14:
        p_prior = cert_df["passed"].iloc[-14:-7].sum()
        f_prior = cert_df["failed"].iloc[-14:-7].sum()
        tot_prior = p_prior + f_prior
        pass_rate_prior_7d = (p_prior / tot_prior * 100.0) if tot_prior > 0 else pass_rate_7d
        delta_pass_rate_7d = pass_rate_7d - pass_rate_prior_7d
    else:
        delta_pass_rate_7d = 0.0

    return {
        "timestamp": datetime.now().isoformat(),
        "as_of_report_date": latest_cert_date.strftime("%Y-%m-%d"),
        "kcz26_price": prices["kcz26"],
        "kch27_price": prices["kch27"],
        "kck27_price": prices["kck27"],
        "spread_zh_cents": prices["spread_zh"],
        "spread_hk_cents": prices["spread_hk"],
        "price_source": prices["price_source"],
        "quote_time_ny": prices.get("quote_time_ny", "N/A"),
        "certified_stocks_bags": int(latest_cert),
        "certified_stock_delta_bags": int(delta_cert),
        "pending_grading_bags": int(latest_pending),
        "pending_delta_bags": int(delta_pending),
        "passed_today_bags": int(passed_today),
        "failed_today_bags": int(failed_today),
        "total_graded_today_bags": int(total_graded_today),
        "pass_rate_today": round(pass_rate_today, 1) if pass_rate_today is not None else None,
        "pass_rate_7d": round(pass_rate_7d, 1),
        "pass_rate_7d_delta": round(delta_pass_rate_7d, 1),
        "last_grading_date": last_grading_date,
        "last_grading_passed": last_grading_p,
        "last_grading_failed": last_grading_f,
        "last_grading_rate": round(last_grading_rate, 1),
        "total_pipeline_bags": int(latest_cert + latest_pending)
    }


def print_dashboard(data: dict):
    """Print a clean, professional market intelligence dashboard."""
    header_color = "\033[1;36m"  # Cyan
    zh_color = "\033[1;32m" if data["spread_zh_cents"] >= 0 else "\033[1;31m"
    hk_color = "\033[1;32m" if data["spread_hk_cents"] >= 0 else "\033[1;31m"
    reset = "\033[0m"

    print("\n" + "=" * 74)
    print(f"{header_color}  KC Monitor  {reset}")
    print("=" * 74)

    print(f"\n  📊 KC OUTRIGHT FUTURES PRICES:")
    print(f"     • KCZ26 (Dec 26):        {data['kcz26_price']:.2f} ¢/lb")
    print(f"     • KCH27 (Mar 27):        {data['kch27_price']:.2f} ¢/lb")
    print(f"     • KCK27 (May 27):        {data['kck27_price']:.2f} ¢/lb")

    print(f"\n  📈 CALENDAR SPREADS:")
    print(f"     • Z26 / H27 Spread:      {zh_color}{data['spread_zh_cents']:+.2f} ¢/lb{reset}")
    print(f"     • H27 / K27 Spread:      {hk_color}{data['spread_hk_cents']:+.2f} ¢/lb{reset}")

    print(f"\n  ☕ PHYSICAL CERTIFIED INVENTORY:")
    print(f"     • Certified Warehouse:   {data['certified_stocks_bags']:,} bags ({data['certified_stock_delta_bags']:+,} bags)")
    print(f"     • Pending for Grading:   {data['pending_grading_bags']:,} bags ({data['pending_delta_bags']:+,} bags)")

    if data["total_graded_today_bags"] > 0:
        print(f"     • Graded Today:          {data['total_graded_today_bags']:,} bags ({data['passed_today_bags']:,} passed / {data['failed_today_bags']:,} failed | {data['pass_rate_today']:.1f}% pass)")
    else:
        print(f"     • Graded Today:          0 bags (Last session {data['last_grading_date']}: {data['last_grading_rate']:.0f}% pass · {data['last_grading_failed']:,} failed)")

    print(f"     • Grading Pass Rate (7d):{data['pass_rate_7d']:.1f}% ({data['pass_rate_7d_delta']:+.1f}% vs prior week)")
    print(f"     • Total Pipeline:        {data['total_pipeline_bags']:,} bags (Certified + Pending)")
    print("=" * 74 + "\n")


def persist_results(data: dict, out_dir: Path):
    """Save latest status to JSON and append to historical CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "kc_market_status.json"
    csv_path = out_dir / "kc_market_history.csv"

    # Save JSON
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)

    # Append to CSV
    row_df = pd.DataFrame([data])
    if csv_path.exists():
        history = pd.read_csv(csv_path)
        if not (history["as_of_report_date"] == data["as_of_report_date"]).any():
            row_df.to_csv(csv_path, mode="a", header=False, index=False)
    else:
        row_df.to_csv(csv_path, index=False)


def main():
    ap = argparse.ArgumentParser(description="KC Coffee 'C' Market & Physical Inventory Monitor")
    ap.add_argument("--z-price", type=float, help="Override KCZ26 futures price in cents/lb")
    ap.add_argument("--h-price", type=float, help="Override KCH27 futures price in cents/lb")
    ap.add_argument("--k-price", type=float, help="Override KCK27 futures price in cents/lb")
    ap.add_argument("--settle-only", action="store_true", help="Use official daily settles instead of live intraday quote")
    ap.add_argument("--no-download", action="store_true", help="Skip checking for new online ICE reports")
    args = ap.parse_args()

    stock_dir = ROOT / "data" / "fundamentals" / "ice_cert_stock"
    raw_curve_file = ROOT / "output" / "KC_databento_fixed_raw_curve.csv"
    monitor_dir = ROOT / "output" / "monitor"

    if not args.no_download:
        download_latest_reports(stock_dir, days_back=7)

    cert_df = load_cert_stocks(ROOT / "data" / "fundamentals")

    prices = get_latest_kc_curve_prices(
        raw_file=raw_curve_file,
        z_override=args.z_price,
        h_override=args.h_price,
        k_override=args.k_price,
        prefer_live=not args.settle_only
    )

    data = evaluate_market_data(prices, cert_df)
    print_dashboard(data)
    persist_results(data, monitor_dir)


if __name__ == "__main__":
    main()
