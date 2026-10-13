"""Hourly Telegram notification bot for ICE Coffee 'C' Z/H Spread and Certified Stocks.

Monitors KCZ26 - KCH27 spread and warehouse stock metrics while the market is open:
  - Market trading hours: 04:15 - 13:30 New York Time (ET), Monday - Friday.
  - Dispatches hourly updates with current spread, P&L, inventory, and distance to targets.
  - Sends immediate high-priority alerts if Take-Profit (+2.00¢) or Stop-Loss (+10.50¢) is hit.

Usage:
  python scripts/telegram_zh_bot.py --test            # Verify Telegram bot credentials
  python scripts/telegram_zh_bot.py --force           # Send single update now regardless of hours
  python scripts/telegram_zh_bot.py                   # Run single check (sends only if market is open)
  python scripts/telegram_zh_bot.py --daemon          # Run persistent hourly loop during market hours
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd

from kcquant.contracts import KC
from kcquant.fundamentals import load_cert_stocks
from kcquant.notify import load_env, send_telegram_message
from monitor_zh_spread import (
    download_latest_reports,
    evaluate_monitor,
    get_latest_prices,
    persist_results,
    TARGET_STOP_LOSS,
    TARGET_TAKE_PROFIT
)

NY_TZ = ZoneInfo("America/New_York")


def is_market_open(dt_ny: datetime | None = None) -> tuple[bool, str]:
    """Check if ICE Futures US Coffee 'C' market is in active electronic trading hours.

    Regular trading hours: Monday - Friday, 04:15 to 13:30 New York Time (ET).
    """
    now = dt_ny or datetime.now(NY_TZ)
    weekday = now.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday

    if weekday >= 5:
        return False, f"Market is closed on weekends (Current NY time: {now:%A %H:%M ET})"

    market_open = now.replace(hour=4, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=13, minute=30, second=0, microsecond=0)

    if now < market_open:
        return False, f"Pre-market (Opens at 04:15 ET; Current NY time: {now:%H:%M ET})"
    elif now > market_close:
        return False, f"Post-close (Closed at 13:30 ET; Current NY time: {now:%H:%M ET})"

    return True, f"Market is OPEN (04:15 - 13:30 ET; Current: {now:%H:%M ET})"


def format_telegram_html(data: dict, is_forced: bool = False) -> str:
    """Format hourly status as a clean Telegram HTML message."""
    flag = data["action_flag"]
    now_ny = datetime.now(NY_TZ)
    time_str = now_ny.strftime("%b %d, %Y · %H:%M ET")

    # Header & Banner based on trigger
    if flag == "TAKE_PROFIT_TRIGGERED":
        banner = (
            "🎯 <b>ALERT: TAKE PROFIT TRIGGERED!</b> 🎯\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Spread dropped to <b>+{data['spread_zh_cents']:.2f}¢</b> (below +2.00¢ target)!\n"
            f"💰 <b>Unrealized Gain:</b> +${data['unrealized_pnl_usd']:,.2f}/ct ({data['unrealized_pnl_cents']:+.2f}¢)\n"
            f"📢 <b>Action:</b> Close Bear Spread (Buy Z26 / Sell H27)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
    elif flag == "STOP_LOSS_TRIGGERED":
        banner = (
            "🛑 <b>ALERT: STOP LOSS TRIGGERED!</b> 🛑\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Spread widened to <b>+{data['spread_zh_cents']:.2f}¢</b> (above +10.50¢ stop)!\n"
            f"⚠️ <b>Loss:</b> ${data['unrealized_pnl_usd']:,.2f}/ct ({data['unrealized_pnl_cents']:+.2f}¢)\n"
            f"📢 <b>Action:</b> Invalidation reached. Cut position immediately.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
    else:
        banner = (
            "☕ <b>KC Z/H SPREAD 30-MIN UPDATE</b>\n"
            f"<i>{time_str}</i> {'(Ad-hoc)' if is_forced else ''}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )

    # Core message body
    body = (
        f"📊 <b>Spread & Futures Prices:</b>\n"
        f"• KCZ26 (Dec 26): <code>{data['kcz26_price']:.2f}¢</code>\n"
        f"• KCH27 (Mar 27): <code>{data['kch27_price']:.2f}¢</code>\n"
        f"• <b>Z/H Calendar Spread:</b> <code>+{data['spread_zh_cents']:.2f}¢</code>\n"
        f"• <b>Source:</b> <i>{data.get('price_source', 'Live Quote')}</i>\n"
        f"• <b>Unrealized P&L:</b> <code>{'+' if data['unrealized_pnl_usd'] >= 0 else ''}${data['unrealized_pnl_usd']:,.2f}</code> per contract\n\n"
        f"🎯 <b>Strategy Milestones:</b>\n"
        f"• Take-Profit (+2.00¢): <code>{data['distance_to_take_profit_cents']:+.2f}¢ to target</code>\n"
        f"• Stop-Loss (+10.50¢): <code>{data['distance_to_stop_loss_cents']:+.2f}¢ cushion</code>\n"
        f"• First Notice Day: <b>{data['fnd_date']}</b> (<code>{data['bdays_to_fnd']} bdays remaining</code>)\n\n"
        f"📦 <b>Physical Certified Inventory:</b>\n"
        f"• Certified Stocks: <code>{data['certified_stocks_bags']:,} bags</code> ({data['certified_stock_delta_bags']:+,})\n"
        f"• Pending for Grading: <code>{data['pending_grading_bags']:,} bags</code> ({data['pending_delta_bags']:+,})\n"
        f"• Grading Pass Rate (7d): <code>{data.get('pass_rate_7d', 0.0):.1f}%</code> (<code>{data.get('pass_rate_7d_delta', 0.0):+.1f}%</code>)\n"
        f"• Total Pipeline: <code>{data['total_pipeline_bags']:,} bags</code>\n"
        f"• As-of Report: <i>{data['as_of_report_date']}</i>\n"
    )

    footer = f"\n📢 <b>Status:</b> <code>{flag}</code>"
    return banner + body + footer


def run_telegram_update(force: bool = False, z_price: float | None = None,
                        h_price: float | None = None, no_download: bool = False,
                        prefer_live: bool = True) -> tuple[bool, str]:
    """Execute a single monitor check and dispatch to Telegram if market is open or force=True."""
    now_ny = datetime.now(NY_TZ)
    is_open, reason = is_market_open(now_ny)

    if not is_open and not force:
        return False, f"Skipping Telegram update: {reason} (Use --force to send anyway)"

    stock_dir = ROOT / "data" / "fundamentals" / "ice_cert_stock"
    raw_curve_file = ROOT / "output" / "KC_databento_fixed_raw_curve.csv"
    monitor_dir = ROOT / "output" / "monitor"

    if not no_download:
        download_latest_reports(stock_dir, days_back=5)

    cert_df = load_cert_stocks(ROOT / "data" / "fundamentals")
    z_px, h_px, source = get_latest_prices(raw_curve_file, z_price, h_price, prefer_live=prefer_live)
    result = evaluate_monitor(z_px, h_px, cert_df, source)
    persist_results(result, monitor_dir)

    msg_html = format_telegram_html(result, is_forced=force and not is_open)
    success, res_str = send_telegram_message(msg_html)

    # Log to jsonl
    log_file = monitor_dir / "telegram_log.jsonl"
    with open(log_file, "a") as f:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "ny_time": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "market_open": is_open,
            "telegram_sent": success,
            "telegram_result": res_str,
            "spread_zh_cents": result["spread_zh_cents"],
            "action_flag": result["action_flag"]
        }
        f.write(json.dumps(log_entry) + "\n")

    return success, res_str


def main():
    load_env()
    ap = argparse.ArgumentParser(description="30-Minute Telegram Bot for KC Z/H Spread")
    ap.add_argument("--test", action="store_true", help="Send a verification test message to Telegram and exit")
    ap.add_argument("--force", action="store_true", help="Force send message even if market is closed")
    ap.add_argument("--daemon", action="store_true", help="Run continuously in background, checking every 30 minutes")
    ap.add_argument("--interval", type=int, default=1800, help="Check interval in seconds for daemon (default: 1800 [30 mins])")
    ap.add_argument("--z-price", type=float, help="Override KCZ26 price in cents/lb")
    ap.add_argument("--h-price", type=float, help="Override KCH27 price in cents/lb")
    ap.add_argument("--settle-only", action="store_true", help="Use official daily settle instead of live intraday quote")
    ap.add_argument("--no-download", action="store_true", help="Skip ICE report download")
    args = ap.parse_args()

    if args.test:
        print("[*] Testing Telegram Bot configuration...")
        test_msg = (
            "🤖 <b>KCquant Telegram Bot Initialized!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Hourly market-open updates and threshold alerts are successfully connected.\n"
            f"📅 Time: {datetime.now(NY_TZ):%Y-%m-%d %H:%M:%S ET}\n"
            "☕ Tracking: <b>KCZ26 / KCH27 Calendar Spread</b>"
        )
        ok, res = send_telegram_message(test_msg)
        if ok:
            print("✅ Success! Test message delivered to Telegram.")
            sys.exit(0)
        else:
            print(f"❌ Failed to send Telegram message: {res}")
            print("\nPlease ensure your credentials in .env are set:")
            print("  TELEGRAM_BOT_TOKEN=your_bot_token")
            print("  TELEGRAM_CHAT_ID=your_chat_id")
            sys.exit(1)

    if args.daemon:
        print(f"[*] Starting persistent Telegram bot daemon (Check interval: {args.interval}s)...")
        print("    Press Ctrl+C to stop.")
        while True:
            try:
                now_ny = datetime.now(NY_TZ)
                is_open, reason = is_market_open(now_ny)
                if is_open or args.force:
                    print(f"[{now_ny:%H:%M:%S ET}] Market is open. Running 30-min update...")
                    ok, res = run_telegram_update(
                        force=args.force, z_price=args.z_price,
                        h_price=args.h_price, no_download=args.no_download,
                        prefer_live=not args.settle_only
                    )
                    print(f"    Result: {'Sent' if ok else 'Failed'} ({res})")
                else:
                    print(f"[{now_ny:%H:%M:%S ET}] {reason}. Sleeping...")
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n[*] Stopping Telegram bot daemon.")
                break
            except Exception as e:
                print(f"[!] Error in loop: {e}. Retrying in 60s...")
                time.sleep(60)
        sys.exit(0)

    # Single-run execution
    ok, res = run_telegram_update(
        force=args.force, z_price=args.z_price,
        h_price=args.h_price, no_download=args.no_download,
        prefer_live=not args.settle_only
    )
    print(f"[*] Status: {'Delivered' if ok else 'Notice'} - {res}")


if __name__ == "__main__":
    main()
