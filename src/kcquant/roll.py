"""Roll engine: which contract is held each day, chained returns, back-adjustment.

Timing convention (no look-ahead):
    pos[t]   contract held *after* the settlement of day t (decided with info
             available at decision_time(t), executed at settle t, e.g. via TAS)
    held[t]  = pos[t-1], the contract held over (settle t-1, settle t]
    r[t]     = S_held[t](t) / S_held[t](t-1) - 1   (both prices from the SAME contract)

A roll on day t means pos[t] != held[t]: sell old at settle t, buy new at settle t.
The return on t still comes from the old contract; from t+1 on, from the new one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .contracts import ContractSpec
from .data import to_wide
from .pit import asof_wide, decision_times


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FixedDaysBeforeFND:
    """Roll at the settlement `days` business days before first notice day."""
    days: int = 10


@dataclass(frozen=True)
class OpenInterestCrossover:
    """Roll when the next contract's open interest exceeds the held contract's,
    using only OI published by the decision time (ICE OI for day t-1 arrives
    on the morning of t). Bounded by a window relative to FND:
        never earlier than `earliest_days` bdays before FND,
        always by `deadline_days` bdays before FND.
    `confirm_days`: crossover must hold for this many consecutive decisions."""
    earliest_days: int = 40
    deadline_days: int = 5
    confirm_days: int = 1


@dataclass
class RollConfig:
    rule: FixedDaysBeforeFND | OpenInterestCrossover = field(default_factory=FixedDaysBeforeFND)
    roll_cycle: str | None = None        # e.g. "HNZ"; None = every listed month
    decision_time: str | None = None     # exchange local; None = spec.decision_time
    max_stale_days: int = 3              # ffill a held contract's missing settle at most this long


@dataclass
class RollResult:
    daily: pd.DataFrame        # per-day held contract, returns, indices, flags
    rolls: pd.DataFrame        # one row per executed roll
    raw_curve: pd.DataFrame    # trade_date x contract settles, untouched
    contracts: pd.DataFrame    # static contract metadata (FND, LTD, ...)
    config: RollConfig


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def build_continuous(panel: pd.DataFrame, spec: ContractSpec, config: RollConfig | None = None,
                     start=None, end=None) -> RollResult:
    config = config or RollConfig()
    rule = config.rule
    cycle = config.roll_cycle or spec.months

    raw = to_wide(panel, "settle")
    avail = to_wide(panel, "settle_available_at")
    meta = spec.contract_table(raw.columns)
    cyc = meta[meta["month_code"].isin(list(cycle))].sort_values("seq")
    order = list(cyc.index)
    nxt = dict(zip(order[:-1], order[1:]))

    dates = raw.index
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]
    if end is not None:
        dates = dates[dates <= pd.Timestamp(end)]
    if len(dates) == 0:
        raise ValueError("no dates in range")

    S = raw.loc[:, order].ffill(limit=config.max_stale_days).loc[dates]
    stale = raw.loc[dates, order].isna() & S.notna()

    # Roll window bounds in business days before FND (calendar-only => no look-ahead)
    fnd = cyc["first_notice_day"]
    bdays_to_fnd = pd.DataFrame(
        {c: spec.tcal.bdays_between_vec(dates, np.full(len(dates), fnd[c].to_datetime64()))
         for c in order}, index=dates)

    oi = None
    if isinstance(rule, OpenInterestCrossover):
        cut = decision_times(dates, spec, config.decision_time or spec.decision_time)
        oi = asof_wide(panel[panel["contract"].isin(order)], "open_interest", "oi_available_at", cut)

    def must_leave(c, t) -> bool:
        b = bdays_to_fnd.at[t, c]
        if isinstance(rule, FixedDaysBeforeFND):
            return b <= rule.days
        return b <= rule.deadline_days

    # initial contract: first in cycle that we are not already obliged to leave and that trades
    t0 = dates[0]
    cur = next((c for c in order if not must_leave(c, t0) and pd.notna(S.at[t0, c])), None)
    if cur is None:
        raise ValueError(f"no tradable contract on {t0.date()}")

    pos, triggers, streak = [], [], 0
    for t in dates:
        trig = None
        while True:
            n = nxt.get(cur)
            if n is None or pd.isna(S.at[t, n]):
                if must_leave(cur, t):
                    raise ValueError(f"must roll {cur} on {t.date()} but next contract has no data")
                break
            if must_leave(cur, t):
                trig = "fixed" if isinstance(rule, FixedDaysBeforeFND) else "deadline"
                cur, streak = n, 0
                continue
            if isinstance(rule, OpenInterestCrossover) and bdays_to_fnd.at[t, cur] <= rule.earliest_days:
                oc, on = oi.at[t, cur], oi.at[t, n]
                streak = streak + 1 if (pd.notna(oc) and pd.notna(on) and on > oc) else 0
                if streak >= rule.confirm_days:
                    trig, cur, streak = "oi_crossover", n, 0
                    continue
            break
        pos.append(cur)
        triggers.append(trig)

    pos = pd.Series(pos, index=dates, name="position")
    held = pos.shift(1)
    held.iloc[0] = pos.iloc[0]

    col = {c: i for i, c in enumerate(order)}
    Sv = S.values
    ix = np.arange(len(dates))
    h_idx = held.map(col).values
    p_idx = pos.map(col).values
    held_px = Sv[ix, h_idx]
    held_prev = np.r_[np.nan, Sv[ix[1:] - 1, h_idx[1:]]]
    pos_px = Sv[ix, p_idx]

    ret = held_px / held_prev - 1
    rolled = pos.values != held.values

    # Ratio back-adjustment: at each roll, ratio k = S_new/S_old at the roll settle;
    # everything up to and including that date is scaled by k (and later ratios).
    k = np.where(rolled, pos_px / held_px, 1.0)
    future_factor = np.cumprod(k[::-1])[::-1]
    backadj = held_px * future_factor

    av = avail.reindex(index=dates, columns=order) \
        .apply(lambda s: pd.to_datetime(s, utc=True).dt.tz_localize(None)).values
    available_at = pd.DatetimeIndex(np.maximum(av[ix, h_idx], av[ix, p_idx])).tz_localize("UTC")

    daily = pd.DataFrame({
        "held_contract": held.values,
        "position_after_settle": pos.values,
        "rolled_at_settle": rolled,
        "roll_trigger": triggers,
        "held_settle": held_px,
        "held_prev_settle": held_prev,
        "stale_price": stale.values[ix, h_idx],
        "return": ret,
        "log_return": np.log1p(ret),
        "bdays_to_fnd": bdays_to_fnd.values[ix, h_idx],
        "available_at": available_at,
    }, index=dates)
    daily.index.name = "trade_date"
    daily["tr_index"] = 100 * (1 + daily["return"].fillna(0)).cumprod()
    # NOT point-in-time: rescaled by rolls that happen after each date. Charting only.
    daily["backadj_price"] = backadj

    if daily["held_contract"].map(lambda c: c is not None).all():
        assert (daily["bdays_to_fnd"] > 0).all(), "held a contract into its notice period"

    r = daily[daily["rolled_at_settle"]]
    rolls = pd.DataFrame({
        "from_contract": r["held_contract"], "to_contract": r["position_after_settle"],
        "from_settle": r["held_settle"],
        "to_settle": [S.at[t, c] for t, c in zip(r.index, r["position_after_settle"])],
        "bdays_before_fnd": r["bdays_to_fnd"], "trigger": r["roll_trigger"],
    })
    rolls["ratio"] = rolls["to_settle"] / rolls["from_settle"]
    rolls["log_spread_new_vs_old"] = np.log(rolls["ratio"])
    return RollResult(daily=daily, rolls=rolls, raw_curve=raw, contracts=meta, config=config)
