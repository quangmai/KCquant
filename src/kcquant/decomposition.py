"""Decompose the rolled (excess) return into spot return and roll yield.

    total_log[t]      = ln( S_held(t) / S_held(t-1) )                 (from the roll engine)
    spot_log[t]       = ln( P_spot(t) / P_spot(t-1) )                 (unadjusted nearby series)
    roll_yield_log[t] = total_log[t] - spot_log[t]

P_spot is the *unadjusted* price of the nearest contract that is not yet in its
notice period (FND > t). It is the usual proxy for the spot price; it jumps
when the nearby contract changes. Over any window the log terms add up exactly:
cum(total) = cum(spot) + cum(roll yield).

Intuition: on days where you hold the same contract as the spot proxy, roll
yield is 0. While you hold the 2nd contract ahead of FND it accrues the
convergence of 2nd vs 1st, and on the day the spot proxy switches it collects
ln(F_old / F_new) -- positive in backwardation, negative in contango.

All inputs are settles of day t and t-1 plus static FND dates, so each row's
available_at is the later of the two settles' availability.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .contracts import ContractSpec
from .data import to_wide
from .roll import RollResult


def spot_proxy(panel: pd.DataFrame, spec: ContractSpec, dates=None) -> pd.DataFrame:
    """Nearest contract with first notice day strictly after t, and its settle."""
    raw = to_wide(panel, "settle")
    meta = spec.contract_table(raw.columns).sort_values("seq")
    dates = raw.index if dates is None else pd.DatetimeIndex(dates)
    fnd = meta["first_notice_day"].values
    cols = list(meta.index)
    R = raw.reindex(index=dates, columns=cols).values
    eligible = (fnd[None, :] > dates.values[:, None]) & ~np.isnan(R)
    first = eligible.argmax(axis=1)
    has = eligible.any(axis=1)
    contract = np.where(has, np.array(cols, dtype=object)[first], None)
    price = np.where(has, R[np.arange(len(dates)), first], np.nan)
    av = to_wide(panel, "settle_available_at").reindex(index=dates, columns=cols)
    av = av.apply(lambda s: pd.to_datetime(s, utc=True).dt.tz_localize(None)).values
    avail = pd.DatetimeIndex(np.where(has, av[np.arange(len(dates)), first], np.datetime64("NaT"))).tz_localize("UTC")
    return pd.DataFrame({"spot_contract": contract, "spot_price": price, "spot_available_at": avail},
                        index=dates)


def decompose_returns(result: RollResult, panel: pd.DataFrame, spec: ContractSpec) -> pd.DataFrame:
    d = result.daily
    sp = spot_proxy(panel, spec, d.index)
    out = pd.DataFrame(index=d.index)
    out["held_contract"] = d["held_contract"]
    out["spot_contract"] = sp["spot_contract"]
    out["spot_price"] = sp["spot_price"]
    out["total_log"] = d["log_return"]
    out["spot_log"] = np.log(sp["spot_price"] / sp["spot_price"].shift(1))
    out["roll_yield_log"] = out["total_log"] - out["spot_log"]
    for c in ["total_log", "spot_log", "roll_yield_log"]:
        out[f"cum_{c}"] = out[c].fillna(0).cumsum()
    # trailing 1y realised roll yield (annualised, log)
    out["roll_yield_1y"] = out["roll_yield_log"].rolling(252, min_periods=200).sum()
    prev_av = sp["spot_available_at"].shift(1)
    out["available_at"] = pd.concat([d["available_at"], sp["spot_available_at"], prev_av], axis=1).max(axis=1)
    return out
