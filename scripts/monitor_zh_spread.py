"""Automated Daily Monitor for ICE Certified Stocks and KC Z/H Spread.

Executes each afternoon (after ~13:30 NY / 17:30 UTC):
  1. Checks and downloads today's ICE Coffee 'C' certified stock report (or most recent).
  2. Extracts certified warehouse inventory, daily change, and pending grading queue.
  3. Evaluates current KCZ26 (Dec) and KCH27 (Mar) futures prices and calendar spread.
  4. Flags when:
       - Spread <= +2.00¢ -> 🎯 [TAKE PROFIT TRIGGERED]
       - Spread >= +10.50¢ -> 🛑 [STOP LOSS TRIGGERED]
       - Otherwise -> ⏳ [HOLD POSITION]
  5. Persists daily snapshot to:
       - output/monitor/zh_spread_status.json (latest state)
       - output/monitor/zh_spread_history.csv (historical log)

Usage:
  python scripts/monitor_zh_spread.py
  python scripts/monitor_zh_spread.py --z-price 284.50 --h-price 278.00
  python scripts/monitor_zh_spread.py --download-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd
import requests

from kcquant.contracts import KC
from kcquant.fundamentals import load_cert_stocks
from fetch_cert_stocks import fetch

# Trading parameters
TARGET_TAKE_PROFIT = 2.00   # +2.00c / lb
TARGET_STOP_LOSS = 10.50    # +10.50c / lb
ENTRY_REFERENCE = 8.25      # +8.25c / lb reference entry
POINT_VALUE_USD = 375.0     # $375 per 1.00c / contract (37,500 lbs)


def download_latest_reports(stock_dir: Path, days_back: int = 5) -> list[pd.Timestamp]:
    """Attempt to download recent daily reports up to today."""
    stock_dir.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp.now().normalize()
    # Check business days in past days_back
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


def get_latest_prices(raw_file: Path | None = None, z_override: float | None = None,
                      h_override: float | None = None, prefer_live: bool = True) -> tuple[float, float, str]:
    """Get the latest KCZ26 and KCH27 prices from live market streaming, Databento settles, or cache."""
    from kcquant.quotes import get_latest_prices as fetch_active_prices
    z_px, h_px, src, _ = fetch_active_prices(raw_file, z_override, h_override, prefer_live=prefer_live)
    return z_px, h_px, src


def evaluate_monitor(z_price: float, h_price: float, cert_df: pd.DataFrame,
                     price_source: str) -> dict:
    """Evaluate current position metrics, target distance, and trigger states."""
    spread = z_price - h_price
    latest_cert_date = cert_df.index[-1]
    latest_cert = float(cert_df.loc[latest_cert_date, "certified"])
    latest_pending = float(cert_df.loc[latest_cert_date, "pending"])
    passed_today = float(cert_df.loc[latest_cert_date, "passed"])
    failed_today = float(cert_df.loc[latest_cert_date, "failed"])

    prev_cert = float(cert_df.iloc[-2]["certified"]) if len(cert_df) >= 2 else latest_cert
    prev_pending = float(cert_df.iloc[-2]["pending"]) if len(cert_df) >= 2 else latest_pending
    delta_cert = latest_cert - prev_cert
    delta_pending = latest_pending - prev_pending

    # Business days to FND
    fnd_date = KC.first_notice_day(*KC.parse("KCZ26"))
    today_ts = pd.Timestamp.now().normalize()
    bdays_to_fnd = KC.cal.bdays_between(today_ts, fnd_date)

    # Strategy Metrics
    unrealized_pts = ENTRY_REFERENCE - spread
    unrealized_usd = unrealized_pts * POINT_VALUE_USD
    dist_to_tp = spread - TARGET_TAKE_PROFIT
    dist_to_sl = TARGET_STOP_LOSS - spread

    # Trigger logic
    if spread <= TARGET_TAKE_PROFIT:
        action_flag = "TAKE_PROFIT_TRIGGERED"
        status_msg = "🎯 TAKE PROFIT TRIGGERED! Spread is <= +2.00¢. Recommendation: Close Bear Spread (Buy Z26, Sell H27)."
        exit_code = 10
    elif spread >= TARGET_STOP_LOSS:
        action_flag = "STOP_LOSS_TRIGGERED"
        status_msg = "🛑 STOP LOSS TRIGGERED! Spread is >= +10.50¢. Recommendation: Cut position immediately to prevent runaway deficit loss."
        exit_code = 20
    else:
        action_flag = "HOLD_POSITION"
        status_msg = "⏳ POSITION ACTIVE / HOLD. Spread is between take-profit (+2.00¢) and stop-loss (+10.50¢)."
        exit_code = 0

    # Pass rate calculations
    total_graded_today = passed_today + failed_today
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

    p_roll = cert_df["passed"].tail(7).sum()
    f_roll = cert_df["failed"].tail(7).sum()
    tot_roll = p_roll + f_roll
    pass_rate_7d = (p_roll / tot_roll * 100.0) if tot_roll > 0 else 0.0

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
        "action_flag": action_flag,
        "status_message": status_msg,
        "exit_code": exit_code,
        "spread_zh_cents": round(spread, 2),
        "kcz26_price": round(z_price, 2),
        "kch27_price": round(h_price, 2),
        "price_source": price_source,
        "entry_reference_cents": ENTRY_REFERENCE,
        "unrealized_pnl_cents": round(unrealized_pts, 2),
        "unrealized_pnl_usd": round(unrealized_usd, 2),
        "distance_to_take_profit_cents": round(dist_to_tp, 2),
        "distance_to_stop_loss_cents": round(dist_to_sl, 2),
        "take_profit_target_cents": TARGET_TAKE_PROFIT,
        "stop_loss_level_cents": TARGET_STOP_LOSS,
        "bdays_to_fnd": bdays_to_fnd,
        "fnd_date": fnd_date.strftime("%Y-%m-%d"),
        "certified_stocks_bags": int(latest_cert),
        "certified_stock_delta_bags": int(delta_cert),
        "pending_grading_bags": int(latest_pending),
        "pending_delta_bags": int(delta_pending),
        "passed_today_bags": int(passed_today),
        "failed_today_bags": int(failed_today),
        "total_graded_today_bags": int(total_graded_today),
        "last_grading_date": last_grading_date,
        "last_grading_passed": last_grading_p,
        "last_grading_failed": last_grading_f,
        "last_grading_rate": round(last_grading_rate, 1),
        "pass_rate_7d": round(pass_rate_7d, 1),
        "pass_rate_7d_delta": round(delta_pass_rate_7d, 1),
        "pass_rate_today": round(pass_rate_today, 1) if pass_rate_today is not None else None,
        "total_pipeline_bags": int(latest_cert + latest_pending)
    }


def print_dashboard(data: dict):
    """Print an eye-catching terminal dashboard."""
    flag = data["action_flag"]
    if flag == "TAKE_PROFIT_TRIGGERED":
        color_header = "\033[1;32m"  # Bold Green
        tag = "🎯 [TAKE PROFIT TRIGGERED]"
    elif flag == "STOP_LOSS_TRIGGERED":
        color_header = "\033[1;31m"  # Bold Red
        tag = "🛑 [STOP LOSS TRIGGERED]"
    else:
        color_header = "\033[1;34m"  # Bold Blue
        tag = "⏳ [POSITION STATUS: HOLD]"
    reset = "\033[0m"

    print("\n" + "=" * 76)
    print(f"{color_header}  KC COFFEE 'C' Z/H SPREAD & CERTIFIED STOCK MONITOR  {reset}")
    print(f"  Status: {color_header}{tag}{reset}  |  As of: {data['as_of_report_date']}")
    print("=" * 76)

    print(f"\n  📊 SPREAD & PRICING:")
    print(f"     • KCZ26 (Dec 26):        {data['kcz26_price']:.2f} ¢/lb")
    print(f"     • KCH27 (Mar 27):        {data['kch27_price']:.2f} ¢/lb")
    print(f"     • Z/H Spread:            {color_header}+{data['spread_zh_cents']:.2f} ¢/lb{reset} ({data['price_source']})")
    print(f"     • Unrealized P&L:        {'+' if data['unrealized_pnl_usd'] >= 0 else ''}${data['unrealized_pnl_usd']:,.2f} per contract ({data['unrealized_pnl_cents']:+.2f}¢)")

    print(f"\n  🎯 STRATEGY TARGETS:")
    print(f"     • Take-Profit Target:    +{data['take_profit_target_cents']:.2f} ¢/lb  (Distance: {data['distance_to_take_profit_cents']:+.2f}¢)")
    print(f"     • Stop-Loss Target:      +{data['stop_loss_level_cents']:.2f} ¢/lb (Distance: {data['distance_to_stop_loss_cents']:+.2f}¢ cushion)")
    print(f"     • First Notice Day:      {data['fnd_date']} ({data['bdays_to_fnd']} business days remaining)")

    print(f"\n  ☕ PHYSICAL CERTIFIED INVENTORY:")
    print(f"     • Certified Warehouse:   {data['certified_stocks_bags']:,} bags ({data['certified_stock_delta_bags']:+,} bags today)")
    print(f"     • Pending for Grading:   {data['pending_grading_bags']:,} bags ({data['pending_delta_bags']:+,} bags today)")
    print(f"     • Graded Today:          {data['passed_today_bags']:,} passed / {data['failed_today_bags']:,} failed")
    print(f"     • Grading Pass Rate (7d):{data['pass_rate_7d']:.1f}% ({data['pass_rate_7d_delta']:+.1f}% vs prior week)")
    print(f"     • Total Pipeline:        {data['total_pipeline_bags']:,} bags (Certified + Pending)")

    print("\n" + "-" * 76)
    print(f"  📢 ACTION: {color_header}{data['status_message']}{reset}")
    print("=" * 76 + "\n")


def persist_results(data: dict, out_dir: Path):
    """Save latest status to JSON and append to historical CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "zh_spread_status.json"
    csv_path = out_dir / "zh_spread_history.csv"

    # Save JSON
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)

    # Append to CSV
    row_df = pd.DataFrame([data])
    if csv_path.exists():
        history = pd.read_csv(csv_path)
        # Avoid duplicate row for same report date
        if not (history["as_of_report_date"] == data["as_of_report_date"]).any():
            row_df.to_csv(csv_path, mode="a", header=False, index=False)
    else:
        row_df.to_csv(csv_path, index=False)


def main():
    ap = argparse.ArgumentParser(description="Daily Monitor for ICE Certified Stocks and KC Z/H Spread")
    ap.add_argument("--z-price", type=float, help="Override KCZ26 futures settle price in cents/lb")
    ap.add_argument("--h-price", type=float, help="Override KCH27 futures settle price in cents/lb")
    ap.add_argument("--settle-only", action="store_true", help="Use official daily settle instead of live intraday market quote")
    ap.add_argument("--no-download", action="store_true", help="Skip checking for new online ICE reports")
    ap.add_argument("--download-only", action="store_true", help="Only download reports and exit")
    ap.add_argument("--notify", action="store_true", help="Send native macOS desktop notification on trigger")
    args = ap.parse_args()

    stock_dir = ROOT / "data" / "fundamentals" / "ice_cert_stock"
    raw_curve_file = ROOT / "output" / "KC_databento_fixed_raw_curve.csv"
    monitor_dir = ROOT / "output" / "monitor"

    if not args.no_download:
        print("[*] Checking for new ICE certified stock reports...")
        new_files = download_latest_reports(stock_dir, days_back=7)
        if new_files:
            print(f"    Downloaded {len(new_files)} new report(s): {', '.join(d.strftime('%Y-%m-%d') for d in new_files)}")
        else:
            print("    Local ICE report archive is up to date.")

    if args.download_only:
        sys.exit(0)

    # Load certified stock DataFrame
    cert_df = load_cert_stocks(ROOT / "data" / "fundamentals")

    # Get prices
    z_px, h_px, source = get_latest_prices(raw_curve_file, args.z_price, args.h_price, prefer_live=not args.settle_only)

    # Evaluate
    result = evaluate_monitor(z_px, h_px, cert_df, source)

    # Print Dashboard
    print_dashboard(result)

    # Persist
    persist_results(result, monitor_dir)

    # Optional desktop notification on alert trigger
    if args.notify and result["exit_code"] in (10, 20):
        try:
            import subprocess
            title = "KC Z/H Spread ALERT"
            msg = result["status_message"].replace('"', '\\"')
            subprocess.run([
                "osascript", "-e",
                f'display notification "{msg}" with title "{title}"'
            ], check=False)
        except Exception:
            pass

    sys.exit(result["exit_code"])


if __name__ == "__main__":
    main()
