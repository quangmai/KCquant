import numpy as np
import pandas as pd
import pytest

from kcquant.backtest import BacktestConfig, metrics, run_sleeve
from kcquant.contracts import KC, RC
from kcquant.features import curve_features
from kcquant.roll import FixedDaysBeforeFND, RollConfig, build_continuous
from kcquant import signals

NY = "America/New_York"


def _daily(n=300, vol=0.30, seed=1, start="2024-01-02"):
    dates = KC.tcal.business_days(start, pd.Timestamp(start) + pd.Timedelta(days=int(n * 1.5)))[:n]
    rng = np.random.default_rng(seed)
    r = rng.normal(0, vol / np.sqrt(252), n)
    r[0] = np.nan
    px = 200 * np.exp(np.nancumsum(np.nan_to_num(r)))
    avail = (dates + pd.Timedelta(hours=18)).tz_localize(NY).tz_convert("UTC")
    return pd.DataFrame({"return": r, "held_settle": px, "rolled_at_settle": False,
                         "tr_index": 100 * px / px[0], "available_at": avail}, index=dates)


def test_signal_acts_only_after_it_is_published():
    d = _daily()
    k = 150
    sig = pd.DataFrame({"signal": np.where(np.arange(len(d)) >= k, 1.0, 0.0),
                        "available_at": d["available_at"]}, index=d.index)
    s = run_sleeve(d, sig, KC, BacktestConfig(ticks_per_side=0, commission_usd=0))
    # published 18:00 on day k -> decision 13:00 on k+1 -> held from settle k+1 -> earns r[k+2]
    assert s["weight"].iloc[k] == 0 and s["weight"].iloc[k + 1] > 0
    assert s["w_held"].iloc[k + 1] == 0 and s["w_held"].iloc[k + 2] > 0
    assert s["gross"].iloc[: k + 2].abs().sum() == 0


def test_vol_targeting_hits_target_roughly():
    d = _daily(n=2000, vol=0.40, seed=3)
    sig = signals.constant(d)
    s = run_sleeve(d, sig, KC, BacktestConfig(target_vol=0.10, ticks_per_side=0, commission_usd=0))
    realised = s["gross"].iloc[100:].std() * np.sqrt(252)
    assert 0.08 < realised < 0.12


def test_roll_days_pay_for_both_legs():
    d = _daily()
    d.loc[d.index[200], "rolled_at_settle"] = True
    sig = signals.constant(d)
    cfg = BacktestConfig(ticks_per_side=1, commission_usd=0)
    s = run_sleeve(d, sig, KC, cfg)
    t = d.index[200]
    assert s.loc[t, "traded"] == pytest.approx(s["weight"].shift(1).loc[t] + s.loc[t, "weight"])
    assert s.loc[t, "cost"] == pytest.approx(s.loc[t, "traded"] * KC.tick_size / d.loc[t, "held_settle"])


def test_backtest_has_no_lookahead(synth):
    cfg = RollConfig(rule=FixedDaysBeforeFND(10))

    def run(panel):
        res = build_continuous(panel, KC, cfg)
        feats = curve_features(panel, KC)
        out = {}
        for name, sig in {"carry": signals.carry(feats), "trend": signals.trend(res.daily)}.items():
            out[name] = run_sleeve(res.daily, sig, KC)
        return out

    full = run(synth)
    for cut in ["2021-03-01", "2023-08-15"]:
        part = synth[synth["trade_date"] <= cut]
        trunc = run(part)
        for k in full:
            pd.testing.assert_frame_equal(trunc[k], full[k].loc[:cut])


def test_spread_signal_direction():
    idx = pd.bdate_range("2023-01-02", periods=400)
    avail = (idx + pd.Timedelta(hours=23)).tz_localize("UTC")
    kc_f1 = pd.Series(200.0, index=idx)
    kc_f1.iloc[-1] = 260.0                          # arabica suddenly rich vs robusta
    fk = pd.DataFrame({"f1": kc_f1 + np.random.default_rng(0).normal(0, 1, 400), "available_at": avail})
    fr = pd.DataFrame({"f1": 3000.0, "available_at": avail}, index=idx)
    s = signals.spread_zscore(fk, fr)
    assert s["zscore"].iloc[-1] > 2 and s["signal"].iloc[-1] == -1.0     # short KC / long RC


def test_metrics_basic():
    r = pd.Series([0.01, -0.005] * 252, index=pd.bdate_range("2020-01-01", periods=504))
    m = metrics(r)
    assert m["years"] == 2.0 and m["hit_rate"] == 0.5 and m["sharpe"] > 0
