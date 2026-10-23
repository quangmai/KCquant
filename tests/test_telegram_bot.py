"""Unit tests for Telegram notification bot and market-hours guard."""
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
import pytest

from kcquant.notify import send_telegram_message
from scripts.telegram_zh_bot import is_market_open, format_telegram_html

NY_TZ = ZoneInfo("America/New_York")


def test_is_market_open():
    # 1. Saturday morning -> closed
    sat = datetime(2026, 10, 10, 10, 0, tzinfo=NY_TZ)
    is_open, reason = is_market_open(sat)
    assert not is_open
    assert "weekends" in reason

    # 2. Wednesday pre-market (03:30 ET) -> closed
    wed_early = datetime(2026, 10, 7, 3, 30, tzinfo=NY_TZ)
    is_open, reason = is_market_open(wed_early)
    assert not is_open
    assert "Pre-market" in reason

    # 3. Wednesday trading hours (10:30 ET) -> OPEN
    wed_mid = datetime(2026, 10, 7, 10, 30, tzinfo=NY_TZ)
    is_open, reason = is_market_open(wed_mid)
    assert is_open
    assert "OPEN" in reason

    # 4. Wednesday post-close (14:30 ET) -> closed
    wed_late = datetime(2026, 10, 7, 14, 30, tzinfo=NY_TZ)
    is_open, reason = is_market_open(wed_late)
    assert not is_open
    assert "Post-close" in reason


def test_format_telegram_html():
    sample_data = {
        "action_flag": "HOLD_POSITION",
        "status_message": "Hold position",
        "spread_zh_cents": 8.25,
        "kcz26_price": 288.75,
        "kch27_price": 280.50,
        "unrealized_pnl_usd": 0.0,
        "unrealized_pnl_cents": 0.0,
        "distance_to_take_profit_cents": 6.25,
        "distance_to_stop_loss_cents": 2.25,
        "take_profit_target_cents": 2.0,
        "stop_loss_level_cents": 10.5,
        "bdays_to_fnd": 29,
        "fnd_date": "2026-11-19",
        "certified_stocks_bags": 252006,
        "certified_stock_delta_bags": -250,
        "pending_grading_bags": 12531,
        "pending_delta_bags": -2436,
        "passed_today_bags": 0,
        "failed_today_bags": 3036,
        "pass_rate_7d": 54.0,
        "pass_rate_7d_delta": -13.2,
        "total_pipeline_bags": 264537,
        "as_of_report_date": "2026-10-09"
    }

    # Standard hold message
    html = format_telegram_html(sample_data)
    assert "KC Z/H SPREAD 30-MIN UPDATE" in html
    assert "KCZ26" in html
    assert "+8.25¢" in html
    assert "252,006 bags" in html
    assert "Grading Pass Rate (7d): <code>54.0%</code> (<code>-13.2%</code>)" in html

    # Take profit trigger
    tp_data = dict(sample_data, action_flag="TAKE_PROFIT_TRIGGERED", spread_zh_cents=1.50, unrealized_pnl_usd=2531.25, unrealized_pnl_cents=6.75)
    tp_html = format_telegram_html(tp_data)
    assert "TAKE PROFIT TRIGGERED" in tp_html
    assert "+$2,531.25" in tp_html

    # Stop loss trigger
    sl_data = dict(sample_data, action_flag="STOP_LOSS_TRIGGERED", spread_zh_cents=11.00, unrealized_pnl_usd=-1031.25, unrealized_pnl_cents=-2.75)
    sl_html = format_telegram_html(sl_data)
    assert "STOP LOSS TRIGGERED" in sl_html


def test_send_telegram_missing_env():
    ok, err = send_telegram_message("Test message", token="", chat_id="")
    assert not ok
    assert "Missing" in err


@patch("requests.post")
def test_send_telegram_mock_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 123}}
    mock_post.return_value = mock_resp

    ok, msg = send_telegram_message("Test message", token="mock_token", chat_id="mock_chat")
    assert ok
    assert "successfully" in msg
