"""Charts. Static PNG via matplotlib."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def _end_label(ax, x, y, text, color):
    ax.plot([x], [y], "o", ms=4, color=color)
    ax.annotate(text, (x, y), xytext=(6, 0), textcoords="offset points",
                va="center", fontsize=9, color=INK2)


def plot_overview(daily: pd.DataFrame, decomp: pd.DataFrame, feats: pd.DataFrame,
                  title: str, path: str, subtitle: str = "", price_unit: str = "cents / lb") -> str:
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True,
                             gridspec_kw=dict(height_ratios=[1.3, 1, 1], hspace=0.28))
    fig.patch.set_facecolor("white")

    # 1) continuous series
    ax = axes[0]; _style(ax)
    ax.plot(decomp.index, decomp["spot_price"], color=MUTED, lw=1.2, label="Unadjusted nearby (spot proxy)")
    ax.plot(daily.index, daily["backadj_price"], color=BLUE, lw=2, label="Ratio back-adjusted continuous")
    rolls = daily.index[daily["rolled_at_settle"]]
    ymin = ax.get_ylim()[0]
    ax.plot(rolls, [ymin] * len(rolls), "|", color=INK2, ms=8, label="Roll dates")
    ax.set_ylabel(price_unit, color=INK2)
    ax.set_title("Continuous series", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=9, loc="upper left")

    # 2) cumulative decomposition
    ax = axes[1]; _style(ax)
    last = decomp.index[-1]
    series = [("cum_total_log", "Rolled return", BLUE), ("cum_spot_log", "Spot return", ORANGE),
              ("cum_roll_yield_log", "Roll yield", AQUA)]
    for col, lab, colr in series:
        ax.plot(decomp.index, 100 * decomp[col], color=colr, lw=2, label=lab)
    # end labels, nudged apart so close values don't overlap
    ends = sorted(((100 * decomp[c].iloc[-1], lab, colr) for c, lab, colr in series), key=lambda t: t[0])
    lo, hi = ax.get_ylim()
    gap, placed = 0.07 * (hi - lo), []
    for y, lab, colr in ends:
        ty = max(y, placed[-1] + gap) if placed else y
        placed.append(ty)
        ax.plot([last], [y], "o", ms=4, color=colr)
        ax.annotate(f"{lab} {y:+.0f}%", (last, y), xytext=(8, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK2, annotation_clip=False)
        ax.texts[-1].xyann = (8, (ty - y) / (hi - lo) * ax.bbox.height * 72 / ax.figure.dpi)
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.set_ylabel("cumulative log return, %", color=INK2)
    ax.set_title("Return decomposition: rolled = spot + roll yield", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=9, loc="upper left", ncol=3)

    # 3) roll yield over time
    ax = axes[2]; _style(ax)
    carry = 100 * feats["carry_12_ann"]
    ax.fill_between(carry.index, 0, carry, where=carry >= 0, color=BLUE, alpha=0.25, lw=0,
                    label="Backwardation (positive carry)")
    ax.fill_between(carry.index, 0, carry, where=carry < 0, color=ORANGE, alpha=0.25, lw=0,
                    label="Contango (negative carry)")
    ax.plot(carry.index, carry, color=INK2, lw=0.8, label="Ex-ante carry, c1→c2 annualised")
    ax.plot(decomp.index, 100 * decomp["roll_yield_1y"], color=AQUA, lw=2, label="Realised roll yield, trailing 1y")
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.set_ylabel("% per year", color=INK2)
    ax.set_title("Roll yield over time", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8.5, loc="lower right", bbox_to_anchor=(1, 0.98), ncol=2)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    fig.suptitle(title, x=0.06, ha="left", fontsize=14, color=INK, y=0.98)
    if subtitle:
        fig.text(0.06, 0.945, subtitle, fontsize=9.5, color=INK2)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_backtest(nets: pd.DataFrame, path: str, subtitle: str = "") -> str:
    """Cumulative net returns of sleeves vs portfolio and buy-and-hold, plus portfolio drawdown."""
    import numpy as np
    fig, axes = plt.subplots(2, 1, figsize=(12, 8.5), sharex=True,
                             gridspec_kw=dict(height_ratios=[2.2, 1], hspace=0.25))
    fig.patch.set_facecolor("white")
    cum = np.log1p(nets).cumsum() * 100
    ax = axes[0]; _style(ax)
    styles = {"carry_KC": (BLUE, "-"), "carry_RC": (BLUE, "--"), "trend_KC": (ORANGE, "-"),
              "trend_RC": (ORANGE, "--"), "spread_KC_RC": (AQUA, "-")}
    for k, (c, ls) in styles.items():
        if k in cum:
            ax.plot(cum.index, cum[k], color=c, ls=ls, lw=1.4, label=k)
    for k in ["long_KC", "long_RC"]:
        if k in cum:
            ax.plot(cum.index, cum[k], color=MUTED, lw=1.0, ls="-" if k.endswith("KC") else ":",
                    label=f"{k} (benchmark)")
    ax.plot(cum.index, cum["PORTFOLIO"], color=INK, lw=2.4, label="PORTFOLIO (equal risk)")
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.set_ylabel("cumulative net log return, %", color=INK2)
    ax.set_title("Strategy sleeves vs buy-and-hold (each sleeve targets 10% vol)", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left", ncol=2)

    ax = axes[1]; _style(ax)
    eq = (1 + nets["PORTFOLIO"]).cumprod()
    dd = (eq / eq.cummax() - 1) * 100
    ax.fill_between(dd.index, dd, 0, color=INK2, alpha=0.25, lw=0)
    ax.plot(dd.index, dd, color=INK2, lw=0.8)
    ax.set_ylabel("drawdown, %", color=INK2)
    ax.set_title("Portfolio drawdown", loc="left", fontsize=11, color=INK)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.suptitle("Coffee futures signals — backtest (research, not a recommendation)", x=0.06, ha="left",
                 fontsize=14, color=INK, y=0.98)
    if subtitle:
        fig.text(0.06, 0.945, subtitle, fontsize=9.5, color=INK2)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path
