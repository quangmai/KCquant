import numpy as np
import pandas as pd
import pytest

from kcquant.contracts import KC
from kcquant.data import apply_default_availability, validate_panel, to_wide
from kcquant.roll import FixedDaysBeforeFND, OpenInterestCrossover, RollConfig, build_continuous

RULES = [FixedDaysBeforeFND(10), FixedDaysBeforeFND(3), OpenInterestCrossover()]


@pytest.mark.parametrize("rule", RULES)
def test_never_held_into_notice(synth, rule):
    r = build_continuous(synth, KC, RollConfig(rule=rule))
    assert (r.daily["bdays_to_fnd"] > 0).all()
    if isinstance(rule, FixedDaysBeforeFND):
        assert (r.rolls["bdays_before_fnd"] == rule.days).all()
    else:
        assert r.rolls["bdays_before_fnd"].between(rule.deadline_days, rule.earliest_days).all()


@pytest.mark.parametrize("rule", RULES)
def test_returns_come_from_the_held_contract(synth, rule):
    r = build_continuous(synth, KC, RollConfig(rule=rule))
    raw = to_wide(synth)
    d = r.daily.iloc[1:]
    prev_dates = r.daily.index[:-1]
    for t, prev, c, ret in zip(d.index, prev_dates, d["held_contract"], d["return"]):
        assert ret == pytest.approx(raw.at[t, c] / raw.at[prev, c] - 1, rel=1e-12)


@pytest.mark.parametrize("rule", RULES)
def test_backadjusted_matches_chained_returns(synth, rule):
    d = build_continuous(synth, KC, RollConfig(rule=rule)).daily
    np.testing.assert_allclose(d["backadj_price"].pct_change().iloc[1:], d["return"].iloc[1:], rtol=1e-10)
    assert d["backadj_price"].iloc[-1] == pytest.approx(d["held_settle"].iloc[-1])
    np.testing.assert_allclose(d["tr_index"] / d["tr_index"].iloc[0],
                               d["backadj_price"] / d["backadj_price"].iloc[0], rtol=1e-10)


@pytest.mark.parametrize("rule", RULES)
def test_no_lookahead_truncation_invariance(synth, rule):
    """Results up to date T must not change when data after T is added."""
    cfg = RollConfig(rule=rule)
    full = build_continuous(synth, KC, cfg).daily.drop(columns=["backadj_price"])
    for cut in ["2020-06-15", "2022-02-10", "2023-11-20"]:
        part = synth[synth["trade_date"] <= cut]
        # data published after the cutoff isn't known either
        part = part.assign(oi_available_at=part["oi_available_at"].where(
            part["oi_available_at"] <= full.loc[:cut, "available_at"].max()))
        trunc = build_continuous(part, KC, cfg).daily.drop(columns=["backadj_price"])
        pd.testing.assert_frame_equal(trunc, full.loc[:cut])


def _two_contract_panel(cross_day: pd.Timestamp, oi_same_day: bool):
    dates = KC.cal.business_days("2025-12-15", "2026-02-18")
    rows = []
    for c, price in [("KCH26", 300.0), ("KCK26", 290.0)]:
        oi = np.where(dates >= cross_day, 2000.0, 1000.0) if c == "KCK26" else np.full(len(dates), 1500.0)
        rows.append(pd.DataFrame(dict(trade_date=dates, contract=c, settle=price, volume=1.0, open_interest=oi)))
    p = pd.concat(rows)
    if oi_same_day:  # pretend OI is known at noon the same day (before 13:00 decision)
        p["oi_available_at"] = (p["trade_date"] + pd.Timedelta(hours=12)).dt.tz_localize("America/New_York")
    return validate_panel(apply_default_availability(p, KC), KC)


def test_oi_crossover_uses_only_published_oi():
    cross = pd.Timestamp("2026-01-14")
    rule = RollConfig(rule=OpenInterestCrossover(earliest_days=40, deadline_days=5))
    late = build_continuous(_two_contract_panel(cross, oi_same_day=False), KC, rule).rolls
    early = build_continuous(_two_contract_panel(cross, oi_same_day=True), KC, rule).rolls
    assert list(early.index) == [cross]
    assert list(late.index) == [cross + KC.cal.bday]   # OI for the 14th is published on the 15th


def test_oi_deadline_forces_roll():
    cross = pd.Timestamp("2026-03-30")   # never crosses before FND
    r = build_continuous(_two_contract_panel(cross, False), KC,
                         RollConfig(rule=OpenInterestCrossover(deadline_days=5))).rolls
    assert r["trigger"].tolist() == ["deadline"] and r["bdays_before_fnd"].tolist() == [5]
