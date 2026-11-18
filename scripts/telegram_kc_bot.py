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
    quote_time = data.get("quote_time_ny") or now_ny.strftime("%H:%M:%S ET")
    dur_ms = data.get("fetch_duration_ms")
    dur_str = f" <i>({dur_ms}ms)</i>" if dur_ms is not None else ""

    banner = (
        "☕ <b>KC Monitor</b>\n"
        f"<i>{time_str}</i> {'(Ad-hoc)' if is_forced else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    prices_section = (
        f"📊 <b>KC Futures Prices (H27 Focus):</b>\n"
        f"• <b>⭐ KCH27 (Mar 27): <code>{data['kch27_price']:.2f}¢</code></b>\n"
        f"• KCZ26 (Dec 26) [Ref]: <code>{data['kcz26_price']:.2f}¢</code>\n"
        f"• KCK27 (May 27) [Ref]: <code>{data['kck27_price']:.2f}¢</code>\n"
        f"• <b>Last Price Update:</b> <code>{quote_time}</code>{dur_str}\n\n"
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

    result = None
    for s_idx in range(max(1, samples)):
        t_start = time.time()
        prices = get_latest_kc_curve_prices(
            raw_file=raw_curve_file,
            z_override=z_price,
            h_override=h_price,
            k_override=k_price,
            prefer_live=prefer_live
        )
        fetch_dur_ms = int((time.time() - t_start) * 1000)
        result = evaluate_market_data(prices, cert_df)
        result["fetch_duration_ms"] = fetch_dur_ms
        persist_results(result, monitor_dir)
        record_cqg_intraday_snapshot(result)

        if s_idx < samples - 1:
            time.sleep(sample_interval)

    # Generate 15-minute smooth price line chart
    chart_path = generate_intraday_5m_chart()

    msg_html = format_telegram_html(result, is_forced=force and not is_open)
    ctrl_kb = build_runner_control_keyboard(paused=is_bot_paused())

    # Deliver via photo card with HTML caption and runner control toggle button
    if chart_path and chart_path.exists():
        success, res_str = send_telegram_photo(chart_path, caption=msg_html, reply_markup=ctrl_kb)
        if not success:
            success, res_str = send_telegram_message(msg_html, reply_markup=ctrl_kb)
    else:
        success, res_str = send_telegram_message(msg_html, reply_markup=ctrl_kb)

    # Log to jsonl
    log_file = monitor_dir / "kc_telegram_log.jsonl"
    with open(log_file, "a") as f:
        log_entry = {
            "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
            "ny_time": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "market_open": is_open,
            "telegram_sent": success,
            "telegram_result": res_str,
            "chart_attached": bool(chart_path and chart_path.exists()),
            "spread_zh_cents": result["spread_zh_cents"],
            "spread_hk_cents": result["spread_hk_cents"]
        }
        f.write(json.dumps(log_entry) + "\n")

    sync_state_to_git("Auto-sync 15m monitor history [skip ci]")

    return success, res_str


CONTROL_STATE_FILE = ROOT / "output" / "monitor" / "bot_control_state.json"
_PROCESSED_CALLBACK_IDS: set[str] = set()


def is_bot_paused(state_file: Path | None = None) -> bool:
    """Return True if the runner has been paused from Telegram chat."""
    path = state_file or CONTROL_STATE_FILE
    if not path.exists():
        return False
    try:
        with open(path) as f:
            data = json.load(f)
        return bool(data.get("paused", False))
    except Exception:
        return False


def set_bot_paused(paused: bool, state_file: Path | None = None) -> dict:
    """Persist runner paused/active state so it survives runner restarts and handovers."""
    path = state_file or CONTROL_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    now_ny = datetime.now(NY_TZ)
    state = {
        "paused": bool(paused),
        "status": "PAUSED" if paused else "ACTIVE",
        "updated_at_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "updated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
    }
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    return state


def build_runner_control_keyboard(paused: bool = False) -> dict:
    """Build inline Telegram button to pause or reactivate the runner."""
    if paused:
        return {
            "inline_keyboard": [[
                {"text": "▶️ Reactivate Runner (/resume)", "callback_data": "ctrl:resume"},
                {"text": "📊 Send Snapshot Now (/now)", "callback_data": "ctrl:now"},
            ]]
        }
    return {
        "inline_keyboard": [[
            {"text": "⏸️ Pause Runner (/pause)", "callback_data": "ctrl:pause"},
            {"text": "📊 Refresh Now (/now)", "callback_data": "ctrl:now"},
        ]]
    }


def _handle_pause_action(chat_id: str | int | None, state_file: Path | None = None) -> None:
    st = set_bot_paused(True, state_file=state_file)
    msg = (
        "⏸️ <b>KC Monitor Runner PAUSED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Status:</b> <code>PAUSED</code>\n"
        f"• <b>Updated:</b> <code>{st['updated_at_ny']}</code>\n\n"
        "<i>Scheduled 15-minute market updates are suspended. "
        "Send <code>/resume</code> or tap the button below anytime to reactivate.</i>"
    )
    send_telegram_message(msg, chat_id=str(chat_id) if chat_id else None, reply_markup=build_runner_control_keyboard(paused=True))
    print(f"[*] Runner PAUSED via Telegram at {st['updated_at_ny']}", flush=True)
    sync_state_to_git("Pause KC runner via Telegram [skip ci]")


def _handle_resume_action(chat_id: str | int | None, state_file: Path | None = None) -> None:
    st = set_bot_paused(False, state_file=state_file)
    msg = (
        "▶️ <b>KC Monitor Runner REACTIVATED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Status:</b> <code>ACTIVE (15m Schedule)</code>\n"
        f"• <b>Updated:</b> <code>{st['updated_at_ny']}</code>\n\n"
        "<i>Scheduled 15-minute updates are back online. Dispatching an immediate market snapshot now...</i>"
    )
    send_telegram_message(msg, chat_id=str(chat_id) if chat_id else None, reply_markup=build_runner_control_keyboard(paused=False))
    print(f"[*] Runner REACTIVATED via Telegram at {st['updated_at_ny']}", flush=True)
    sync_state_to_git("Reactivate KC runner via Telegram [skip ci]")


def _handle_status_action(chat_id: str | int | None, state_file: Path | None = None) -> None:
    paused = is_bot_paused(state_file=state_file)
    now_ny = datetime.now(NY_TZ)
    is_open, reason = is_market_open(now_ny)
    status_badge = "⏸️ PAUSED" if paused else "🟢 ACTIVE (Every 15m)"
    msg = (
        "🤖 <b>KC Monitor Runner Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Runner State:</b> <code>{status_badge}</code>\n"
        f"• <b>Market Status:</b> <code>{'OPEN' if is_open else 'CLOSED'}</code>\n"
        f"• <b>Detail:</b> <i>{reason}</i>\n"
        f"• <b>Commands:</b> <code>/pause</code> · <code>/resume</code> · <code>/status</code> · <code>/now</code>"
    )
    send_telegram_message(msg, chat_id=str(chat_id) if chat_id else None, reply_markup=build_runner_control_keyboard(paused=paused))


def process_telegram_control_commands(
    offset: int | None = None,
    state_file: Path | None = None
) -> tuple[int, bool]:
    """Poll Telegram for /pause, /resume, /status, /now commands or control button taps.

    Returns:
        (next_offset: int, trigger_immediate_update: bool)
    """
    global _PROCESSED_CALLBACK_IDS
    from kcquant.notify import get_telegram_updates, answer_telegram_callback

    updates = get_telegram_updates(offset=offset, timeout=2)
    next_offset = offset or 0
    trigger_now = False

    for u in updates:
        up_id = u.get("update_id", 0)
        next_offset = max(next_offset, up_id + 1)

        # 1. Handle Inline Control Button Taps
        cb = u.get("callback_query")
        if cb:
            cb_id = cb.get("id")
            if cb_id in _PROCESSED_CALLBACK_IDS:
                continue
            if cb_id:
                _PROCESSED_CALLBACK_IDS.add(cb_id)

            data = cb.get("data", "")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")

            if data == "ctrl:pause":
                if cb_id:
                    answer_telegram_callback(cb_id, text="⏸️ Runner Paused")
                _handle_pause_action(chat_id, state_file=state_file)
            elif data == "ctrl:resume":
                if cb_id:
                    answer_telegram_callback(cb_id, text="▶️ Runner Reactivated")
                _handle_resume_action(chat_id, state_file=state_file)
                trigger_now = True
            elif data == "ctrl:now":
                if cb_id:
                    answer_telegram_callback(cb_id, text="📊 Generating market snapshot...")
                trigger_now = True

        # 2. Handle Slash Commands in Chat
        msg = u.get("message")
        if msg:
            raw_text = (msg.get("text") or "").strip().lower()
            cmd = raw_text.split()[0].split("@")[0] if raw_text else ""
            chat_id = msg.get("chat", {}).get("id")

            if cmd in ["/pause", "/stop"]:
                _handle_pause_action(chat_id, state_file=state_file)
            elif cmd in ["/resume", "/start", "/activate", "/reactivate"]:
                _handle_resume_action(chat_id, state_file=state_file)
                trigger_now = True
            elif cmd in ["/status"]:
                _handle_status_action(chat_id, state_file=state_file)
            elif cmd in ["/now", "/update"]:
                trigger_now = True

    return next_offset, trigger_now


def sync_state_to_git(commit_message: str) -> None:
    """Commit and push monitor state if running in GitHub Actions or explicitly enabled."""
    if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("ENABLE_GIT_SYNC") != "1":
        return

    try:
        import subprocess
        git_env = os.environ.copy()
        git_env["GIT_TERMINAL_PROMPT"] = "0"

        res = subprocess.run(
            ["git", "status", "--porcelain", "output/monitor"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=10, env=git_env
        )
        if res.stdout.strip():
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=str(ROOT), check=False, timeout=5, env=git_env)
            subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=str(ROOT), check=False, timeout=5, env=git_env)
            subprocess.run(["git", "add", "output/monitor/"], cwd=str(ROOT), check=False, timeout=10, env=git_env)
            subprocess.run(["git", "commit", "-m", commit_message], cwd=str(ROOT), check=False, timeout=10, env=git_env)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=str(ROOT), check=False, timeout=15, env=git_env)
            subprocess.run(["git", "push", "origin", "main"], cwd=str(ROOT), check=False, timeout=15, env=git_env)
    except Exception as e:
        print(f"[!] Git sync notice: {e}")


def main():
    load_env()
    ap = argparse.ArgumentParser(description="KC Coffee 'C' Market & Physical Inventory Telegram Bot")
    ap.add_argument("--test", action="store_true", help="Send a verification test message to Telegram and exit")
    ap.add_argument("--force", action="store_true", help="Force send message even if market is closed")
    ap.add_argument("--daemon", action="store_true", help="Run continuously in background, checking every 15 minutes")
    ap.add_argument("--interval", type=int, default=900, help="Check interval in seconds for daemon (default: 900 [15 mins])")
    ap.add_argument("--z-price", type=float, help="Override KCZ26 price in cents/lb")
    ap.add_argument("--h-price", type=float, help="Override KCH27 price in cents/lb")
    ap.add_argument("--k-price", type=float, help="Override KCK27 price in cents/lb")
    ap.add_argument("--settle-only", action="store_true", help="Use official daily settle instead of live intraday quote")
    ap.add_argument("--no-download", action="store_true", help="Skip ICE report download")
    ap.add_argument("--samples", type=int, default=1, help="Number of samples to record in this run (default: 1)")
    ap.add_argument("--sample-interval", type=int, default=300, help="Interval between samples in seconds (default: 300 [5m])")
    ap.add_argument("--duration", type=int, default=0, help="Max run duration in seconds for daemon (0 = run indefinitely)")
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

    if args.daemon:
        print(f"[*] Starting persistent KC market Telegram bot daemon (Interval: {args.interval}s)...", flush=True)
        try:
            from kcquant.notify import register_telegram_bot_commands
            register_telegram_bot_commands()
        except Exception:
            pass
        print("[*] Pre-warming CQG Desktop live session for instant sub-second execution...", flush=True)
        try:
            from kcquant.cqg import get_warm_cqg_driver
            get_warm_cqg_driver()
            print("[*] CQG Desktop live session ready! Sub-second execution enabled.", flush=True)
        except Exception as e:
            print(f"[!] Notice: CQG pre-warm skipped ({e}). Will use fallback.", flush=True)
        print(f"    Sending market updates every {args.interval // 60}m & listening for /pause and /resume. Press Ctrl+C to stop.", flush=True)

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
                            ts_str = last_entry["timestamp"]
                            dt_obj = datetime.fromisoformat(ts_str)
                            if dt_obj.tzinfo is None:
                                dt_obj = dt_obj.replace(tzinfo=ZoneInfo("UTC"))
                            last_report_ts = dt_obj.timestamp()
            except Exception:
                last_report_ts = 0.0

        offset = 0
        start_time = time.time()
        while _RUNNING:
            if args.duration > 0 and (time.time() - start_time) >= args.duration:
                print(f"[*] Target duration ({args.duration}s) reached. Exiting cleanly for runner handover...", flush=True)
                break
            try:
                # 1. Poll for /pause, /resume, /status, /now commands or control button taps
                offset, trigger_now = process_telegram_control_commands(offset)

                if trigger_now:
                    now_ny = datetime.now(NY_TZ)
                    print(f"[{now_ny:%H:%M:%S ET}] Immediate update requested from Telegram...", flush=True)
                    ok, res = run_telegram_update(
                        force=True, z_price=args.z_price,
                        h_price=args.h_price, k_price=args.k_price,
                        no_download=args.no_download,
                        prefer_live=not args.settle_only,
                        samples=args.samples,
                        sample_interval=args.sample_interval
                    )
                    print(f"    Result: {'Sent' if ok else 'Failed'} ({res})", flush=True)
                    last_report_ts = time.time()

                # 2. Check if it's time for the 15-minute scheduled report
                cur_time = time.time()
                if cur_time - last_report_ts >= args.interval:
                    now_ny = datetime.now(NY_TZ)
                    if is_bot_paused():
                        print(f"[{now_ny:%H:%M:%S ET}] Runner is PAUSED via Telegram. Skipping scheduled update...", flush=True)
                    else:
                        is_open, reason = is_market_open(now_ny)
                        if is_open or args.force:
                            print(f"[{now_ny:%H:%M:%S ET}] Market is open. Running {args.interval // 60}-min update...", flush=True)
                            ok, res = run_telegram_update(
                                force=args.force, z_price=args.z_price,
                                h_price=args.h_price, k_price=args.k_price,
                                no_download=args.no_download,
                                prefer_live=not args.settle_only,
                                samples=args.samples,
                                sample_interval=args.sample_interval
                            )
                            print(f"    Result: {'Sent' if ok else 'Failed'} ({res})", flush=True)
                        else:
                            print(f"[{now_ny:%H:%M:%S ET}] {reason}. Waiting...", flush=True)
                    last_report_ts = cur_time

                time.sleep(2)
            except KeyboardInterrupt:
                print("\n[*] Stopping Telegram bot daemon.", flush=True)
                break
            except Exception as e:
                print(f"[!] Error in loop: {e}. Retrying in 5s...", flush=True)
                time.sleep(5)

        print("[*] Telegram bot daemon exited cleanly.")
        try:
            from kcquant.cqg import _SHARED_DRIVER
            if _SHARED_DRIVER is not None:
                _SHARED_DRIVER.quit()
        except Exception:
            pass
        sys.exit(0)

    # Single-run execution
    offset, _ = process_telegram_control_commands()
    if is_bot_paused() and not args.force:
        print("[*] Notice: Runner is currently PAUSED via Telegram (use /resume in Telegram or --force).")
        sys.exit(0)
    ok, res = run_telegram_update(
        force=args.force, z_price=args.z_price,
        h_price=args.h_price, k_price=args.k_price,
        no_download=args.no_download,
        prefer_live=not args.settle_only,
        samples=args.samples,
        sample_interval=args.sample_interval
    )
    process_telegram_control_commands(offset)
    print(f"[*] Status: {'Delivered' if ok else 'Notice'} - {res}")


if __name__ == "__main__":
    main()
