"""Trading signals. Each returns a DataFrame indexed by trade_date with

    signal        float in [-1, +1]   (+1 = full long, -1 = full short)
    available_at  UTC timestamp       (max availability of every input used)

so the backtester can join them to decisions with `pit.align_asof`.
Parameters are fixed, textbook values chosen BEFORE looking at results; they
are not fitted to this data. `scripts/run_backtest.py` reports a robustness
grid around them instead of picking the best.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LB_PER_TONNE = 2204.62262


def _frame(signal: pd.Series, available_at: pd.Series) -> pd.DataFrame:
    out = pd.DataFrame({"signal": signal.clip(-1, 1), "available_at": available_at})
    out.index.name = "trade_date"
    return out.dropna(subset=["signal"])


def carry(features: pd.DataFrame, full_at: float = 0.10) -> pd.DataFrame:
    """Curve carry: long in backwardation, short in contango.

    Uses carry_12_ann = ln(F1/F2) / (T2-T1), the annualised roll yield the
    front of the curve would earn if prices didn't move. Position scales
    linearly and is full size at |carry| >= `full_at` (10%/yr).
    """
    return _frame(features["carry_12_ann"] / full_at, features["available_at"])


def trend(daily: pd.DataFrame, lookbacks=(63, 126, 252)) -> pd.DataFrame:
    """Time-series momentum on the rolled (tradable) return index.

    Signal = average of sign(past return) over ~3, 6 and 12 months, so it
    takes values in {-1, -1/3, +1/3, +1}. Uses `tr_index`, which is built from
    the held contract's own returns (no roll jumps).
    """
    lr = np.log(daily["tr_index"])
    sigs = [np.sign(lr - lr.shift(L)) for L in lookbacks]
    s = pd.concat(sigs, axis=1).mean(axis=1, skipna=False)
    return _frame(s, daily["available_at"])


def spread_zscore(feat_kc: pd.DataFrame, feat_rc: pd.DataFrame, window: int = 252,
                  min_periods: int = 200, full_at: float = 2.0) -> pd.DataFrame:
    """Arabica/robusta relative value (mean reversion of the price ratio).

    ratio = ln(KC front in USD/t / RC front in USD/t); z = (ratio - rolling mean)
    / rolling std over `window` days (past data only). Signal is for a
    KC-vs-RC pair: +1 = long KC / short RC (arabica cheap vs history), full
    size at |z| >= `full_at`. Fronts are each market's nearest contract before
    first notice; their delivery months differ, so the ratio carries some
    calendar noise - acceptable for a 1-year mean-reversion window.
    """
    kc_usd_t = feat_kc["f1"] / 100 * LB_PER_TONNE            # c/lb -> USD/t
    df = pd.concat({"kc": kc_usd_t, "rc": feat_rc["f1"],
                    "a_kc": feat_kc["available_at"], "a_rc": feat_rc["available_at"]}, axis=1).dropna()
    ratio = np.log(df["kc"] / df["rc"])
    mp = min(min_periods, window)
    mu = ratio.rolling(window, min_periods=mp).mean()
    sd = ratio.rolling(window, min_periods=mp).std()
    z = (ratio - mu) / sd
    out = _frame(-z / full_at, df[["a_kc", "a_rc"]].max(axis=1))
    out["zscore"] = z.reindex(out.index)
    out["log_ratio"] = ratio.reindex(out.index)
    return out


def relative_carry(feat_kc: pd.DataFrame, feat_rc: pd.DataFrame, full_at: float = 0.10) -> pd.DataFrame:
    """Cross-sectional carry for a 2-market universe: long the coffee with the higher
    carry, short the other. +1 = long KC / short RC. Market-neutral by construction,
    so it isolates the carry premium from the direction of coffee prices.
    EXPLORATORY: added after the first backtest results (see run_backtest.py)."""
    df = pd.concat({"kc": feat_kc["carry_12_ann"], "rc": feat_rc["carry_12_ann"],
                    "a_kc": feat_kc["available_at"], "a_rc": feat_rc["available_at"]}, axis=1).dropna()
    return _frame((df["kc"] - df["rc"]) / full_at, df[["a_kc", "a_rc"]].max(axis=1))


def constant(daily: pd.DataFrame, value: float = 1.0) -> pd.DataFrame:
    """Benchmark: always long (value=1)."""
    return _frame(pd.Series(value, index=daily.index), daily["available_at"])


# ---------------------------------------------------------------------------
# Fundamental signals - definitions fixed in docs/fundamental_hypotheses.md
# ---------------------------------------------------------------------------
def cot_positioning(cot: pd.DataFrame, window: int = 156, min_periods: int = 104,
                    full_at: float = 2.0) -> pd.DataFrame:
    """H1/H2 crowding reversal: -z/2 of managed-money net % of open interest, z over the
    previous `window` weekly reports (3 years), past data only."""
    x = cot["mm_net_pct_oi"]
    mp = min(min_periods, window)
    z = (x - x.rolling(window, min_periods=mp).mean()) / x.rolling(window, min_periods=mp).std()
    out = _frame(-z / full_at, cot["available_at"])
    out["zscore"] = z.reindex(out.index)
    return out


def brl_move(ptax: pd.DataFrame, lookback: int = 20, full_at: float = 0.02) -> pd.DataFrame:
    """H3: long arabica when the real strengthens. -(20-day log change of USD/BRL) / 2%."""
    chg = np.log(ptax["usdbrl"]).diff(lookback)
    return _frame(-chg / full_at, ptax["available_at"])


def weather_brazil(regional: pd.DataFrame, rain_anom: pd.Series, frost_c: float = 2.0,
                   hold_days: int = 28) -> pd.DataFrame:
    """H4 (long only): +1 for ~20 sessions after a frost day (min temp <= 2 C, May-Sep);
    otherwise in Sep-Nov, +(1 - rain_60d / climatology_60d) clipped to [0, 1]."""
    frost = ((regional["tmin"] <= frost_c) & regional.index.month.isin([5, 6, 7, 8, 9])).astype(float)
    frost_recent = frost.rolling(hold_days, min_periods=1).max()
    drought = (1 - rain_anom).clip(0, 1).where(regional.index.month.isin([9, 10, 11]), 0.0)
    s = pd.concat([frost_recent, drought.reindex(regional.index)], axis=1).max(axis=1)
    out = _frame(s, regional["available_at"])
    out["frost"] = frost_recent.reindex(out.index)
    return out


def weather_vietnam(regional: pd.DataFrame, rain_anom: pd.Series) -> pd.DataFrame:
    """H5 (long only): +(1 - rain_90d / climatology_90d) clipped to [0, 1]."""
    s = (1 - rain_anom.reindex(regional.index)).clip(0, 1)
    return _frame(s, regional["available_at"])


# ---------------------------------------------------------------------------
# Warehouse stock signals - H6 / H8 in docs/fundamental_hypotheses.md
# ---------------------------------------------------------------------------
def _trailing_pct_rank(x: pd.Series, window: int = 250, min_periods: int = 120) -> pd.Series:
    """Percentile of each value among the PREVIOUS `window` values (current one excluded)."""
    vals = x.values
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        past = vals[max(0, i - window):i]
        past = past[~np.isnan(past)]
        if np.isnan(vals[i]) or len(past) < min_periods:
            continue
        out[i] = (past < vals[i]).mean() + 0.5 * (past == vals[i]).mean()
    return pd.Series(out, index=x.index)


def _gap_ok(index: pd.DatetimeIndex, cal, lookback: int, max_gap_bdays: int = 5) -> pd.Series:
    """True where no gap between consecutive reports in the last `lookback` reports exceeds
    `max_gap_bdays` trading days."""
    gaps = pd.Series(cal.bdays_between_vec(index[:-1], index[1:]), index=index[1:]).reindex(index)
    return gaps.rolling(lookback, min_periods=1).max().le(max_gap_bdays)


def cert_stock_change(cert: pd.DataFrame, cal, column: str = "certified", lookback: int = 20,
                      window: int = 250, min_periods: int = 120) -> pd.DataFrame:
    """H6: falling certified stocks -> long. Signal = -(2p - 1), p = trailing percentile rank of
    the 20-report log change."""
    x = np.log(cert[column].where(cert[column] > 0)).diff(lookback)
    x = x.where(_gap_ok(cert.index, cal, lookback))
    p = _trailing_pct_rank(x, window, min_periods)
    out = _frame(-(2 * p - 1), cert["available_at"])
    out["change_20"] = x.reindex(out.index)
    return out


def pending_grading(cert: pd.DataFrame, window: int = 250, min_periods: int = 120) -> pd.DataFrame:
    """H8: large pending-grading pile relative to certified stocks -> short.
    Signal = -(2p - 1), p = trailing percentile rank of pending / certified."""
    r = cert["pending"] / cert["certified"].where(cert["certified"] > 0)
    p = _trailing_pct_rank(r, window, min_periods)
    out = _frame(-(2 * p - 1), cert["available_at"])
    out["pending_ratio"] = r.reindex(out.index)
    return out


def inventory_spread_relief(features: pd.DataFrame, cert: pd.DataFrame,
                            min_backwardation: float = 3.0,
                            min_pending_ratio: float = 0.05,
                            stock_lookback: int = 5) -> pd.DataFrame:
    """H9 (Calendar Spread Relief Signal):
    Generates a bear spread signal (-1.0 = Short prompt F1 / Long deferred F2)
    when the front calendar spread is in backwardation (spread_12 >= min_backwardation)
    AND physical supply relief is arriving, evidenced by either:
      1) Pending grading queue is substantial (pending / certified >= min_pending_ratio)
      2) Certified inventory is recovering (positive stock_lookback delta in certified stocks)

    Returns a point-in-time signal in [-1.0, 0.0]:
      -1.0 = full bear spread (short F1, long F2)
       0.0 = neutral / no position
    """
    sp = features["spread_12"]
    r = cert["pending"] / cert["certified"].where(cert["certified"] > 0)
    stock_delta = cert["certified"].diff(stock_lookback)

    # Reindex cert series to features index safely
    r_aligned = r.reindex(features.index).ffill()
    stock_delta_aligned = stock_delta.reindex(features.index).ffill()
    cert_avail_aligned = cert["available_at"].reindex(features.index).ffill()

    in_bw = sp >= min_backwardation
    supply_relief = (r_aligned >= min_pending_ratio) | (stock_delta_aligned > 0)

    sig = pd.Series(0.0, index=features.index)
    sig[in_bw & supply_relief] = -1.0

    avail = pd.concat([pd.to_datetime(features["available_at"]),
                       pd.to_datetime(cert_avail_aligned)], axis=1).max(axis=1)

    out = _frame(sig, avail)
    out["spread_12"] = sp.reindex(out.index)
    out["pending_ratio"] = r_aligned.reindex(out.index)
    out["stock_delta"] = stock_delta_aligned.reindex(out.index)
    return out

