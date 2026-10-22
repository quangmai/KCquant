"""Tests for paper trading order manager and Telegram quick-order keyboard."""
import json
from pathlib import Path
import pytest

from kcquant.orders import (
    build_quick_order_keyboard,
    place_paper_order,
    update_positions_from_log,
    format_order_confirmation_html,
    CONTRACT_SIZE_LBS,
    DOLLARS_PER_CENT,
)


def test_build_quick_order_keyboard():
    kb = build_quick_order_keyboard(kcz26_price=306.85)
    assert "inline_keyboard" in kb
    rows = kb["inline_keyboard"]
    assert len(rows) == 1
    assert len(rows[0]) == 2

    # Check button text
    assert "BUY 1 KC Z26 Market" in rows[0][0]["text"]
    assert "SELL 1 KC Z26 Market" in rows[0][1]["text"]

    # Check callback_data
    assert rows[0][0]["callback_data"] == "ord:BUY:KCZ26:MKT:306.85"
    assert rows[0][1]["callback_data"] == "ord:SELL:KCZ26:MKT:306.85"


def test_place_paper_order_and_position_tracking(tmp_path):
    out_dir = tmp_path / "orders"
    out_dir.mkdir(parents=True)

    # 1. Place BUY 1 KCZ26 Market at 306.85
    ord1, pos1 = place_paper_order(
        side="BUY",
        contract="KCZ26",
        order_type="MARKET",
        qty=1,
        live_price=306.85,
        orders_dir=out_dir
    )
    assert ord1["status"] == "FILLED"
    assert ord1["fill_price"] == 306.85
    assert pos1["positions"]["KCZ26"]["net_qty"] == 1
    assert pos1["positions"]["KCZ26"]["avg_price_cents"] == 306.85

    # 2. Place SELL 1 KCZ26 Market at 310.85 (+4.00 cents profit)
    ord2, pos2 = place_paper_order(
        side="SELL",
        contract="KCZ26",
        order_type="MARKET",
        qty=1,
        live_price=310.85,
        orders_dir=out_dir
    )
    assert ord2["status"] == "FILLED"
    assert pos2["positions"]["KCZ26"]["net_qty"] == 0
    # 4.00 cents * $375/cent = $1,500.00 realized profit
    assert pos2["realized_pnl_usd"] == 1500.00


def test_format_order_confirmation_html():
    order = {
        "order_id": "ORD-20261022-TEST01",
        "contract": "KCZ26",
        "side": "BUY",
        "qty": 1,
        "fill_price": 306.85,
        "order_type": "MARKET",
        "notional_usd": 115068.75,
        "datetime_ny": "2026-10-22 10:25 ET"
    }
    pos = {
        "positions": {
            "KCZ26": {
                "net_qty": 1,
                "avg_price_cents": 306.85,
                "unrealized_pnl_usd": 0.0
            }
        },
        "realized_pnl_usd": 0.0
    }
    html = format_order_confirmation_html(order, pos)
    assert "Paper Order Executed" in html
    assert "BUY 1 KCZ26" in html
    assert "306.85¢" in html
    assert "ORD-20261022-TEST01" in html
