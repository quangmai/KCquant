import numpy as np
import pandas as pd
import pytest

from conftest import make_curve_panel
from kcquant.contracts import KC
from kcquant.decomposition import decompose_returns
from kcquant.features import curve_features
from kcquant.pit import align_asof, assert_point_in_time, decision_times
from kcquant.roll import FixedDaysBeforeFND, RollConfig, build_continuous


def test_decomposition_adds_up(synth):
    r = build_continuous(synth, KC, RollConfig(rule=FixedDaysBeforeFND(10)))
    d = decompose_returns(r, synth, KC).iloc[1:]
    np.testing.assert_allclose(d["total_log"], d["spot_log"] + d["roll_yield_log"], atol=1e-14)
    same = (d["held_contract"] == d["spot_contract"]) & (d["spot_contract"] == d["spot_contract"].shift(1))
    assert np.allclose(d.loc[same, "roll_yield_log"], 0)


@pytest.mark.parametrize("carry", [0.10, -0.10])
def test_roll_yield_sign_on_static_curve(carry):
    """Flat spot + static curve: spot return ~0, roll yield ~ -carry per year."""
    p = make_curve_panel(carry)
    r = build_continuous(p, KC, RollConfig(rule=FixedDaysBeforeFND(10)), start="2021-06-01", end="2023-06-01")
    d = decompose_returns(r, p, KC)
    years = (d.index[-1] - d.index[0]).days / 365.25
    assert d["cum_total_log"].iloc[-1] == pytest.approx(-carry * years, rel=0.02)
    assert abs(d["cum_spot_log"].iloc[-1]) < 0.05
    assert np.sign(d["cum_roll_yield_log"].iloc[-1]) == -np.sign(carry)


@pytest.mark.parametrize("carry,flag", [(0.08, 1), (-0.08, -1), (0.0, 0)])
def test_curve_features_on_static_curve(carry, flag):
    f = curve_features(make_curve_panel(carry), KC)
    np.testing.assert_allclose(f["slope_12_ann"], carry, atol=1e-9)
    np.testing.assert_allclose(f["slope_curve_ann"].dropna(), carry, atol=1e-9)
    assert (f["curve_flag"] == flag).all()
    assert (np.sign(f["spread_12"].round(9)) == -flag).all()


def test_features_truncation_invariance(synth):
    full = curve_features(synth, KC)
    for cut in ["2020-03-02", "2023-07-14"]:
        part = curve_features(synth[synth["trade_date"] <= cut], KC)
        pd.testing.assert_frame_equal(part, full.loc[:cut])


def test_features_front_excludes_notice_period(synth):
    f = curve_features(synth, KC)
    fnd = f["c1_contract"].map(lambda c: KC.first_notice_day(*KC.parse(c)))
    assert (fnd > f.index).all()


def test_align_asof_only_uses_published_features(synth):
    f = curve_features(synth, KC)
    cut = decision_times(f.index, KC, "13:00")     # before today's 14:00 settle
    a = align_asof(f, cut)
    a["decision_time"] = a["decision_time"]
    assert_point_in_time(a, "decision_time")
    # at 13:00 on day t the newest available features are from day t-1
    assert (a.iloc[1:]["trade_date"].values == f.index[:-1].values).all()
