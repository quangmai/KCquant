"""Unit tests for the new KC Market & Inventory Telegram Bot."""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest

from scripts.telegram_kc_bot import (
    format_telegram_html,
    is_market_open,
    is_bot_paused,
    set_bot_paused,
    build_runner_control_keyboard,
    process_telegram_control_commands,
)

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

    # Must contain prices and both spreads (with H27 Focus and Z26/K27 Ref)
    assert "KCH27 (Mar 27): <code>292.25¢</code>" in html
    assert "KCZ26 (Dec 26) [Ref]: <code>305.00¢</code>" in html
    assert "KCK27 (May 27) [Ref]: <code>287.35¢</code>" in html
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


def test_bot_pause_and_resume_state(tmp_path):
    state_file = tmp_path / "bot_control_state.json"
    assert is_bot_paused(state_file) is False

    st_paused = set_bot_paused(True, state_file)
    assert st_paused["paused"] is True
    assert st_paused["status"] == "PAUSED"
    assert is_bot_paused(state_file) is True

    kb_paused = build_runner_control_keyboard(paused=True)
    assert kb_paused["inline_keyboard"][0][0]["callback_data"] == "ctrl:resume"

    st_active = set_bot_paused(False, state_file)
    assert st_active["paused"] is False
    assert st_active["status"] == "ACTIVE"
    assert is_bot_paused(state_file) is False

    kb_active = build_runner_control_keyboard(paused=False)
    assert kb_active["inline_keyboard"][0][0]["callback_data"] == "ctrl:pause"


def test_process_telegram_control_commands(tmp_path):
    state_file = tmp_path / "bot_control_state.json"

    fake_updates_pause = [
        {
            "update_id": 100,
            "message": {"chat": {"id": 501402719}, "text": "/pause"}
        }
    ]
    with patch("kcquant.notify.get_telegram_updates", return_value=fake_updates_pause), \
         patch("scripts.telegram_kc_bot.send_telegram_message") as mock_send:
        next_off, trigger_now = process_telegram_control_commands(0, state_file=state_file)
        assert next_off == 101
        assert trigger_now is False
        assert is_bot_paused(state_file) is True
        assert mock_send.called

    fake_updates_resume = [
        {
            "update_id": 101,
            "callback_query": {
                "id": "cb_resume_1",
                "data": "ctrl:resume",
                "message": {"chat": {"id": 501402719}}
            }
        }
    ]
    with patch("kcquant.notify.get_telegram_updates", return_value=fake_updates_resume), \
         patch("kcquant.notify.answer_telegram_callback") as mock_ans, \
         patch("scripts.telegram_kc_bot.send_telegram_message") as mock_send:
        next_off, trigger_now = process_telegram_control_commands(101, state_file=state_file)
        assert next_off == 102
        assert trigger_now is True
        assert is_bot_paused(state_file) is False
        assert mock_ans.called
        assert mock_send.called

