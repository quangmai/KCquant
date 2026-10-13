"""Curve features, timestamped for point-in-time use.

Curve positions on trade date t: contracts with a settle on t and first notice
day strictly after t (optionally at least `min_bdays_to_fnd` business days
away), ordered by expiry -> c1, c2, ... . Only same-day settles and static
calendar data are used, so each feature row's `available_at` is the latest
settle availability among the contracts it uses.

Sign conventions
    spread_12        F1 - F2 (quote units).  > 0  => backwardation
    slope_12_ann     ln(F2/F1) / (T2 - T1)   > 0  => contango   (per year)
    carry_12_ann     -slope_12_ann           expected annualised roll yield if the curve doesn't move
    slope_curve_ann  OLS slope of ln F on T over the first `n_slope` contracts
    curve_flag       +1 contango, -1 backwardation, 0 flat (|slope_12_ann| <= flat_band)

Join features to trading decisions with `kcquant.pit.align_asof`, never by date.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .contracts import ContractSpec


def curve_positions(panel: pd.DataFrame, spec: ContractSpec, max_positions: int = 6,
                    min_bdays_to_fnd: int = 0) -> pd.DataFrame:
    """Long table: trade_date, position (1..N), contract, settle, T_years, settle_available_at."""
    meta = spec.contract_table(panel["contract"].unique())
    p = panel[["trade_date", "contract", "settle", "settle_available_at"]].join(
        meta[["first_notice_day", "last_trading_day"]], on="contract")
    p = p[p["settle"].notna()]
    b = spec.tcal.bdays_between_vec(p["trade_date"].values, p["first_notice_day"].values)
    p = p[b > max(min_bdays_to_fnd, 0)].copy()
    p["T_years"] = (p["last_trading_day"] - p["trade_date"]).dt.days / 365.25
    p = p.sort_values(["trade_date", "last_trading_day"])
    p["position"] = p.groupby("trade_date").cumcount() + 1
    return p[p["position"] <= max_positions].drop(columns=["first_notice_day", "last_trading_day"])


def curve_features(panel: pd.DataFrame, spec: ContractSpec, n_slope: int = 4,
                   flat_band: float = 0.01, min_bdays_to_fnd: int = 0) -> pd.DataFrame:
    cp = curve_positions(panel, spec, max(n_slope, 2), min_bdays_to_fnd)
    w = cp.pivot(index="trade_date", columns="position")
    f = pd.DataFrame(index=w.index)
    f["c1_contract"], f["c2_contract"] = w[("contract", 1)], w[("contract", 2)]
    f["f1"], f["f2"] = w[("settle", 1)], w[("settle", 2)]
    T1, T2 = w[("T_years", 1)], w[("T_years", 2)]
    f["spread_12"] = f["f1"] - f["f2"]
    f["spread_12_usd"] = f["spread_12"] * spec.point_value_usd
    f["log_spread_12"] = np.log(f["f1"] / f["f2"])
    f["dT_12_years"] = T2 - T1
    f["slope_12_ann"] = np.log(f["f2"] / f["f1"]) / f["dT_12_years"]
    f["carry_12_ann"] = -f["slope_12_ann"]

    # OLS slope of ln F on T across the first n_slope positions (needs all present)
    sub = cp[cp["position"] <= n_slope].assign(y=lambda x: np.log(x["settle"]))
    g = sub.groupby("trade_date")
    n = g.size()
    sx, sy = g["T_years"].sum(), g["y"].sum()
    sxx = g["T_years"].apply(lambda s: (s ** 2).sum())
    sxy = (sub["T_years"] * sub["y"]).groupby(sub["trade_date"]).sum()
    slope = (n * sxy - sx * sy) / (n * sxx - sx ** 2)
    f["slope_curve_ann"] = slope.where(n == n_slope)

    s = f["slope_12_ann"]
    f["curve_flag"] = np.select([s > flat_band, s < -flat_band], [1, -1], 0)
    f.loc[s.isna(), "curve_flag"] = np.nan
    f["curve_state"] = f["curve_flag"].map({1: "contango", -1: "backwardation", 0: "flat"})

    # available_at: latest availability among the settles used (incl. the slope set)
    av = sub.groupby("trade_date")["settle_available_at"].max()
    av12 = cp[cp["position"] <= 2].groupby("trade_date")["settle_available_at"].max()
    f["available_at"] = pd.concat([av, av12], axis=1).max(axis=1).reindex(f.index)
    f.index.name = "trade_date"
    return f
