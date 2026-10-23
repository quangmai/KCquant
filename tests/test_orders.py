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
    assert rows[0][0]["callback_data"] == "ord:BUY:KCZ26:MKT"
    assert rows[0][1]["callback_data"] == "ord:SELL:KCZ26:MKT"


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
    assert "Last Price Update" in html
    assert "Source:" not in html


def test_format_portfolio_position_html():
    from kcquant.orders import format_portfolio_position_html

    pos = {
        "positions": {
            "KCZ26": {
                "net_qty": -1,
                "side": "SHORT",
                "avg_price_cents": 310.60,
                "current_price_cents": 309.80,
                "realized_pnl_usd": 1181.25,
                "unrealized_pnl_usd": 300.00,
            }
        },
        "realized_pnl_usd": 1181.25,
        "unrealized_pnl_usd": 300.00,
        "total_orders": 21,
    }
    quotes = {
        "KCZ26": {
            "price": 309.80,
            "bid": 309.70,
            "ask": 309.80,
            "source": "CQG Desktop (Live)",
            "latency_ms": 48,
        }
    }
    html = format_portfolio_position_html(pos, quotes)
    assert "Portfolio & Real-Time P&L" in html
    assert "SHORT <b>1</b>" in html
    assert "310.60¢" in html
    assert "309.80¢" in html
    assert "+$300.00" in html
    assert "+$1,181.25" in html
    assert "+$1,481.25" in html  # Net Combined P&L
    assert "Last Price Update" in html
    assert "48ms" in html
    assert "Price Source" not in html
    assert "CQG Live" not in html


def test_format_portfolio_position_html_flat():
    from kcquant.orders import format_portfolio_position_html

    pos = {
        "positions": {},
        "realized_pnl_usd": 500.00,
        "unrealized_pnl_usd": 0.0,
        "total_orders": 4,
    }
    html = format_portfolio_position_html(pos, {})
    assert "FLAT (No open contracts)" in html
    assert "+$500.00" in html

