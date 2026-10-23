import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kcquant.contracts import KC  # noqa: E402
from kcquant.data import apply_default_availability, make_synthetic_panel, validate_panel  # noqa: E402


def make_curve_panel(carry: float, start="2021-01-01", end="2023-12-31", spot=100.0,
                     spec=KC, listed_months=18):
    """Deterministic curve: constant spot, F(t,T) = spot * exp(carry * T)."""
    dates = spec.cal.business_days(start, end)
    meta = spec.contract_table(spec.contracts_between(start, pd.Timestamp(end) + pd.DateOffset(months=listed_months)))
    rows = []
    for cid, c in meta.iterrows():
        live = dates[(dates >= c["delivery_start"] - pd.DateOffset(months=listed_months))
                     & (dates <= c["last_trading_day"])]
        T = (c["last_trading_day"] - live).days.values / 365.25
        rows.append(pd.DataFrame(dict(trade_date=live, contract=cid, settle=spot * np.exp(carry * T),
                                      volume=100.0, open_interest=1000.0)))
    return validate_panel(apply_default_availability(pd.concat(rows), spec), spec)


@pytest.fixture(scope="session")
def synth():
    return make_synthetic_panel(KC, start="2019-01-01", end="2024-12-31", seed=4)
