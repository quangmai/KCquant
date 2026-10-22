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


def build_quick_order_keyboard(kcz26_price: float, kch27_price: float) -> dict:
    """Build the 4 quick tap order buttons requested by user."""
    h_lmt = round(kch27_price - 3.0, 2)
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🟢 BUY 1 KC Z26 Market",
                    "callback_data": f"ord:BUY:KCZ26:MKT:{kcz26_price:.2f}"
                },
                {
                    "text": f"🔵 BUY 1 KC H27 Limit ({h_lmt:.2f}¢)",
                    "callback_data": f"ord:BUY:KCH27:LMT:{h_lmt:.2f}"
                }
            ],
            [
                {
                    "text": "🔴 SELL 1 KC Z26 Market",
                    "callback_data": f"ord:SELL:KCZ26:MKT:{kcz26_price:.2f}"
                },
                {
                    "text": f"🟠 SELL 1 KC H27 Limit ({h_lmt:.2f}¢)",
                    "callback_data": f"ord:SELL:KCH27:LMT:{h_lmt:.2f}"
                }
            ]
        ]
    }


def place_paper_order(
    side: str,
    contract: str,
    order_type: str,
    qty: int = 1,
    target_price: float | None = None,
    live_price: float | None = None,
    orders_dir: Path | None = None,
) -> tuple[dict, dict]:
    """Execute and record a simulated paper order.

    Returns:
        (order_dict, updated_positions_dict)
    """
    out_dir = orders_dir or get_orders_dir()
    orders_file = out_dir / "paper_orders.jsonl"
    pos_file = out_dir / "positions.json"

    side = side.strip().upper()
    contract = contract.strip().upper()
    order_type = order_type.strip().upper()

    now_ny = datetime.now(NY_TZ)
    order_id = f"ORD-{now_ny.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    # Determine execution fill price
    if order_type == "MARKET":
        fill_price = round(live_price or target_price or 300.0, 2)
    else:  # LIMIT
        fill_price = round(target_price or live_price or 300.0, 2)

    order_record = {
        "order_id": order_id,
        "timestamp": now_ny.isoformat(),
        "datetime_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "contract": contract,
        "side": side,
        "qty": int(qty),
        "order_type": order_type,
        "limit_price": round(target_price, 2) if target_price is not None else None,
        "fill_price": fill_price,
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

    return (
        f"{side_icon} <b>Paper Order Executed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Action:</b> <b>{side} {qty} {contract}</b>\n"
        f"• <b>Type:</b> <code>{order_type}</code>\n"
        f"• <b>Fill Price:</b> <code>{fill_px:.2f}¢</code>\n"
        f"• <b>Notional:</b> <code>${order.get('notional_usd', 0.0):,.2f}</code>\n"
        f"• <b>Order ID:</b> <code>{order_id}</code>\n"
        f"• <b>Time:</b> <i>{time_str}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Position Snapshot:</b>\n"
        f"• Net {contract}: <b>{pos_desc}</b> (Avg: {avg_px:.2f}¢)\n"
        f"• Unrealized PnL: <code>{u_pnl_sign}</code>\n"
        f"• Total Realized: <code>${pos.get('realized_pnl_usd', 0.0):+,.2f}</code>"
    )
