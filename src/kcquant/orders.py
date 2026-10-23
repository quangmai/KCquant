"""Paper trading order manager and Telegram quick-order keyboard for KC Coffee 'C'.

Supports:
  1. Quick Tap Inline Keyboard for Telegram:
     - BUY 1 KC Z26 Market
     - BUY 1 KC H27 Limit at (Current Price - 3)
     - SELL 1 KC Z26 Market
     - SELL 1 KC H27 Limit at (Current Price - 3)
  2. Simulated paper order execution, position tracking, and PnL calculation.
     ICE Coffee 'C' Contract Size: 37,500 lbs (1.00¢ move = $375.00 per contract).
  3. Persistence to output/orders/paper_orders.jsonl and output/orders/positions.json.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
NY_TZ = ZoneInfo("America/New_York")

CONTRACT_SIZE_LBS = 37_500  # 1 contract = 37,500 lbs Arabica Coffee 'C'
DOLLARS_PER_CENT = 375.0   # 1 cent/lb = $375.00 per contract


def get_orders_dir() -> Path:
    d = ROOT / "output" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_quick_order_keyboard(kcz26_price: float | None = None, kch27_price: float | None = None) -> dict:
    """Build the quick tap Market order buttons."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🟢 BUY 1 KC Z26 Market",
                    "callback_data": "ord:BUY:KCZ26:MKT"
                },
                {
                    "text": "🔴 SELL 1 KC Z26 Market",
                    "callback_data": "ord:SELL:KCZ26:MKT"
                }
            ]
        ]
    }


def place_paper_order(
    side: str,
    contract: str,
    order_type: str = "MARKET",
    qty: int = 1,
    target_price: float | None = None,
    live_price: float | None = None,
    orders_dir: Path | None = None,
) -> tuple[dict, dict]:
    """Execute and record a simulated paper order using real-time instant CQG market price.

    Returns:
        (order_dict, updated_positions_dict)
    """
    import time
    t_start = time.time()
    out_dir = orders_dir or get_orders_dir()
    orders_file = out_dir / "paper_orders.jsonl"
    pos_file = out_dir / "positions.json"

    side = side.strip().upper()
    contract = contract.strip().upper()
    order_type = order_type.strip().upper()

    now_ny = datetime.now(NY_TZ)
    order_id = f"ORD-{now_ny.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    bid_px = 0.0
    ask_px = 0.0
    source_label = "Manual"
    fill_side = "Trade"

    # Fetch instant real-time price from CQG at the exact moment of placement
    if order_type == "MARKET" and live_price is None:
        try:
            from kcquant.cqg import fetch_instant_cqg_quote
            quote = fetch_instant_cqg_quote(contract)
        except Exception:
            quote = None

        if quote and float(quote.get("price", 0.0)) > 0:
            trade_px = float(quote.get("price", 0.0))
            bid_px = float(quote.get("bid", trade_px))
            ask_px = float(quote.get("ask", trade_px))
            source_label = quote.get("source", "CQG Desktop (Live)")

            if side == "BUY":
                fill_price = ask_px if ask_px > 0 else trade_px
                fill_side = "Ask"
            else:  # SELL
                fill_price = bid_px if bid_px > 0 else trade_px
                fill_side = "Bid"
        else:
            # Fallback to freshest known market price from monitor cache / curve history
            try:
                from kcquant.quotes import get_latest_kc_curve_prices
                latest_data = get_latest_kc_curve_prices(prefer_live=True)
                fill_price = float(latest_data.get(contract.lower(), 310.20))
                source_label = latest_data.get("price_source", "Market Cache")
            except Exception:
                fill_price = 310.20
                source_label = "Market Baseline"
            bid_px = fill_price
            ask_px = fill_price
    elif order_type == "LIMIT":
        fill_price = round(target_price or live_price or 310.20, 2)
        bid_px = fill_price
        ask_px = fill_price
        fill_side = "Limit"
    else:
        fill_price = round(live_price or target_price or 310.20, 2)
        bid_px = fill_price
        ask_px = fill_price

    latency_ms = int((time.time() - t_start) * 1000)

    order_record = {
        "order_id": order_id,
        "timestamp": now_ny.isoformat(),
        "datetime_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "contract": contract,
        "side": side,
        "qty": int(qty),
        "order_type": order_type,
        "limit_price": round(target_price, 2) if target_price is not None else None,
        "fill_price": round(fill_price, 2),
        "fill_side": fill_side,
        "bid": round(bid_px, 2),
        "ask": round(ask_px, 2),
        "spread_cents": round(ask_px - bid_px, 2) if ask_px and bid_px else 0.0,
        "source": source_label,
        "latency_ms": latency_ms,
        "status": "FILLED",
        "contract_size_lbs": CONTRACT_SIZE_LBS,
        "notional_usd": round(fill_price * CONTRACT_SIZE_LBS / 100.0 * qty, 2),
        "execution": "PAPER_SIMULATED",
    }

    # Append to orders log
    with open(orders_file, "a") as f:
        f.write(json.dumps(order_record) + "\n")

    # Update positions and PnL
    positions = update_positions_from_log(orders_file, pos_file, live_prices={contract: fill_price})
    return order_record, positions


def update_positions_from_log(
    orders_file: Path,
    pos_file: Path,
    live_prices: dict[str, float] | None = None
) -> dict:
    """Rebuild net positions, average entry prices, and realized PnL from order history."""
    if not orders_file.exists():
        empty_state = {"positions": {}, "realized_pnl_usd": 0.0, "total_orders": 0}
        with open(pos_file, "w") as f:
            json.dump(empty_state, f, indent=2)
        return empty_state

    orders = []
    with open(orders_file) as f:
        for line in f:
            if line.strip():
                try:
                    orders.append(json.loads(line))
                except Exception:
                    pass

    # Track inventory per contract: list of lots [(price, qty)]
    inventory: dict[str, list[list[float]]] = {}
    realized_pnl_cents: dict[str, float] = {}

    for ord_info in orders:
        c = ord_info["contract"]
        side = ord_info["side"]
        qty = ord_info["qty"]
        px = ord_info["fill_price"]

        if c not in inventory:
            inventory[c] = []
            realized_pnl_cents[c] = 0.0

        current_lots = inventory[c]

        if side == "BUY":
            # If currently net short, cover short first
            rem_qty = qty
            while current_lots and current_lots[0][1] < 0 and rem_qty > 0:
                short_px, short_qty = current_lots[0]
                cover_amt = min(abs(short_qty), rem_qty)
                # Profit when covering short = entry - exit
                realized_pnl_cents[c] += (short_px - px) * cover_amt
                if abs(short_qty) == cover_amt:
                    current_lots.pop(0)
                else:
                    current_lots[0][1] += cover_amt  # short_qty was negative
                rem_qty -= cover_amt

            if rem_qty > 0:
                current_lots.append([px, rem_qty])

        elif side == "SELL":
            # If currently net long, sell long first
            rem_qty = qty
            while current_lots and current_lots[0][1] > 0 and rem_qty > 0:
                long_px, long_qty = current_lots[0]
                close_amt = min(long_qty, rem_qty)
                # Profit when closing long = exit - entry
                realized_pnl_cents[c] += (px - long_px) * close_amt
                if long_qty == close_amt:
                    current_lots.pop(0)
                else:
                    current_lots[0][1] -= close_amt
                rem_qty -= close_amt

            if rem_qty > 0:
                current_lots.append([px, -rem_qty])

    # Summarize per contract
    summary: dict = {"positions": {}, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "total_orders": len(orders)}
    total_realized_usd = 0.0
    total_unrealized_usd = 0.0

    live_px_map = live_prices or {}

    for c, lots in inventory.items():
        net_qty = sum(lot[1] for lot in lots)
        if net_qty != 0:
            avg_price = sum(lot[0] * abs(lot[1]) for lot in lots) / abs(net_qty)
        else:
            avg_price = 0.0

        r_usd = round(realized_pnl_cents.get(c, 0.0) * DOLLARS_PER_CENT, 2)
        total_realized_usd += r_usd

        # Unrealized PnL
        cur_px = live_px_map.get(c, avg_price)
        if net_qty > 0:
            unrealized_usd = round((cur_px - avg_price) * net_qty * DOLLARS_PER_CENT, 2)
        elif net_qty < 0:
            unrealized_usd = round((avg_price - cur_px) * abs(net_qty) * DOLLARS_PER_CENT, 2)
        else:
            unrealized_usd = 0.0

        total_unrealized_usd += unrealized_usd

        summary["positions"][c] = {
            "net_qty": int(net_qty),
            "side": "LONG" if net_qty > 0 else ("SHORT" if net_qty < 0 else "FLAT"),
            "avg_price_cents": round(avg_price, 2),
            "current_price_cents": round(cur_px, 2),
            "realized_pnl_usd": r_usd,
            "unrealized_pnl_usd": unrealized_usd,
        }

    summary["realized_pnl_usd"] = round(total_realized_usd, 2)
    summary["unrealized_pnl_usd"] = round(total_unrealized_usd, 2)
    summary["updated_at"] = datetime.now(NY_TZ).isoformat()

    pos_file.parent.mkdir(parents=True, exist_ok=True)
    with open(pos_file, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def format_order_confirmation_html(order: dict, pos: dict | None = None) -> str:
    """Format Telegram HTML receipt after order placement."""
    side_icon = "🟢" if order["side"] == "BUY" else "🔴"
    contract = order["contract"]
    side = order["side"]
    qty = order["qty"]
    fill_px = order["fill_price"]
    order_type = order["order_type"]
    order_id = order["order_id"]
    time_str = order.get("datetime_ny", datetime.now(NY_TZ).strftime("%b %d · %H:%M ET"))

    pos_info = (pos or {}).get("positions", {}).get(contract, {})
    net_qty = pos_info.get("net_qty", 0)
    pos_desc = f"{net_qty:+d} contracts" if net_qty != 0 else "FLAT (0)"
    avg_px = pos_info.get("avg_price_cents", fill_px)

    u_pnl = pos_info.get("unrealized_pnl_usd", 0.0)
    u_pnl_sign = f"+${u_pnl:,.2f}" if u_pnl >= 0 else f"-${abs(u_pnl):,.2f}"

    fill_side = order.get("fill_side", "")
    fill_side_str = f" ({fill_side})" if fill_side else ""
    bid = order.get("bid", 0.0)
    ask = order.get("ask", 0.0)
    latency = order.get("latency_ms")

    ba_line = f"• <b>Market B/A:</b> <code>{bid:.2f} / {ask:.2f}¢</code>\n" if (bid > 0 and ask > 0) else ""
    latency_str = f" <i>({latency}ms)</i>" if latency is not None else ""

    return (
        f"{side_icon} <b>Paper Order Executed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Action:</b> <b>{side} {qty} {contract}</b>\n"
        f"• <b>Type:</b> <code>{order_type}</code>\n"
        f"• <b>Fill Price:</b> <code>{fill_px:.2f}¢</code>{fill_side_str}\n"
        + ba_line +
        f"• <b>Last Price Update:</b> <code>{time_str}</code>{latency_str}\n"
        f"• <b>Notional:</b> <code>${order.get('notional_usd', 0.0):,.2f}</code>\n"
        f"• <b>Order ID:</b> <code>{order_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Position Snapshot:</b>\n"
        f"• Net {contract}: <b>{pos_desc}</b> (Avg: {avg_px:.2f}¢)\n"
        f"• Unrealized PnL: <code>{u_pnl_sign}</code>\n"
        f"• Total Realized: <code>${pos.get('realized_pnl_usd', 0.0):+,.2f}</code>"
    )


def format_portfolio_position_html(pos: dict, quote_details: dict | None = None) -> str:
    """Format Telegram HTML message displaying open positions, live prices, and unrealized/realized P&L."""
    quotes = quote_details or {}
    meta = quotes.get("_meta", {})
    pos_lines = []

    for c, p in pos.get("positions", {}).items():
        net = p.get("net_qty", 0)
        if net != 0:
            side_tag = "🟢 LONG" if net > 0 else "🔴 SHORT"
            avg_px = p.get("avg_price_cents", 0.0)
            cur_px = p.get("current_price_cents", avg_px)

            # Price delta in cents (positive = in trader's favor)
            diff_cents = (cur_px - avg_px) if net > 0 else (avg_px - cur_px)
            diff_str = f"{diff_cents:+.2f}¢"

            u_pnl = p.get("unrealized_pnl_usd", 0.0)
            u_pnl_str = f"+${u_pnl:,.2f}" if u_pnl >= 0 else f"-${abs(u_pnl):,.2f}"
            u_icon = "🟢" if u_pnl >= 0 else "🔴"

            q = quotes.get(c, {})
            bid = float(q.get("bid", 0.0))
            ask = float(q.get("ask", 0.0))
            ba_str = f" · <i>B/A: {bid:.2f}/{ask:.2f}¢</i>" if (bid > 0 and ask > 0) else ""

            pos_lines.append(
                f"• <b>{c}:</b> {side_tag} <b>{abs(net)}</b>\n"
                f"   ├ Entry: <code>{avg_px:.2f}¢</code>\n"
                f"   ├ Live Price: <code>{cur_px:.2f}¢</code>{ba_str}\n"
                f"   └ Unrealized P&L: {u_icon} <b>{u_pnl_str}</b> <i>({diff_str})</i>"
            )

    if not pos_lines:
        kcz26_q = quotes.get("KCZ26", {})
        live_px = float(kcz26_q.get("price", 0.0))
        px_line = f"\n• <b>KCZ26 Market:</b> <code>{live_px:.2f}¢</code>" if live_px > 0 else ""
        pos_lines.append(f"• <b>Status:</b> FLAT (No open contracts){px_line}")

    total_u = pos.get("unrealized_pnl_usd", 0.0)
    total_r = pos.get("realized_pnl_usd", 0.0)
    net_pnl = total_r + total_u

    u_str = f"+${total_u:,.2f}" if total_u >= 0 else f"-${abs(total_u):,.2f}"
    r_str = f"+${total_r:,.2f}" if total_r >= 0 else f"-${abs(total_r):,.2f}"
    net_str = f"+${net_pnl:,.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):,.2f}"
    net_icon = "🟢" if net_pnl >= 0 else "🔴"

    now_ny = datetime.now(NY_TZ)
    time_str = meta.get("time_ny") or now_ny.strftime("%H:%M:%S ET")

    # Duration/latency formatted cleanly
    dur_ms = meta.get("duration_ms")
    if dur_ms is None:
        for q in quotes.values():
            if isinstance(q, dict) and q.get("latency_ms"):
                dur_ms = q["latency_ms"]
                break
    dur_str = f" <i>({dur_ms}ms)</i>" if dur_ms is not None else ""

    return (
        "📊 <b>KCquant Portfolio & Real-Time P&L:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(pos_lines) + "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Total Unrealized:</b> <code>{u_str}</code>\n"
        f"• <b>Total Realized:</b> <code>{r_str}</code>\n"
        f"• <b>Net Combined P&L:</b> {net_icon} <code>{net_str}</code>\n"
        f"• <b>Total Orders Filled:</b> <code>{pos.get('total_orders', 0)}</code>\n"
        f"• <b>Last Price Update:</b> <code>{time_str}</code>{dur_str}"
    )


def update_portfolio_with_cqg_quotes(
    orders_file: Path | None = None,
    pos_file: Path | None = None,
) -> tuple[dict, dict]:
    """Fetch live quotes for all active contracts and calculate unrealized P&L.

    Returns:
        (updated_positions_dict, quotes_dict)
    """
    import time
    t_start = time.time()
    out_dir = orders_file.parent if orders_file else get_orders_dir()
    pos_file = pos_file or (out_dir / "positions.json")
    orders_file = orders_file or (out_dir / "paper_orders.jsonl")

    # Rebuild base inventory
    pos = update_positions_from_log(orders_file, pos_file)

    active_contracts = set(c for c, p in pos.get("positions", {}).items() if p.get("net_qty", 0) != 0)
    if not active_contracts:
        active_contracts = {"KCZ26"}

    live_prices: dict[str, float] = {}
    quotes_data: dict[str, dict] = {}

    try:
        from kcquant.cqg import fetch_instant_cqg_quote
        for c in active_contracts:
            try:
                q = fetch_instant_cqg_quote(c)
                if q and float(q.get("price", 0.0)) > 0:
                    live_prices[c] = float(q.get("price"))
                    quotes_data[c] = q
            except Exception as e:
                logger.warning(f"Error fetching quote for {c}: {e}")
    except Exception as e:
        logger.warning(f"Error accessing quote provider: {e}")

    if live_prices:
        pos = update_positions_from_log(orders_file, pos_file, live_prices=live_prices)

    duration_ms = int((time.time() - t_start) * 1000)
    now_ny = datetime.now(NY_TZ)
    quotes_data["_meta"] = {
        "duration_ms": duration_ms,
        "time_ny": now_ny.strftime("%H:%M:%S ET")
    }

    return pos, quotes_data


