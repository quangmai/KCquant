"""Plot ICE Coffee 'C' grading pass rate evaluation, weekly volumes, and origin breakdown."""
from __future__ import annotations

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from kcquant.fundamentals import load_cert_stocks

# Palette
BLUE = "#1d63b8"
GREEN = "#1b9e77"
RED = "#d9383a"
ORANGE = "#d95f02"
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


def plot_pass_rate(output_path: Path):
    df, detail = load_cert_stocks(Path("data/fundamentals"), with_detail=True)

    fig, axes = plt.subplots(
        2, 2, figsize=(14, 10),
        gridspec_kw={"height_ratios": [1.1, 1.0], "wspace": 0.22, "hspace": 0.28}
    )
    fig.patch.set_facecolor("white")

    # -------------------------------------------------------------
    # 1) Top-Left: Weekly Graded Volume & Pass Rate Trend
    # -------------------------------------------------------------
    ax0 = axes[0, 0]
    _style_ax(ax0)
    df_wk = df.copy()
    df_wk["week_start"] = df_wk.index.to_period("W").start_time
    wk = df_wk.groupby("week_start")[["passed", "failed"]].sum()
    wk = wk[(wk["passed"] + wk["failed"]) > 0]
    wk["total"] = wk["passed"] + wk["failed"]
    wk["pass_rate"] = wk["passed"] / wk["total"] * 100

    dates_num = mdates.date2num(wk.index)
    bar_width = 3.5

    ax0.bar(dates_num - bar_width/2, wk["passed"], width=bar_width, color=GREEN, alpha=0.85, label="Passed (Bags)")
    ax0.bar(dates_num + bar_width/2, wk["failed"], width=bar_width, color=RED, alpha=0.75, label="Failed (Bags)")
    ax0.set_ylabel("Volume Graded (Bags)", fontsize=10, color=INK2)
    ax0.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):,}"))

    # Line for pass rate on twin axis
    ax0_twin = ax0.twinx()
    ax0_twin.spines["top"].set_visible(False)
    ax0_twin.spines["left"].set_visible(False)
    ax0_twin.spines["right"].set_color(BLUE)
    ax0_twin.plot(dates_num, wk["pass_rate"], color=BLUE, lw=2.4, marker="o", markersize=6, label="Pass Rate (%)")
    ax0_twin.set_ylabel("Pass Rate (%)", fontsize=10, color=BLUE)
    ax0_twin.set_ylim(0, 100)
    ax0_twin.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{x:.0f}%"))

    # Format x-axis
    ax0.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax0.set_title("Weekly Grading Activity & Pass Rate Progression", loc="left", fontsize=11, fontweight="bold", color=INK)

    # Legends
    l1, lab1 = ax0.get_legend_handles_labels()
    l2, lab2 = ax0_twin.get_legend_handles_labels()
    ax0.legend(l1 + l2, lab1 + lab2, loc="upper left", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)

    # -------------------------------------------------------------
    # 2) Top-Right: Monthly Pass Rate Comparison
    # -------------------------------------------------------------
    ax1 = axes[0, 1]
    _style_ax(ax1)
    df_m = df.copy()
    df_m["month_name"] = df_m.index.strftime("%B %Y")
    m = df_m.groupby("month_name", sort=False)[["passed", "failed"]].sum()
    m["total"] = m["passed"] + m["failed"]
    m["pass_rate"] = m["passed"] / m["total"] * 100

    months = list(m.index)
    x_pos = np.arange(len(months))
    colors = [ORANGE, GREEN, BLUE]

    bars = ax1.bar(x_pos, m["pass_rate"], color=colors, alpha=0.85, width=0.55)
    ax1.set_xticks(x_pos)
    ax1.set_xticklabels(months, fontsize=9.5)
    ax1.set_ylabel("Pass Rate (%)", fontsize=10, color=INK2)
    ax1.set_ylim(0, 100)
    ax1.axhline(65.6, color=MUTED, linestyle=":", lw=1.2, label="Overall Avg: 65.6%")

    for b, rate, tot in zip(bars, m["pass_rate"], m["total"]):
        ax1.annotate(
            f"{rate:.1f}%\n({int(tot):,} bags)",
            (b.get_x() + b.get_width() / 2, b.get_height() - 14),
            ha="center", va="center", color="white", fontweight="bold", fontsize=9
        )
    ax1.set_title("Monthly Pass Rate: August Drought vs Sep Surge vs Oct", loc="left", fontsize=11, fontweight="bold", color=INK)
    ax1.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)

    # -------------------------------------------------------------
    # 3) Bottom-Left: Breakdown by Country of Origin
    # -------------------------------------------------------------
    ax2 = axes[1, 0]
    _style_ax(ax2)
    p_orig = detail[detail["section"] == "passed"].groupby("origin")["bags"].sum()
    f_orig = detail[detail["section"] == "failed"].groupby("origin")["bags"].sum()
    orig = pd.DataFrame({"passed": p_orig, "failed": f_orig}).fillna(0)
    orig["total"] = orig["passed"] + orig["failed"]
    orig = orig.sort_values("total", ascending=True)

    y_pos = np.arange(len(orig))
    ax2.barh(y_pos, orig["passed"], color=GREEN, alpha=0.85, label="Passed Bags")
    ax2.barh(y_pos, orig["failed"], left=orig["passed"], color=RED, alpha=0.75, label="Failed Bags")
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels(orig.index, fontsize=9.5)
    ax2.set_xlabel("Graded Volume (Bags)", fontsize=10, color=INK2)
    ax2.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{int(x):,}"))
    ax2.set_title("Grading Volume & Outcome by Country of Origin", loc="left", fontsize=11, fontweight="bold", color=INK)
    ax2.legend(loc="lower right", frameon=True, facecolor="white", edgecolor="none", fontsize=8.5)

    # Add Brazil callout
    br_rate = orig.loc["Brazil", "passed"] / orig.loc["Brazil", "total"] * 100
    ax2.annotate(
        f"Brazil: 96.4% of all coffee\n66.2% Pass Rate",
        (orig.loc["Brazil", "passed"] / 2, y_pos[-1]),
        ha="center", va="center", color="white", fontweight="bold", fontsize=9
    )

    # -------------------------------------------------------------
    # 4) Bottom-Right: Pending Queue Conversion Projection
    # -------------------------------------------------------------
    ax3 = axes[1, 1]
    _style_ax(ax3)

    latest_pending = float(df.iloc[-1]["pending"])
    cur_cert = float(df.iloc[-1]["certified"])

    scenarios = [
        ("Conservative (Oct 54%)", 0.540, RED),
        ("Baseline (Average 66%)", 0.656, BLUE),
        ("Optimistic (Sep 72%)", 0.717, GREEN),
    ]

    labels = [s[0] for s in scenarios]
    passed_proj = [latest_pending * s[1] for s in scenarios]
    final_cert_proj = [cur_cert + p for p in passed_proj]

    x_proj = np.arange(len(scenarios))
    bars3 = ax3.bar(x_proj, passed_proj, color=[s[2] for s in scenarios], alpha=0.85, width=0.5)
    ax3.set_xticks(x_proj)
    ax3.set_xticklabels(labels, fontsize=8.5)
    ax3.set_ylabel("Expected New Certified Bags", fontsize=10, color=INK2)
    ax3.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"+{int(x):,}"))
    ax3.set_title(f"Projected Inflow from Current Pending ({latest_pending:,.0f} Bags)", loc="left", fontsize=11, fontweight="bold", color=INK)

    for b, p, fc in zip(bars3, passed_proj, final_cert_proj):
        ax3.annotate(
            f"+{int(p):,} bags\n(Total: {int(fc):,})",
            (b.get_x() + b.get_width() / 2, b.get_height() / 2),
            ha="center", va="center", color="white", fontweight="bold", fontsize=9
        )

    fig.suptitle("ICE Coffee 'C' Grading Pass Rate Comprehensive Evaluation", fontsize=14, fontweight="bold", color=INK, y=0.985)
    plt.subplots_adjust(top=0.92, bottom=0.08)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved pass rate chart to {output_path}")


if __name__ == "__main__":
    out_dir = Path("output")
    out_file = out_dir / "grading_pass_rate_evaluation.png"
    plot_pass_rate(out_file)
