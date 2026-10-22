"""Unit tests for the new KC Market & Inventory Telegram Bot."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from scripts.telegram_kc_bot import format_telegram_html, is_market_open

NY_TZ = ZoneInfo("America/New_York")


def test_is_market_open_kc_bot():
    # 1. Saturday -> closed
    sat = datetime(2026, 10, 24, 10, 0, tzinfo=NY_TZ)
    is_open, reason = is_market_open(sat)
    assert not is_open
    assert "weekends" in reason

    # 2. Weekday pre-market (03:30 ET) -> closed
    wed_early = datetime(2026, 10, 21, 3, 30, tzinfo=NY_TZ)
    is_open, reason = is_market_open(wed_early)
    assert not is_open
    assert "Pre-market" in reason

    # 3. Weekday trading hours (10:30 ET) -> OPEN
    wed_mid = datetime(2026, 10, 21, 10, 30, tzinfo=NY_TZ)
    is_open, reason = is_market_open(wed_mid)
    assert is_open
    assert "OPEN" in reason

    # 4. Weekday post-close (14:30 ET) -> closed
    wed_late = datetime(2026, 10, 21, 14, 30, tzinfo=NY_TZ)
    is_open, reason = is_market_open(wed_late)
    assert not is_open
    assert "Post-close" in reason


def test_format_telegram_html_clean_structure():
    sample_data = {
        "kcz26_price": 305.00,
        "kch27_price": 292.25,
        "kck27_price": 287.35,
        "spread_zh_cents": 12.75,
        "spread_hk_cents": 4.90,
        "price_source": "Live Market (ICE / Yahoo · 08:30 ET)",
        "certified_stocks_bags": 239748,
        "certified_stock_delta_bags": 4847,
        "pending_grading_bags": 18016,
        "pending_delta_bags": -8787,
        "passed_today_bags": 7932,
        "failed_today_bags": 855,
        "total_graded_today_bags": 8787,
        "pass_rate_today": 90.3,
        "pass_rate_7d": 65.3,
        "pass_rate_7d_delta": 11.2,
        "total_pipeline_bags": 257764,
        "as_of_report_date": "2026-10-21"
    }

    html = format_telegram_html(sample_data)

    # Must contain prices and both spreads
    assert "KCZ26 (Dec 26): <code>305.00¢</code>" in html
    assert "KCH27 (Mar 27): <code>292.25¢</code>" in html
    assert "KCK27 (May 27): <code>287.35¢</code>" in html
    assert "<b>Z26 / H27 Spread:</b> <code>+12.75¢</code>" in html
    assert "<b>H27 / K27 Spread:</b> <code>+4.90¢</code>" in html

    # Must contain physical certified inventory
    assert "Certified Stocks: <code>239,748 bags</code> (+4,847)" in html
    assert "Pending for Grading: <code>18,016 bags</code> (-8,787)" in html
    assert "Grading Pass Rate (7d): <code>65.3%</code> (<code>+11.2%</code>)" in html
    assert "Total Pipeline: <code>257,764 bags</code>" in html

    # Header check
    assert "<b>KC Monitor</b>" in html
    assert "Source:" not in html
    assert "As-of Report:" not in html

    # Must NOT contain trading positions, strategy milestones, or status
    assert "HOLD_POSITION" not in html
    assert "TAKE PROFIT" not in html
    assert "STOP LOSS" not in html
    assert "Unrealized P&L" not in html
    assert "Take-Profit Target" not in html
    assert "Stop-Loss Target" not in html
    assert "First Notice Day" not in html
