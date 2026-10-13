"""Point-in-time helpers.

Conventions used across the package
-----------------------------------
* Every value carries an `available_at` (UTC) = the earliest moment it was knowable.
* A *decision* on trade date t happens at `decision_time(t)` (default: shortly
  before the close, so it can be executed at that day's settlement via TAS).
  A decision may only use values with available_at <= decision_time(t).
* Derived rows (returns, features) carry `available_at` = max(available_at of
  their inputs). Joining them onto a decision timeline must go through
  `align_asof`, never through a plain same-date join.
"""
from __future__ import annotations

import pandas as pd

from .contracts import ContractSpec


def decision_times(dates, spec: ContractSpec, local_time: str | None = None) -> pd.Series:
    """UTC decision cutoff for each trade date (exchange local time; default spec.decision_time)."""
    local_time = local_time or spec.decision_time
    dates = pd.DatetimeIndex(pd.to_datetime(dates)).normalize()
    h, m = map(int, local_time.split(":"))
    loc = (dates + pd.Timedelta(hours=h, minutes=m)).tz_localize(spec.timezone)
    return pd.Series(loc.tz_convert("UTC"), index=dates, name="decision_time")


def asof_wide(panel: pd.DataFrame, field: str, avail_col: str,
              cutoffs: pd.Series) -> pd.DataFrame:
    """For each cutoff (indexed by trade date) and contract, the latest `field`
    value whose `avail_col` <= cutoff. Returns trade_date x contract."""
    left = cutoffs.rename("cutoff").rename_axis("trade_date").reset_index().sort_values("cutoff")
    out = {}
    src = panel[["contract", avail_col, field]].dropna().sort_values(avail_col)
    for c, g in src.groupby("contract"):
        m = pd.merge_asof(left, g[[avail_col, field]], left_on="cutoff", right_on=avail_col,
                          direction="backward", allow_exact_matches=True)
        out[c] = m.set_index("trade_date")[field]
    return pd.DataFrame(out).reindex(cutoffs.index)


def align_asof(values: pd.DataFrame, cutoffs: pd.Series,
               avail_col: str = "available_at") -> pd.DataFrame:
    """Attach to each decision cutoff the most recent row of `values` that was
    available by then. This is the only safe way to join features to trades."""
    left = cutoffs.rename("decision_time").rename_axis("decision_date").reset_index().sort_values("decision_time")
    right = values.reset_index().sort_values(avail_col)
    m = pd.merge_asof(left, right, left_on="decision_time", right_on=avail_col, direction="backward")
    return m.set_index("decision_date")


def assert_point_in_time(df: pd.DataFrame, used_at_col: str, avail_col: str = "available_at") -> None:
    """Raise if any row is used before it was available."""
    bad = df[avail_col].notna() & (df[avail_col] > df[used_at_col])
    if bad.any():
        raise AssertionError(f"{int(bad.sum())} rows used before available, e.g.\n{df[bad].head()}")
