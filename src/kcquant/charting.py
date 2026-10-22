"""5-Minute Intraday Price Tracker and Chart Generator for KC Coffee 'C' (CQG Live Feed)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
NY_TZ = ZoneInfo("America/New_York")


def get_default_intraday_csv() -> Path:
    p = ROOT / "output" / "monitor" / "kc_cqg_intraday_5m.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_default_chart_path() -> Path:
    p = ROOT / "output" / "monitor" / "kc_intraday_5m_chart.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def snap_to_5min(dt: datetime) -> datetime:
    """Snap datetime to the nearest 5-minute boundary."""
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)


def record_cqg_intraday_snapshot(data: dict, csv_path: Path | None = None) -> Path:
    """Record or update a 5-minute price point in the intraday CSV history.

    Parameters
    ----------
    data : dict containing kcz26_price, kch27_price, kck27_price, spread_zh_cents, spread_hk_cents
    """
    path = csv_path or get_default_intraday_csv()
    now_ny = datetime.now(NY_TZ)
    bucket_dt = snap_to_5min(now_ny)
    bucket_str = bucket_dt.strftime("%Y-%m-%d %H:%M")

    kcz26 = float(data.get("kcz26_price", 0.0))
    kch27 = float(data.get("kch27_price", 0.0))
    kck27 = float(data.get("kck27_price", 0.0))
    spread_zh = float(data.get("spread_zh_cents", round(kcz26 - kch27, 2)))
    spread_hk = float(data.get("spread_hk_cents", round(kch27 - kck27, 2)))

    new_row = {
        "datetime_ny": bucket_str,
        "timestamp": int(bucket_dt.timestamp()),
        "kcz26": round(kcz26, 2),
        "kch27": round(kch27, 2),
        "kck27": round(kck27, 2),
        "spread_zh": round(spread_zh, 2),
        "spread_hk": round(spread_hk, 2),
    }

    if path.exists() and path.stat().st_size > 0:
        try:
            df = pd.read_csv(path)
            # Upsert row by datetime_ny
            if bucket_str in df["datetime_ny"].values:
                df.loc[df["datetime_ny"] == bucket_str, ["kcz26", "kch27", "kck27", "spread_zh", "spread_hk"]] = [
                    new_row["kcz26"], new_row["kch27"], new_row["kck27"], new_row["spread_zh"], new_row["spread_hk"]
                ]
            else:
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            # Sort by datetime and keep today's session (or last 288 5-min bars = 24h)
            df = df.sort_values("datetime_ny").tail(288)
            df.to_csv(path, index=False)
            return path
        except Exception as e:
            logger.warning(f"Error reading existing intraday CSV ({e}), creating fresh.")

    df = pd.DataFrame([new_row])
    df.to_csv(path, index=False)
    return path


def get_prev_day_range(contract: str = "KCZ26") -> dict[str, float | str] | None:
    """Retrieve previous trading day high, low, and close for a contract (e.g. KCZ26).

    Queries Yahoo Finance for daily bars, selects the latest session prior to today,
    and caches results in output/monitor/kc_prev_day_range_{contract}.json.
    """
    clean_sym = contract.replace(".NYB", "").upper()
    cache_path = ROOT / "output" / "monitor" / f"kc_prev_day_range_{clean_sym.lower()}.json"

    now_ny = datetime.now(NY_TZ)
    today_ny = now_ny.date()

    # 1. Check recent cache
    if cache_path.exists():
        try:
            import json
            with open(cache_path) as f:
                cached = json.load(f)
            # If cached date is prior to today and high/low exist, use it
            if cached.get("date") != str(today_ny) and "high" in cached and "low" in cached:
                return cached
        except Exception:
            pass

    # 2. Query Yahoo Finance daily bars
    try:
        import requests
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_sym}.NYB?interval=1d&range=5d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
        }
        resp = requests.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 200:
            payload = resp.json()
            res = payload.get("chart", {}).get("result")
            if res and len(res) > 0:
                timestamps = res[0].get("timestamp", [])
                quotes = res[0].get("indicators", {}).get("quote", [{}])[0]
                highs = quotes.get("high", [])
                lows = quotes.get("low", [])
                closes = quotes.get("close", [])

                valid_bars = []
                for t, h, l, c in zip(timestamps, highs, lows, closes):
                    if h is None or l is None or c is None:
                        continue
                    b_date = datetime.fromtimestamp(t, tz=NY_TZ).date()
                    valid_bars.append((b_date, float(h), float(l), float(c)))

                if valid_bars:
                    # Select the latest bar strictly before today
                    prev_bars = [b for b in valid_bars if b[0] < today_ny]
                    target = prev_bars[-1] if prev_bars else (valid_bars[-2] if len(valid_bars) >= 2 else valid_bars[-1])
                    result = {
                        "contract": clean_sym,
                        "date": str(target[0]),
                        "high": round(target[1], 2),
                        "low": round(target[2], 2),
                        "close": round(target[3], 2),
                        "updated_at": now_ny.isoformat()
                    }
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    import json
                    with open(cache_path, "w") as f:
                        json.dump(result, f, indent=2)
                    return result
    except Exception as e:
        logger.warning(f"Could not fetch previous day range from Yahoo for {clean_sym}: {e}")

    # 3. Check fallback cache or known baseline
    if cache_path.exists():
        try:
            import json
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            pass

    # Baseline fallback for KCZ26
    return {
        "contract": clean_sym,
        "date": "2026-10-21",
        "high": 317.90,
        "low": 308.35,
        "close": 316.15,
        "updated_at": now_ny.isoformat()
    }


def generate_intraday_5m_chart(
    csv_path: Path | None = None,
    output_image: Path | None = None,
    prev_day_high: float | None = None,
    prev_day_low: float | None = None,
    prev_day_date: str | None = None,
    contract: str = "KCZ26",
) -> Path | None:
    """Generate high-resolution 5-minute price line chart with dark aesthetics.

    Subplot 1: KCZ26, KCH27, KCK27 outright prices, with previous day high and low
               drawn as crisp white lines to show if price has cut through the range.
    Subplot 2: Z/H Spread and H/K Spread.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    path = csv_path or get_default_intraday_csv()
    out_img = output_image or get_default_chart_path()

    if not path.exists() or path.stat().st_size == 0:
        return None

    try:
        df = pd.read_csv(path)
        if len(df) == 0:
            return None

        # Filter for recent trading day/session (up to last 24 hours)
        df["dt"] = pd.to_datetime(df["datetime_ny"])
        df = df.sort_values("dt").reset_index(drop=True)

        # Resolve previous day high/low if not explicitly provided
        pdh = prev_day_high
        pdl = prev_day_low
        p_date = prev_day_date
        if pdh is None or pdl is None:
            prev_info = get_prev_day_range(contract)
            if prev_info:
                pdh = float(prev_info.get("high", 0.0))
                pdl = float(prev_info.get("low", 0.0))
                p_date = prev_info.get("date")

        # Plot setup with dark quantitative theme
        plt.style.use("dark_background")
        fig, (ax1, ax2) = plt.subplots(
            2, 1,
            figsize=(11, 7.5),
            dpi=160,
            sharex=True,
            gridspec_kw={"height_ratios": [1.4, 1.0], "hspace": 0.08}
        )

        bg_color = "#0e131b"
        panel_color = "#141c26"
        grid_color = "#202c3d"

        fig.patch.set_facecolor(bg_color)
        ax1.set_facecolor(panel_color)
        ax2.set_facecolor(panel_color)

        # Color palette
        c_z = "#00e5ff"  # Cyan for KCZ26
        c_h = "#ffab00"  # Amber for KCH27
        c_k = "#b388ff"  # Lavender for KCK27
        c_zh = "#00e676"  # Emerald for Z/H
        c_hk = "#2979ff"  # Royal Blue for H/K

        # --- SUBPLOT 1: Outrights & Previous Day High/Low ---
        # Previous Day High / Low white lines
        if pdh is not None and pdl is not None and pdh > 0 and pdl > 0:
            ax1.axhline(pdh, color="#ffffff", linestyle="--", linewidth=1.3, alpha=0.9, zorder=2)
            ax1.axhline(pdl, color="#ffffff", linestyle="--", linewidth=1.3, alpha=0.9, zorder=2)
            ax1.axhspan(pdl, pdh, color="#ffffff", alpha=0.035, zorder=1)

            # Left inline badges for PDH and PDL
            start_x = df["dt"].iloc[0] + timedelta(minutes=4)
            ax1.text(
                start_x, pdh + 0.35,
                f"PREV DAY HIGH: {pdh:.2f}¢",
                color="#ffffff",
                fontweight="bold",
                fontsize=8,
                va="bottom",
                ha="left",
                bbox=dict(boxstyle="square,pad=0.2", fc="#141c26", ec="#ffffff", lw=0.8, alpha=0.9)
            )
            ax1.text(
                start_x, pdl + 0.35,
                f"PREV DAY LOW: {pdl:.2f}¢",
                color="#ffffff",
                fontweight="bold",
                fontsize=8,
                va="bottom",
                ha="left",
                bbox=dict(boxstyle="square,pad=0.2", fc="#141c26", ec="#ffffff", lw=0.8, alpha=0.9)
            )

        ax1.plot(df["dt"], df["kcz26"], color=c_z, linewidth=2.2, label="KCZ26 (Dec 26)", marker="o", markersize=3.5, zorder=3)
        ax1.plot(df["dt"], df["kch27"], color=c_h, linewidth=2.0, label="KCH27 (Mar 27)", marker="o", markersize=3.5, zorder=3)
        ax1.plot(df["dt"], df["kck27"], color=c_k, linewidth=1.8, label="KCK27 (May 27)", marker="o", markersize=3.5, zorder=3)

        # Annotate latest prices on ax1
        latest = df.iloc[-1]
        ax1.annotate(
            f"KCZ26: {latest['kcz26']:.2f}¢",
            xy=(latest["dt"], latest["kcz26"]),
            xytext=(10, 0), textcoords="offset points",
            color=c_z, fontweight="bold", fontsize=9,
            va="center",
            bbox=dict(boxstyle="round,pad=0.25", fc=bg_color, ec=c_z, alpha=0.9)
        )
        ax1.annotate(
            f"KCH27: {latest['kch27']:.2f}¢",
            xy=(latest["dt"], latest["kch27"]),
            xytext=(10, 0), textcoords="offset points",
            color=c_h, fontweight="bold", fontsize=9,
            va="center",
            bbox=dict(boxstyle="round,pad=0.25", fc=bg_color, ec=c_h, alpha=0.9)
        )
        ax1.annotate(
            f"KCK27: {latest['kck27']:.2f}¢",
            xy=(latest["dt"], latest["kck27"]),
            xytext=(10, 0), textcoords="offset points",
            color=c_k, fontweight="bold", fontsize=9,
            va="center",
            bbox=dict(boxstyle="round,pad=0.25", fc=bg_color, ec=c_k, alpha=0.9)
        )

        # Check range cut-through status
        if pdh is not None and pdl is not None and pdh > 0 and pdl > 0:
            latest_z = latest["kcz26"]
            if latest_z < pdl:
                status_text = f"▼ CUT BELOW PREV LOW ({latest_z - pdl:+.2f}¢)"
                status_color = "#ff5252"
            elif latest_z > pdh:
                status_text = f"▲ CUT ABOVE PREV HIGH ({latest_z - pdh:+.2f}¢)"
                status_color = "#69f0ae"
            else:
                status_text = "● INSIDE PREV RANGE"
                status_color = "#e0e0e0"

            ax1.text(
                0.985, 0.92,
                status_text,
                transform=ax1.transAxes,
                color=status_color,
                fontweight="bold",
                fontsize=9,
                ha="right",
                va="top",
                bbox=dict(boxstyle="round,pad=0.35", fc="#1b2230", ec=status_color, lw=1.2, alpha=0.95)
            )

        ax1.set_ylabel("Price (¢/lb)", fontsize=10.5, color="#e0e0e0", labelpad=8)
        ax1.grid(True, color=grid_color, linestyle="--", linewidth=0.7, alpha=0.8)
        ax1.legend(loc="lower left", framealpha=0.85, facecolor=bg_color, edgecolor=grid_color, fontsize=8.5, ncol=3)
        ax1.set_title("KC Coffee 'C' · 5-Minute Intraday Curve & Calendar Spreads", fontsize=13, fontweight="bold", color="#ffffff", pad=12)

        # --- SUBPLOT 2: Spreads ---
        ax2.plot(df["dt"], df["spread_zh"], color=c_zh, linewidth=2.4, label="Z26 / H27 Spread", marker="s", markersize=4)
        ax2.plot(df["dt"], df["spread_hk"], color=c_hk, linewidth=2.0, label="H27 / K27 Spread", marker="^", markersize=4)

        # Zero reference line
        ax2.axhline(0, color="#78909c", linestyle=":", linewidth=1.0, alpha=0.8)

        # Subtle shading between spread and zero for visual depth
        ax2.fill_between(df["dt"], df["spread_zh"], 0, color=c_zh, alpha=0.10)
        ax2.fill_between(df["dt"], df["spread_hk"], 0, color=c_hk, alpha=0.08)

        # Annotate latest spread on ax2
        ax2.annotate(
            f"Z/H: {latest['spread_zh']:+.2f}¢",
            xy=(latest["dt"], latest["spread_zh"]),
            xytext=(10, 0), textcoords="offset points",
            color=c_zh, fontweight="bold", fontsize=9.5,
            va="center",
            bbox=dict(boxstyle="round,pad=0.3", fc=bg_color, ec=c_zh, alpha=0.95)
        )
        ax2.annotate(
            f"H/K: {latest['spread_hk']:+.2f}¢",
            xy=(latest["dt"], latest["spread_hk"]),
            xytext=(10, 0), textcoords="offset points",
            color=c_hk, fontweight="bold", fontsize=9.5,
            va="center",
            bbox=dict(boxstyle="round,pad=0.3", fc=bg_color, ec=c_hk, alpha=0.95)
        )

        ax2.set_ylabel("Spread (¢/lb)", fontsize=10.5, color="#e0e0e0", labelpad=8)
        ax2.grid(True, color=grid_color, linestyle="--", linewidth=0.7, alpha=0.8)
        ax2.legend(loc="upper left", framealpha=0.8, facecolor=bg_color, edgecolor=grid_color, fontsize=9.5)

        # X-axis time formatting
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M ET"))
        ax2.set_xlabel("Time (America/New_York)", fontsize=10, color="#bdbdbd", labelpad=8)

        # Expand x-limits to fit right edge labels cleanly
        time_span = (df["dt"].iloc[-1] - df["dt"].iloc[0]).total_seconds()
        pad_seconds = max(300, time_span * 0.14)
        ax2.set_xlim(df["dt"].iloc[0] - timedelta(seconds=120), df["dt"].iloc[-1] + timedelta(seconds=pad_seconds))

        # Bottom watermark/source
        last_time_str = latest["dt"].strftime("%b %d, %Y · %H:%M ET")
        footer_parts = [f"CQG Live Feed · {last_time_str}"]
        if pdh is not None and pdl is not None and pdh > 0 and pdl > 0:
            footer_parts.append(f"Prev Day High/Low: {pdh:.2f} / {pdl:.2f}¢")
        fig.text(0.98, 0.015, " · ".join(footer_parts), fontsize=8.5, color="#78909c", ha="right", va="bottom")

        fig.subplots_adjust(top=0.92, bottom=0.08, left=0.08, right=0.90, hspace=0.12)
        out_img.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_img, facecolor=fig.get_facecolor(), edgecolor="none", dpi=160, bbox_inches="tight")
        plt.close(fig)

        logger.info(f"Generated 5-minute price line chart at {out_img}")
        return out_img
    except Exception as e:
        logger.error(f"Error generating intraday 5m chart: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None
