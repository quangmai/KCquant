"""Telegram Notification Bot for KC Coffee 'C' Market, Spreads, and Physical Inventory.

Tracks:
  - Outright Prices: KCZ26 (Dec 26), KCH27 (Mar 27), KCK27 (May 27)
  - Consecutive Calendar Spreads:
      • Z/H Spread: KCZ26 - KCH27
      • H/K Spread: KCH27 - KCK27
  - Physical Certified Inventory Pipeline:
      • Certified Warehouse Stocks & daily delta
      • Pending Grading Queue & daily delta
      • Graded Today (passed / failed, pass rate)
      • 7-Day Rolling Grading Pass Rate & weekly delta
      • Total Pipeline (Certified + Pending)

Usage:
  python scripts/telegram_kc_bot.py --test            # Verify Telegram bot credentials
  python scripts/telegram_kc_bot.py --force           # Send single update now regardless of hours
  python scripts/telegram_kc_bot.py                   # Run single check (sends if market is open)
  python scripts/telegram_kc_bot.py --daemon          # Run persistent loop (every 30 mins)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_RUNNING = True

def _handle_exit_signal(signum, frame):
    global _RUNNING
    print(f"\n[*] Received exit signal ({signum}). Stopping gracefully...")
    _RUNNING = False

try:
    signal.signal(signal.SIGTERM, _handle_exit_signal)
    signal.signal(signal.SIGINT, _handle_exit_signal)
except Exception:
    pass


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from kcquant.fundamentals import load_cert_stocks
from kcquant.notify import load_env, send_telegram_message
from kcquant.quotes import get_latest_kc_curve_prices
from monitor_kc import (
    download_latest_reports,
    evaluate_market_data,
    persist_results
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
    """Format market prices, spreads, and certified inventory as clean Telegram HTML."""
    now_ny = datetime.now(NY_TZ)
    time_str = now_ny.strftime("%b %d, %Y · %H:%M ET")

    banner = (
        "☕ <b>KC Monitor</b>\n"
        f"<i>{time_str}</i> {'(Ad-hoc)' if is_forced else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    prices_section = (
        f"📊 <b>KC Futures Prices:</b>\n"
        f"• KCZ26 (Dec 26): <code>{data['kcz26_price']:.2f}¢</code>\n"
        f"• KCH27 (Mar 27): <code>{data['kch27_price']:.2f}¢</code>\n"
        f"• KCK27 (May 27): <code>{data['kck27_price']:.2f}¢</code>\n\n"
    )

    spreads_section = (
        f"📈 <b>Calendar Spreads:</b>\n"
        f"• <b>Z26 / H27 Spread:</b> <code>{data['spread_zh_cents']:+.2f}¢</code>\n"
        f"• <b>H27 / K27 Spread:</b> <code>{data['spread_hk_cents']:+.2f}¢</code>\n\n"
    )

    if data.get("total_graded_today_bags", 0) > 0:
        graded_str = f"<code>{data['total_graded_today_bags']:,} bags</code> ({data['passed_today_bags']:,} passed / {data['failed_today_bags']:,} failed · {data['pass_rate_today']:.1f}% pass)"
    else:
        last_dt = data.get("last_grading_date", "N/A")
        last_rate = data.get("last_grading_rate", 0.0)
        graded_str = f"<code>0 bags</code> <i>(Last session {last_dt}: {last_rate:.0f}% pass)</i>"

    inventory_section = (
        f"📦 <b>Physical Certified Inventory:</b>\n"
        f"• Certified Stocks: <code>{data['certified_stocks_bags']:,} bags</code> ({data['certified_stock_delta_bags']:+,})\n"
        f"• Pending for Grading: <code>{data['pending_grading_bags']:,} bags</code> ({data['pending_delta_bags']:+,})\n"
        f"• Graded Today: {graded_str}\n"
        f"• Grading Pass Rate (7d): <code>{data.get('pass_rate_7d', 0.0):.1f}%</code> (<code>{data.get('pass_rate_7d_delta', 0.0):+.1f}%</code>)\n"
        f"• Total Pipeline: <code>{data['total_pipeline_bags']:,} bags</code>"
    )

    return banner + prices_section + spreads_section + inventory_section


def run_telegram_update(force: bool = False, z_price: float | None = None,
                        h_price: float | None = None, k_price: float | None = None,
                        no_download: bool = False, prefer_live: bool = True,
                        samples: int = 1, sample_interval: int = 300) -> tuple[bool, str]:
    """Execute monitor check, record 5m intraday snapshot, generate chart, and dispatch to Telegram."""
    now_ny = datetime.now(NY_TZ)
    is_open, reason = is_market_open(now_ny)

    if not is_open and not force:
        return False, f"Skipping Telegram update: {reason} (Use --force to send anyway)"

    stock_dir = ROOT / "data" / "fundamentals" / "ice_cert_stock"
    raw_curve_file = ROOT / "output" / "KC_databento_fixed_raw_curve.csv"
    monitor_dir = ROOT / "output" / "monitor"

    if not no_download:
        download_latest_reports(stock_dir, days_back=7)

    cert_df = load_cert_stocks(ROOT / "data" / "fundamentals")

    from kcquant.charting import record_cqg_intraday_snapshot, generate_intraday_5m_chart
    from kcquant.notify import send_telegram_photo
    from kcquant.orders import build_quick_order_keyboard

    result = None
    for s_idx in range(max(1, samples)):
        prices = get_latest_kc_curve_prices(
            raw_file=raw_curve_file,
            z_override=z_price,
            h_override=h_price,
            k_override=k_price,
            prefer_live=prefer_live
        )
        result = evaluate_market_data(prices, cert_df)
        persist_results(result, monitor_dir)
        record_cqg_intraday_snapshot(result)

        if s_idx < samples - 1:
            time.sleep(sample_interval)

    # Generate 15-minute smooth price line chart
    chart_path = generate_intraday_5m_chart()

    msg_html = format_telegram_html(result, is_forced=force and not is_open)

    # Build interactive quick order keyboard
    quick_keyboard = build_quick_order_keyboard(
        kcz26_price=float(result.get("kcz26_price", 305.0)),
        kch27_price=float(result.get("kch27_price", 290.0)),
    )

    # Deliver via photo card with HTML caption and inline keyboard
    if chart_path and chart_path.exists():
        success, res_str = send_telegram_photo(chart_path, caption=msg_html, reply_markup=quick_keyboard)
        if not success:
            success, res_str = send_telegram_message(msg_html, reply_markup=quick_keyboard)
    else:
        success, res_str = send_telegram_message(msg_html, reply_markup=quick_keyboard)

    # Log to jsonl
    log_file = monitor_dir / "kc_telegram_log.jsonl"
    with open(log_file, "a") as f:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "ny_time": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "market_open": is_open,
            "telegram_sent": success,
            "telegram_result": res_str,
            "chart_attached": bool(chart_path and chart_path.exists()),
            "spread_zh_cents": result["spread_zh_cents"],
            "spread_hk_cents": result["spread_hk_cents"]
        }
        f.write(json.dumps(log_entry) + "\n")

    return success, res_str


_PROCESSED_CALLBACK_IDS: set[str] = set()


def process_telegram_callbacks(offset: int | None = None) -> int:
    """Check Telegram for incoming callback_query taps or commands and handle them.

    Returns:
        Next offset integer.
    """
    global _PROCESSED_CALLBACK_IDS
    from kcquant.notify import get_telegram_updates, answer_telegram_callback
    from kcquant.orders import (
        place_paper_order,
        format_order_confirmation_html,
        update_positions_from_log,
        get_orders_dir
    )

    updates = get_telegram_updates(offset=offset, timeout=2)
    next_offset = offset or 0

    for u in updates:
        up_id = u.get("update_id", 0)
        next_offset = max(next_offset, up_id + 1)

        # 1. Handle Inline Button Callback Queries (Quick Orders)
        cb = u.get("callback_query")
        if cb:
            cb_id = cb.get("id")
            if cb_id in _PROCESSED_CALLBACK_IDS:
                continue
            _PROCESSED_CALLBACK_IDS.add(cb_id)

            data = cb.get("data", "")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")

            # Expected data format: "ord:BUY:KCZ26:MKT" or "ord:SELL:KCH27:LMT:290.00"
            if data.startswith("ord:"):
                parts = data.split(":")
                if len(parts) >= 4:
                    side = parts[1]        # BUY or SELL
                    contract = parts[2]    # KCZ26 or KCH27
                    ord_type = parts[3]    # MKT or LMT
                    price = float(parts[4]) if len(parts) >= 5 else None

                    # Acknowledge tap in Telegram UI
                    answer_telegram_callback(
                        cb_id,
                        text=f"✅ Placed: {side} 1 {contract} {ord_type}",
                        show_alert=False
                    )

                    # Execute paper trade (queries real-time CQG price at this exact split second)
                    order_record, positions = place_paper_order(
                        side=side,
                        contract=contract,
                        order_type="MARKET" if ord_type == "MKT" else "LIMIT",
                        qty=1,
                        target_price=price,
                        live_price=price if ord_type == "LMT" else None
                    )

                    # Send confirmation receipt to Telegram
                    receipt_html = format_order_confirmation_html(order_record, positions)
                    send_telegram_message(receipt_html, chat_id=chat_id)
                    print(f"[*] Executed Paper Order: {side} 1 {contract} {ord_type} @ {order_record['fill_price']:.2f}¢ ({order_record['order_id']})")

        # 2. Handle Text Commands (/position, /positions, /pos, /orders, /history)
        msg = u.get("message")
        if msg:
            text = (msg.get("text") or "").strip().lower()
            chat_id = msg.get("chat", {}).get("id")
            if text in ["/position", "/positions", "/pos", "/p"]:
                from kcquant.orders import (
                    update_portfolio_with_cqg_quotes,
                    format_portfolio_position_html,
                    build_quick_order_keyboard,
                )
                out_dir = get_orders_dir()
                pos_file = out_dir / "positions.json"
                orders_file = out_dir / "paper_orders.jsonl"
                pos, quotes = update_portfolio_with_cqg_quotes(orders_file, pos_file)
                pos_msg = format_portfolio_position_html(pos, quotes)
                send_telegram_message(pos_msg, reply_markup=build_quick_order_keyboard(), chat_id=chat_id)
                print(f"[*] Handled {text} command: Net P&L ${pos.get('realized_pnl_usd', 0.0) + pos.get('unrealized_pnl_usd', 0.0):+,.2f}")

            elif text in ["/order", "/orders", "/history"]:
                from kcquant.orders import build_quick_order_keyboard
                out_dir = get_orders_dir()
                orders_file = out_dir / "paper_orders.jsonl"
                recent_orders = []
                if orders_file.exists():
                    with open(orders_file) as f:
                        for line in f:
                            if line.strip():
                                try:
                                    recent_orders.append(json.loads(line))
                                except Exception:
                                    pass
                recent_orders = recent_orders[-5:]
                if recent_orders:
                    order_lines = []
                    for o in reversed(recent_orders):
                        s_icon = "🟢" if o.get("side") == "BUY" else "🔴"
                        order_lines.append(
                            f"{s_icon} <b>{o.get('side')} {o.get('qty')} {o.get('contract')}</b> @ <code>{o.get('fill_price', 0.0):.2f}¢</code>\n"
                            f"   └ <i>{o.get('datetime_ny', '')} · {o.get('order_id', '')}</i>"
                        )
                    ord_msg = (
                        "📋 <b>Recent Paper Orders:</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        + "\n".join(order_lines)
                    )
                else:
                    ord_msg = "📋 No orders recorded yet."
                send_telegram_message(ord_msg, reply_markup=build_quick_order_keyboard(), chat_id=chat_id)

    return next_offset


def main():
    load_env()
    ap = argparse.ArgumentParser(description="KC Coffee 'C' Market & Physical Inventory Telegram Bot")
    ap.add_argument("--test", action="store_true", help="Send a verification test message to Telegram and exit")
    ap.add_argument("--positions", "--position", dest="positions", action="store_true", help="Query live CQG prices, calculate unrealized P&L, and send snapshot to Telegram")
    ap.add_argument("--force", action="store_true", help="Force send message even if market is closed")
    ap.add_argument("--daemon", action="store_true", help="Run continuously in background, checking every 15 minutes and listening for quick orders")
    ap.add_argument("--listen", action="store_true", help="Run in dedicated interactive listener mode to process Telegram order taps")
    ap.add_argument("--interval", type=int, default=900, help="Check interval in seconds for daemon (default: 900 [15 mins])")
    ap.add_argument("--z-price", type=float, help="Override KCZ26 price in cents/lb")
    ap.add_argument("--h-price", type=float, help="Override KCH27 price in cents/lb")
    ap.add_argument("--k-price", type=float, help="Override KCK27 price in cents/lb")
    ap.add_argument("--settle-only", action="store_true", help="Use official daily settle instead of live intraday quote")
    ap.add_argument("--no-download", action="store_true", help="Skip ICE report download")
    ap.add_argument("--samples", type=int, default=1, help="Number of samples to record in this run (default: 1)")
    ap.add_argument("--sample-interval", type=int, default=300, help="Interval between samples in seconds (default: 300 [5m])")
    args = ap.parse_args()

    if args.test:
        print("[*] Testing Telegram Bot configuration...")
        test_msg = (
            "🤖 <b>KCquant Coffee Market Bot Initialized!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Tracking: <b>KC Futures (Z26, H27, K27)</b>\n"
            "📈 Spreads: <b>Z/H & H/K Calendar Spreads</b>\n"
            "📦 Inventory: <b>ICE Certified Warehouse & Grading Pass Rate</b>\n"
            f"📅 Time: {datetime.now(NY_TZ):%Y-%m-%d %H:%M:%S ET}"
        )
        ok, res = send_telegram_message(test_msg)
        if ok:
            print("✅ Success! Test message delivered to Telegram.")
            sys.exit(0)
        else:
            print(f"❌ Failed to send Telegram message: {res}")
            sys.exit(1)

    if args.positions:
        print("[*] Fetching live CQG quotes and calculating Unrealized P&L...")
        from kcquant.orders import (
            update_portfolio_with_cqg_quotes,
            format_portfolio_position_html,
            build_quick_order_keyboard
        )
        pos, quotes = update_portfolio_with_cqg_quotes()
        html = format_portfolio_position_html(pos, quotes)
        clean_text = (
            html.replace("<b>", "").replace("</b>", "")
            .replace("<code>", "").replace("</code>", "")
            .replace("<i>", "").replace("</i>", "")
        )
        print("\n" + clean_text + "\n")
        ok, res = send_telegram_message(html, reply_markup=build_quick_order_keyboard())
        if ok:
            print("✅ Position snapshot with live CQG P&L successfully delivered to Telegram!")
        else:
            print(f"❌ Failed to send position snapshot: {res}")
        sys.exit(0)

    if args.daemon or args.listen:
        mode_str = "daemon" if args.daemon else "interactive listener"
        print(f"[*] Starting persistent KC market Telegram bot {mode_str} (Interval: {args.interval}s)...")
        print("[*] Pre-warming CQG Desktop live session for instant sub-second execution...")
        try:
            from kcquant.cqg import get_warm_cqg_driver
            get_warm_cqg_driver()
            print("[*] CQG Desktop live session ready! Sub-second execution enabled.")
        except Exception as e:
            print(f"[!] Notice: CQG pre-warm skipped ({e}). Will use fallback.")
        print(f"    Listening for interactive order taps & sending market updates every {args.interval // 60}m. Press Ctrl+C to stop.")

        # Read last report timestamp from log file so we resume cleanly
        last_report_ts = 0.0
        log_file = ROOT / "output" / "monitor" / "kc_telegram_log.jsonl"
        if log_file.exists():
            try:
                with open(log_file) as f:
                    lines = [line.strip() for line in f if line.strip()]
                    if lines:
                        last_entry = json.loads(lines[-1])
                        if "timestamp" in last_entry:
                            last_report_ts = datetime.fromisoformat(last_entry["timestamp"]).timestamp()
            except Exception:
                pass

        offset = 0
        while _RUNNING:
            try:
                # 1. Listen for interactive Telegram button taps and text commands
                offset = process_telegram_callbacks(offset)

                # 2. Check if it's time for the 15-minute scheduled report
                cur_time = time.time()
                if cur_time - last_report_ts >= args.interval:
                    now_ny = datetime.now(NY_TZ)
                    is_open, reason = is_market_open(now_ny)
                    if is_open or args.force:
                        print(f"[{now_ny:%H:%M:%S ET}] Market is open. Running {args.interval // 60}-min update...")
                        ok, res = run_telegram_update(
                            force=args.force, z_price=args.z_price,
                            h_price=args.h_price, k_price=args.k_price,
                            no_download=args.no_download,
                            prefer_live=not args.settle_only,
                            samples=args.samples,
                            sample_interval=args.sample_interval
                        )
                        print(f"    Result: {'Sent' if ok else 'Failed'} ({res})")
                    else:
                        print(f"[{now_ny:%H:%M:%S ET}] {reason}. Waiting...")
                    last_report_ts = cur_time

                time.sleep(1.5)
            except KeyboardInterrupt:
                print("\n[*] Stopping Telegram bot daemon.")
                break
            except Exception as e:
                print(f"[!] Error in loop: {e}. Retrying in 5s...")
                time.sleep(5)

        print("[*] Telegram bot daemon exited cleanly.")
        try:
            from kcquant.cqg import _SHARED_DRIVER
            if _SHARED_DRIVER is not None:
                _SHARED_DRIVER.quit()
        except Exception:
            pass
        sys.exit(0)

    # Single-run execution: drain callbacks then send report
    offset = process_telegram_callbacks()
    ok, res = run_telegram_update(
        force=args.force, z_price=args.z_price,
        h_price=args.h_price, k_price=args.k_price,
        no_download=args.no_download,
        prefer_live=not args.settle_only,
        samples=args.samples,
        sample_interval=args.sample_interval
    )
    process_telegram_callbacks(offset)
    print(f"[*] Status: {'Delivered' if ok else 'Notice'} - {res}")


if __name__ == "__main__":
    main()
