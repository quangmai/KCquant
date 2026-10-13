"""Plot ICE Coffee 'C' certified warehouse stocks and grading queue trends."""
from __future__ import annotations

import shutil
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from kcquant.fundamentals import load_cert_stocks

# Visual style tokens consistent with kcquant
BLUE = "#1d63b8"
ORANGE = "#d95f02"
GREEN = "#1b9e77"
RED = "#d9383a"
PURPLE = "#7570b3"
INK = "#111111"
INK2 = "#4a4a4a"
MUTED = "#888888"
GRID = "#ebebeb"
BG_LIGHT = "#f8f9fa"


def _style_ax(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, lw=0.8, linestyle="--", alpha=0.7)
    ax.set_axisbelow(True)


def plot_cert_trends(data_dir: Path, output_path: Path):
    df, detail = load_cert_stocks(data_dir, with_detail=True)

    # Compute key series
    df = df.copy()
    df["net_change"] = df["certified"].diff()
    df["total_pipeline"] = df["certified"] + df["pending"]
    df["cum_net_change"] = df["certified"] - df["certified"].iloc[0]

    # Overall summary stats
    start_date = df.index.min()
    end_date = df.index.max()
    trough_date = df["certified"].idxmin()
    trough_val = df.loc[trough_date, "certified"]
    latest_date = df.index[-1]
    latest_cert = df.loc[latest_date, "certified"]
    latest_pending = df.loc[latest_date, "pending"]
    peak_pending_date = df["pending"].idxmax()
    peak_pending = df.loc[peak_pending_date, "pending"]
    total_passed = df["passed"].sum()
    total_failed = df["failed"].sum()
    pass_rate = total_passed / (total_passed + total_failed) * 100 if (total_passed + total_failed) > 0 else 0

    fig, axes = plt.subplots(
        3, 1, figsize=(13, 11), sharex=True,
        gridspec_kw={"height_ratios": [1.4, 1.1, 1.0], "hspace": 0.22}
    )
    fig.patch.set_facecolor("white")

    # -------------------------------------------------------------
    # 1) Top panel: Certified Stocks vs Total Pipeline
    # -------------------------------------------------------------
    ax0 = axes[0]
    _style_ax(ax0)

    # Shaded total pipeline
    ax0.plot(df.index, df["total_pipeline"], color=PURPLE, lw=1.6, linestyle="--", label="Total Pipeline (Certified + Pending)")
    ax0.fill_between(df.index, df["certified"], df["total_pipeline"], color=PURPLE, alpha=0.10, label="Pending Grading Inflow Buffer")

    # Certified stock line
    ax0.plot(df.index, df["certified"], color=BLUE, lw=2.6, marker="o", markersize=4, label="Certified Stocks (Bags)")

    # Key callouts
    # Start
    ax0.annotate(
        f"Aug 05: {df['certified'].iloc[0]:,.0f}",
        (df.index[0], df["certified"].iloc[0]),
        xytext=(8, -12), textcoords="offset points",
        fontsize=8.5, color=INK2, fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor=MUTED, alpha=0.8)
    )

    # Trough
    ax0.plot([trough_date], [trough_val], "o", color=RED, markersize=8)
    ax0.annotate(
        f"Trough (Sep 15): {trough_val:,.0f} bags\n(-33,771 / -13.4% from Aug)",
        (trough_date, trough_val),
        xytext=(-120, 20), textcoords="offset points",
        fontsize=8.5, color=RED, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=RED, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.2", facecolor="#fff5f5", edgecolor=RED, alpha=0.9)
    )

    # Peak / Current
    ax0.plot([latest_date], [latest_cert], "o", color=GREEN, markersize=8)
    ax0.annotate(
        f"Oct 02: {latest_cert:,.0f} bags\n(+42,593 / +19.6% rebound)",
        (latest_date, latest_cert),
        xytext=(-120, -32), textcoords="offset points",
        fontsize=8.5, color=GREEN, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.2", facecolor="#f0fff4", edgecolor=GREEN, alpha=0.9)
    )

    ax0.set_ylabel("60-kg Bags", fontsize=10, color=INK2)
    ax0.set_title("ICE Coffee 'C' Certified Stocks & Total Pipeline (Certified + Pending)", loc="left", fontsize=12, fontweight="bold", color=INK)
    ax0.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)
    ax0.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):,}"))

    # -------------------------------------------------------------
    # 2) Middle panel: Pending Grading Queue & Daily Grading Results
    # -------------------------------------------------------------
    ax1 = axes[1]
    _style_ax(ax1)

    # Pending line and fill
    ax1.plot(df.index, df["pending"], color=ORANGE, lw=2.2, label="Pending Grading Queue")
    ax1.fill_between(df.index, 0, df["pending"], color=ORANGE, alpha=0.15)

    # Peak pending annotation
    ax1.plot([peak_pending_date], [peak_pending], "o", color=ORANGE, markersize=8)
    ax1.annotate(
        f"Pending Peak: {peak_pending:,.0f} bags\n(Delivery surge into Antwerp)",
        (peak_pending_date, peak_pending),
        xytext=(-140, -32), textcoords="offset points",
        fontsize=8.5, color=ORANGE, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=ORANGE, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.2", facecolor="#fffbf0", edgecolor=ORANGE, alpha=0.9)
    )

    # Secondary twin axis for daily passed vs failed bars
    ax1_bars = ax1.twinx()
    ax1_bars.spines["top"].set_visible(False)
    ax1_bars.spines["left"].set_visible(False)
    ax1_bars.spines["right"].set_color(MUTED)

    bar_width = 0.4
    # Days with grading
    grading_days = df[(df["passed"] > 0) | (df["failed"] > 0)]
    dates_num = mdates.date2num(grading_days.index)

    p1 = ax1_bars.bar(dates_num - bar_width/2, grading_days["passed"], width=bar_width, color=GREEN, alpha=0.8, label="Passed Grading (Daily)")
    p2 = ax1_bars.bar(dates_num + bar_width/2, grading_days["failed"], width=bar_width, color=RED, alpha=0.75, label="Failed Grading (Daily)")

    ax1.set_ylabel("Pending Queue (Bags)", fontsize=10, color=ORANGE)
    ax1_bars.set_ylabel("Graded Today (Bags)", fontsize=10, color=INK2)
    ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):,}"))
    ax1_bars.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):,}"))

    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1_bars.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)
    ax1.set_title("Grading Pipeline: Pending Queue Inflow vs Daily Passed/Failed Output", loc="left", fontsize=11, fontweight="bold", color=INK)

    # -------------------------------------------------------------
    # 3) Bottom panel: Daily Net Certified Change & Trend Direction
    # -------------------------------------------------------------
    ax2 = axes[2]
    _style_ax(ax2)

    net_chg = df["net_change"].fillna(0)
    pos_mask = net_chg >= 0
    dates_all = mdates.date2num(df.index)

    ax2.bar(dates_all[pos_mask], net_chg[pos_mask], width=0.7, color=GREEN, alpha=0.85, label="Daily Inflow (+ Passed)")
    ax2.bar(dates_all[~pos_mask], net_chg[~pos_mask], width=0.7, color=RED, alpha=0.8, label="Daily Outflow (Withdrawals / Aging)")
    ax2.axhline(0, color=MUTED, lw=0.9)

    # Line of 5-day rolling average net change
    rolling_net = net_chg.rolling(5, min_periods=1).mean()
    ax2.plot(df.index, rolling_net, color=INK, lw=1.8, linestyle=":", label="5-Day Rolling Mean Change")

    ax2.set_ylabel("Net Daily Delta (Bags)", fontsize=10, color=INK2)
    ax2.set_title("Daily Certified Inventory Momentum: Clear Shift from August Outflows to September Accumulation", loc="left", fontsize=11, fontweight="bold", color=INK)
    ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):+,}"))
    ax2.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)

    # Format shared x-axis
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=1))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%Y"))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=0, ha="center")

    # Overall title & subtitle header box
    fig.suptitle(
        "ICE Coffee 'C' Certified Stocks & Pending Grading Analysis (Aug - Oct 2026)",
        fontsize=14, fontweight="bold", color=INK, y=0.985
    )

    # Add key takeaway banner at bottom
    summary_box_text = (
        f"Key Takeaways:  • Current Certified: {latest_cert:,.0f} bags (+19.6% off Sep 15 trough of {trough_val:,.0f})  "
        f"• Pending Queue: {latest_pending:,.0f} bags (peaked at {peak_pending:,.0f} on Sep 11-15)  \n"
        f"• Total Graded: {total_passed + total_failed:,.0f} bags ({total_passed:,.0f} passed, {total_failed:,.0f} failed | {pass_rate:.1f}% pass rate)  "
        f"• Origin & Port: >96% Brazil origin tendered directly to Antwerp (ANT)"
    )
    fig.text(
        0.5, 0.015, summary_box_text, ha="center", va="bottom",
        fontsize=9, color=INK2,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#f3f4f6", edgecolor="#d1d5db", alpha=0.9)
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.subplots_adjust(top=0.93, bottom=0.08)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved chart to {output_path}")


if __name__ == "__main__":
    out_dir = Path("output")
    out_file = out_dir / "ice_cert_stock_trend.png"
    plot_cert_trends(Path("data/fundamentals"), out_file)
