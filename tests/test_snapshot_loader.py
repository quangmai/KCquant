"""SnapshotStatisticsLoader with a fake Databento client (no network)."""
import numpy as np
import pandas as pd
import pytest

from kcquant.contracts import KC
from kcquant.data import SnapshotStatisticsLoader

NY = "America/New_York"


class _Store:
    def __init__(self, df):
        self.df = df

    def to_df(self):
        return self.df.set_index("ts_recv") if len(self.df) else self.df


NO_EVENING = {pd.Timestamp("2026-08-06")}          # like 2019-07-17 in the probe
EARLY_CLOSE = {pd.Timestamp("2026-08-12")}         # like 24 Dec: only a 12:15 snapshot
EARLY_EVENING = {pd.Timestamp("2026-08-04")}       # like 2024-07-17: 17:59:59


class _TS:
    """Serves snapshots (settle, OI, volume for 2 contracts) at 13:30 and 18:00 NY,
    with the quirks seen in the probe."""
    def __init__(self):
        self.calls = []

    def _snap_times(self, day):
        d = day.tz_localize(None)
        if not KC.cal.is_business_day(d):
            return []
        if d in EARLY_CLOSE:
            return [(day + pd.Timedelta("12:15:00")).tz_convert("UTC")]
        t = [day + pd.Timedelta(hours=13, minutes=30)]
        if d in EARLY_EVENING:
            t.append(day + pd.Timedelta("17:59:59"))
        elif d not in NO_EVENING:
            t.append(day + pd.Timedelta(hours=18))
        return [x.tz_convert("UTC") for x in t]

    def get_range(self, **kw):
        self.calls.append(kw)
        s, e = pd.Timestamp(kw["start"]), pd.Timestamp(kw["end"])
        day = s.tz_convert(NY).normalize()
        hits = [t for t in self._snap_times(day) if s <= t < e]
        if not hits:
            return _Store(pd.DataFrame())
        snap = hits[0]
        rows = []
        for i, sym in enumerate(["KC  FMZ0026!", "KC  FMH0027!"]):
            base = dict(ts_recv=snap, ts_event=snap, ts_ref=pd.NaT, update_action=1, stat_flags=0, symbol=sym)
            rows += [dict(base, stat_type=3, price=300.0 + i + day.day / 100, quantity=0),
                     dict(base, stat_type=9, price=np.nan, quantity=50_000 - i * 10_000),
                     dict(base, stat_type=6, price=np.nan, quantity=10_000)]
        return _Store(pd.DataFrame(rows))


class _Meta:
    def get_dataset_range(self, dataset):
        return {"start": "2018-12-23T00:00:00Z", "end": "2026-10-03T00:00:00Z"}

    def get_cost(self, **kw):
        return 0.001

    def get_dataset_condition(self, dataset, start_date, end_date):
        days = pd.bdate_range(start_date, end_date)
        return [{"date": f"{d:%Y-%m-%d}", "condition": "degraded" if d == pd.Timestamp("2026-08-11") else "available"}
                for d in days]


class _Client:
    def __init__(self):
        self.metadata, self.timeseries = _Meta(), _TS()


def _loader(tmp_path, **kw):
    return SnapshotStatisticsLoader(KC, cache_dir=tmp_path, client=_Client(), **kw)


def test_windows_follow_daylight_saving(tmp_path):
    ld = _loader(tmp_path)
    assert ld.utc_window("2026-01-14") == (pd.Timestamp("2026-01-14 22:59", tz="UTC"),
                                           pd.Timestamp("2026-01-14 23:01", tz="UTC"))   # EST
    assert ld.utc_window("2026-07-15") == (pd.Timestamp("2026-07-15 21:59", tz="UTC"),
                                           pd.Timestamp("2026-07-15 22:01", tz="UTC"))   # EDT
    assert ld.utc_window("2026-07-15", ("13:29:30", "13:30:30")) == (
        pd.Timestamp("2026-07-15 17:29:30", tz="UTC"), pd.Timestamp("2026-07-15 17:30:30", tz="UTC"))


def test_request_days_are_weekdays_within_available_range(tmp_path):
    days = _loader(tmp_path).request_days("2026-09-24", "2026-10-10")
    assert days[0] == pd.Timestamp("2026-09-24") and days[-1] == pd.Timestamp("2026-10-02")
    assert all(d.weekday() < 5 for d in days)


def test_fetch_is_incremental_and_normalises(tmp_path):
    ld = _loader(tmp_path)
    panel = ld.load("2026-08-03", "2026-08-15", with_definitions=False)
    n_calls = len(ld.client.timeseries.calls)
    # 10 weekdays; no-evening day needs 2 requests, early-close day needs 3
    assert n_calls == 13
    assert ld.fallback_days == [pd.Timestamp("2026-08-06"), pd.Timestamp("2026-08-12")]
    td = set(panel.loc[panel.contract == "KCZ26", "trade_date"])
    assert set(pd.bdate_range("2026-08-03", "2026-08-14")) <= td   # no day lost
    early = panel[(panel.contract == "KCZ26") & (panel.trade_date == "2026-08-04")].iloc[0]
    assert early.settle_available_at == pd.Timestamp("2026-08-04 17:59:59", tz=NY)
    fb = panel[(panel.contract == "KCZ26") & (panel.trade_date == "2026-08-06")].iloc[0]
    assert fb.settle_available_at == pd.Timestamp("2026-08-06 13:30", tz=NY)
    # second run: nothing new to fetch
    ld.load("2026-08-03", "2026-08-15", with_definitions=False)
    assert len(ld.client.timeseries.calls) == n_calls
    # extending the range only fetches the new days
    ld.load("2026-08-03", "2026-08-22", with_definitions=False)
    assert len(ld.client.timeseries.calls) == n_calls + 5

    row = panel[(panel.contract == "KCZ26") & (panel.trade_date == "2026-08-05")].iloc[0]
    assert row.settle == pytest.approx(300.05)
    assert row.settle_available_at == pd.Timestamp("2026-08-05 18:00", tz=NY)
    # OI in Monday's snapshot belongs to Friday's session
    mon = panel[(panel.contract == "KCZ26") & (panel.trade_date == "2026-08-07")].iloc[0]
    assert mon.oi_available_at == pd.Timestamp("2026-08-10 18:00", tz=NY)


def test_cost_estimate_counts_only_missing_days(tmp_path):
    ld = _loader(tmp_path)
    c1 = ld.estimate_costs("2026-08-03", "2026-08-15")
    assert c1["_days_to_fetch"] == 10
    ld.fetch("2026-08-03", "2026-08-15")
    assert ld.estimate_costs("2026-08-03", "2026-08-15")["_days_to_fetch"] == 0


def test_cost_cap(tmp_path):
    ld = _loader(tmp_path, max_cost_usd=0.0001)
    with pytest.raises(RuntimeError, match="max_cost"):
        ld.fetch("2026-08-03", "2026-08-15")


def test_repair_recovers_days_cached_with_old_windows(tmp_path):
    old = _loader(tmp_path, windows=SnapshotStatisticsLoader.DEFAULT_WINDOWS[:2])
    old.fetch("2026-08-10", "2026-08-15")
    assert pd.Timestamp("2026-08-12") in old.empty_days          # early close missed
    new = _loader(tmp_path)                                       # same cache files
    assert new.missing_days("2026-08-10", "2026-08-15") == []
    panel = new.load("2026-08-10", "2026-08-15", with_definitions=False)
    assert pd.Timestamp("2026-08-12") in set(panel.trade_date)
    n = len(new.client.timeseries.calls)
    new.load("2026-08-10", "2026-08-15", with_definitions=False)  # retried only once
    assert len(new.client.timeseries.calls) == n


def test_no_data_warnings_are_silenced_and_degraded_days_recorded(tmp_path, recwarn):
    import warnings

    class _WarnTS(_TS):
        def get_range(self, **kw):
            warnings.warn("No data found for the request you submitted.")
            return super().get_range(**kw)

    ld = _loader(tmp_path)
    ld.client.timeseries = _WarnTS()
    ld.fetch("2026-08-10", "2026-08-12")
    assert not [w for w in recwarn if "No data found" in str(w.message)]
    assert ld.degraded_days == {"2026-08-11"}


def test_parallel_fetch_matches_sequential(tmp_path):
    seq = _loader(tmp_path / "a", workers=1).load("2026-07-01", "2026-09-01", with_definitions=False)
    par = _loader(tmp_path / "b", workers=8).load("2026-07-01", "2026-09-01", with_definitions=False)
    pd.testing.assert_frame_equal(seq, par)
    assert len(list((tmp_path / "b").glob("*.days.txt"))) == 2       # July and August written


def test_transient_errors_are_retried(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class _Flaky(_TS):
        fails = 2

        def get_range(self, **kw):
            if _Flaky.fails:
                _Flaky.fails -= 1
                raise RuntimeError("429 Too Many Requests")
            return super().get_range(**kw)

    ld = _loader(tmp_path, workers=1)
    ld.client.timeseries = _Flaky()
    panel = ld.load("2026-08-03", "2026-08-05", with_definitions=False)
    assert set(panel.trade_date) == {pd.Timestamp("2026-08-03"), pd.Timestamp("2026-08-04")}
