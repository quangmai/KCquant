"""Point-in-time backtester for futures signals.

Timing (one market):
    decision on trade date d, at spec.decision_time (before the close)
        uses only rows whose available_at <= decision time  (pit.align_asof)
    -> target weight w[d], traded at the settlement of d (e.g. TAS)
    -> held over (settle d, settle d+1], earning r[d+1] from the roll engine
So the weight that earns r[t] is w[t-1]. With snapshot timestamps (18:00 NY /
23:00 London), a decision on d sees settles up to d-1: a signal acts with a
lag of about 1.5 sessions, which is realistic rather than optimistic.

Sizing: w = signal * target_vol / vol_estimate, capped at +/- max_leverage,
where vol_estimate is an EWMA of the rolled returns, itself point-in-time.
Weights are notional exposure as a fraction of capital.

Costs: each traded unit of notional pays `ticks_per_side` ticks of slippage +
commission at the held contract's price. A roll trades the old and the new
contract (|w_old| + |w_new|); otherwise the trade is |w[d] - w[d-1]|.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .contracts import ContractSpec
from .pit import align_asof, decision_times

TRADING_DAYS = 252


@dataclass
class BacktestConfig:
    target_vol: float = 0.10        # annualised, per sleeve
    max_leverage: float = 3.0       # cap on |notional / capital|
    vol_span: int = 60              # EWMA span (days) for the volatility estimate
    vol_floor: float = 0.05         # never assume vol below 5%/yr
    ticks_per_side: float = 1.0     # slippage per side, in ticks
    commission_usd: float = 2.5     # per contract per side
    min_trade: float = 0.0          # skip rebalances smaller than this (notional fraction)


def vol_estimate(daily: pd.DataFrame, span: int = 60, floor: float = 0.05) -> pd.DataFrame:
    """EWMA annualised vol of rolled returns; row t uses returns up to t (available_at[t])."""
    v = daily["return"].ewm(span=span, min_periods=span // 2).std() * np.sqrt(TRADING_DAYS)
    out = pd.DataFrame({"vol": v.clip(lower=floor), "available_at": daily["available_at"]})
    return out.dropna(subset=["vol"])


def cost_per_unit(daily: pd.DataFrame, spec: ContractSpec, cfg: BacktestConfig) -> pd.Series:
    """Cost of trading 1.0 of notional, as a fraction, on each date."""
    px = daily["held_settle"]
    slip = cfg.ticks_per_side * spec.tick_size / px
    comm = cfg.commission_usd / (px * spec.point_value_usd)      # contract notional in USD
    return (slip + comm).fillna(0.0)


def run_sleeve(daily: pd.DataFrame, signal: pd.DataFrame, spec: ContractSpec,
               cfg: BacktestConfig | None = None, scale: float = 1.0) -> pd.DataFrame:
    """Backtest one signal on one market's rolled returns (`daily` from roll.build_continuous).

    scale: multiplies the target vol (e.g. 0.5 for each leg of a pair).
    Returns per-day: signal_used, weight (decided at d), w_held, gross, cost, net.
    """
    cfg = cfg or BacktestConfig()
    dates = daily.index
    cut = decision_times(dates, spec)
    sig = align_asof(signal[["signal", "available_at"]], cut)["signal"].reindex(dates)
    vol = align_asof(vol_estimate(daily, cfg.vol_span, cfg.vol_floor), cut)["vol"].reindex(dates)

    w = (sig * cfg.target_vol * scale / vol).clip(-cfg.max_leverage, cfg.max_leverage).fillna(0.0)
    if cfg.min_trade > 0:                      # optional no-trade band
        vals, last = w.values.copy(), 0.0
        for i, x in enumerate(vals):
            if abs(x - last) < cfg.min_trade:
                vals[i] = last
            last = vals[i]
        w = pd.Series(vals, index=dates)

    w_prev = w.shift(1).fillna(0.0)
    rolled = daily["rolled_at_settle"].astype(bool)
    traded = np.where(rolled, w_prev.abs() + w.abs(), (w - w_prev).abs())
    cost = traded * cost_per_unit(daily, spec, cfg)

    w_held = w_prev                            # weight that earns r[t] was set at d = t-1
    gross = (w_held * daily["return"]).fillna(0.0)
    # trade costs at settle d hit that day's P&L
    net = gross - cost
    return pd.DataFrame({"signal_used": sig, "weight": w, "w_held": w_held, "traded": traded,
                         "gross": gross, "cost": cost, "net": net}, index=dates)


def combine(sleeves: dict[str, pd.DataFrame], weights: dict[str, float] | None = None) -> pd.Series:
    """Portfolio daily net return = weighted sum of sleeve nets (dates unioned, missing = 0)."""
    weights = weights or {k: 1.0 / len(sleeves) for k in sleeves}
    df = pd.concat({k: v["net"] for k, v in sleeves.items()}, axis=1).fillna(0.0)
    return sum(df[k] * w for k, w in weights.items())


def metrics(net: pd.Series, sleeve: pd.DataFrame | None = None) -> dict:
    """Standard performance statistics for a daily net return series."""
    r = net.dropna()
    r = r.loc[r.ne(0).idxmax():] if r.ne(0).any() else r      # start at first non-zero day
    n = len(r)
    if n < 2:
        return {}
    yrs = n / TRADING_DAYS
    eq = (1 + r).cumprod()
    ann_ret = eq.iloc[-1] ** (1 / yrs) - 1
    ann_vol = r.std() * np.sqrt(TRADING_DAYS)
    dd = eq / eq.cummax() - 1
    sharpe = r.mean() / r.std() * np.sqrt(TRADING_DAYS) if r.std() > 0 else np.nan
    out = {
        "start": r.index[0].date(), "end": r.index[-1].date(), "years": round(yrs, 2),
        "ann_return": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
        "sharpe_se": np.sqrt((1 + 0.5 * sharpe ** 2) / yrs) if np.isfinite(sharpe) else np.nan,
        "max_drawdown": dd.min(), "calmar": ann_ret / -dd.min() if dd.min() < 0 else np.nan,
        "hit_rate": (r[r != 0] > 0).mean(), "skew": r.skew(),
    }
    if sleeve is not None:
        s = sleeve.loc[r.index]
        out["turnover_per_yr"] = s["traded"].sum() / yrs
        out["cost_per_yr"] = s["cost"].sum() / yrs
        out["avg_gross_lev"] = s["w_held"].abs().mean()
        out["pct_long"] = (s["w_held"] > 0).mean()
        out["pct_short"] = (s["w_held"] < 0).mean()
    return out


def annual_returns(net: pd.Series) -> pd.Series:
    return (1 + net).groupby(net.index.year).prod() - 1
