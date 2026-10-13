"""Plot KC Z26/H27 calendar spread vs certified stocks and strategy roadmap."""
from __future__ import annotations

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from kcquant.fundamentals import load_cert_stocks

# Style constants
BLUE = "#1d63b8"
ORANGE = "#d95f02"
GREEN = "#1b9e77"
RED = "#d9383a"
PURPLE = "#7570b3"
INK = "#111111"
INK2 = "#4a4a4a"
MUTED = "#888888"
GRID = "#ebebeb"


def _style_ax(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, lw=0.8, linestyle="--", alpha=0.7)
    ax.set_axisbelow(True)


def plot_zh_strategy(output_path: Path):
    raw = pd.read_csv("output/KC_databento_fixed_raw_curve.csv")
    raw["date"] = pd.to_datetime(raw["trade_date"])
    raw = raw.set_index("date")

    cert = load_cert_stocks(Path("data/fundamentals"))

    # Focus window: July 2026 to Oct 2026
    df = raw.loc["2026-07-01":, ["KCZ26", "KCH27"]].copy()
    df["spread_ZH"] = df["KCZ26"] - df["KCH27"]
    df = df.join(cert[["certified", "pending"]])

    fig, axes = plt.subplots(
        3, 1, figsize=(13, 11), sharex=True,
        gridspec_kw={"height_ratios": [1.1, 1.4, 1.1], "hspace": 0.22}
    )
    fig.patch.set_facecolor("white")

    # 1) Outright Prices: KCZ26 vs KCH27
    ax0 = axes[0]
    _style_ax(ax0)
    ax0.plot(df.index, df["KCZ26"], color=BLUE, lw=2.2, label="KCZ26 (Dec 2026, Prompt)")
    ax0.plot(df.index, df["KCH27"], color=PURPLE, lw=1.8, linestyle="--", label="KCH27 (Mar 2027, Deferred)")
    ax0.set_ylabel("c / lb", fontsize=10, color=INK2)
    ax0.set_title("ICE Coffee 'C' Outright Futures: KCZ26 vs KCH27", loc="left", fontsize=12, fontweight="bold", color=INK)
    ax0.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="none", fontsize=9)
    ax0.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{x:.0f}¢"))

    # 2) Z/H Spread (KCZ26 - KCH27) with Strategy Targets & Ranges
    ax1 = axes[1]
    _style_ax(ax1)
    ax1.plot(df.index, df["spread_ZH"], color=INK, lw=2.5, marker="o", markersize=3.5, label="KC Z26 / H27 Calendar Spread (c/lb)")

    # Highlight zones
    latest_spread = df["spread_ZH"].dropna().iloc[-1]
    latest_date = df["spread_ZH"].dropna().index[-1]

    # Entry zone
    ax1.axhspan(8.00, 8.50, color=ORANGE, alpha=0.18, label="Current Short Entry Zone (+8.00 to +8.50¢)")
    # Target zone
    ax1.axhspan(1.50, 3.00, color=GREEN, alpha=0.20, label="Target 1 Zone (+1.50 to +3.00¢)")
    # Invalidation / Stop zone
    ax1.axhline(10.50, color=RED, linestyle=":", lw=1.8, label="Stop Loss Invalidation Level (+10.50¢)")
    ax1.axhline(0.00, color=MUTED, linestyle="-", lw=1.0)

    # Key callouts on spread
    peak_date = df["spread_ZH"].idxmax()
    peak_val = df.loc[peak_date, "spread_ZH"]
    ax1.plot([peak_date], [peak_val], "o", color=RED, markersize=7)
    ax1.annotate(
        f"Inventory Squeeze Peak: +{peak_val:.2f}¢\n(Stocks below 225k bags)",
        (peak_date, peak_val),
        xytext=(-140, -10), textcoords="offset points",
        fontsize=8.5, color=RED, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=RED, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.2", facecolor="#fff5f5", edgecolor=RED, alpha=0.9)
    )

    ax1.plot([latest_date], [latest_spread], "o", color=BLUE, markersize=8)
    ax1.annotate(
        f"Current: +{latest_spread:.2f}¢ ($3,094/ct)\n• 34 bdays to FND (Nov 19)\n• Shorts reloaded with 260k cert bags",
        (latest_date, latest_spread),
        xytext=(-180, 18), textcoords="offset points",
        fontsize=8.5, color=BLUE, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.25", facecolor="#f0f7ff", edgecolor=BLUE, alpha=0.9)
    )

    ax1.set_ylabel("Spread (c / lb)", fontsize=10, color=INK2)
    ax1.set_title("KC Z26/H27 Backwardation & Bear Spread Trading Roadmap", loc="left", fontsize=11, fontweight="bold", color=INK)
    ax1.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)
    ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{x:+.1f}¢"))

    # 3) Inventory Buffer: Certified Stocks vs Spread Compression Driver
    ax2 = axes[2]
    _style_ax(ax2)
    valid_cert = df.dropna(subset=["certified"])
    ax2.plot(valid_cert.index, valid_cert["certified"], color=BLUE, lw=2.2, label="Certified Warehouse Stocks (Bags)")
    ax2.plot(valid_cert.index, valid_cert["certified"] + valid_cert["pending"], color=PURPLE, lw=1.5, linestyle="--", label="Total Pipeline (Cert + Pending)")
    ax2.fill_between(valid_cert.index, valid_cert["certified"], valid_cert["certified"] + valid_cert["pending"], color=PURPLE, alpha=0.12)

    ax2.annotate(
        f"Certified Inflow: +42,593 bags (+19.6%)\nDirectly defused the prompt squeeze",
        (valid_cert.index[-5], valid_cert["certified"].iloc[-5]),
        xytext=(-160, -35), textcoords="offset points",
        fontsize=8.5, color=BLUE, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor=BLUE, alpha=0.9)
    )

    ax2.set_ylabel("60-kg Bags", fontsize=10, color=INK2)
    ax2.set_title("Physical Inventory Driver: Certified Stocks & Pending Grading Inflow", loc="left", fontsize=11, fontweight="bold", color=INK)
    ax2.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)
    ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):,}"))

    # Format shared x-axis
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%Y"))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=0, ha="center")

    fig.suptitle("KC Coffee 'C' Z26 / H27 Spread Assessment: Fundamental Supply Relief Trade", fontsize=13.5, fontweight="bold", color=INK, y=0.985)

    summary_banner = (
        "Strategy Thesis: Short Z26 / Long H27 Bear Spread  |  Entry: +8.00¢ to +8.50¢  |  Target 1: +2.50¢  |  Target 2: Flat (0.00¢)  |  Stop: +10.50¢\n"
        "Key Catalysts: (1) 12.6k remaining pending bags certified  (2) Index fund rolls in early Nov  (3) Tender deliveries on FND (Nov 19) by well-stocked shorts"
    )
    fig.text(
        0.5, 0.015, summary_banner, ha="center", va="bottom",
        fontsize=8.8, color=INK2,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#f3f4f6", edgecolor="#d1d5db", alpha=0.9)
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.subplots_adjust(top=0.93, bottom=0.08)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved chart to {output_path}")


if __name__ == "__main__":
    out_dir = Path("output")
    out_file = out_dir / "kc_zh_spread_strategy.png"
    plot_zh_strategy(out_file)
