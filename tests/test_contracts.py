import pandas as pd

from kcquant.contracts import KC, RC


def test_kc_notice_and_expiry_dates():
    # ICE Coffee C: FND = 7 bdays before 1st bday of delivery month (Thanksgiving skipped)
    assert KC.first_notice_day(2025, 12) == pd.Timestamp("2025-11-19")
    assert KC.last_notice_day(2025, 12) == pd.Timestamp("2025-12-19")
    assert KC.last_trading_day(2025, 12) == pd.Timestamp("2025-12-18")
    assert KC.first_notice_day(2026, 3) == pd.Timestamp("2026-02-19")


def test_ids_and_symbols():
    assert KC.contract_id(2026, 3) == "KCH26"
    assert KC.parse("KCH26") == (2026, 3)
    assert KC.databento_raw_symbol(2026, 3) == "KC  FMH0026!"
    assert KC.contracts_between("2026-01-01", "2026-12-31") == ["KCH26", "KCK26", "KCN26", "KCU26", "KCZ26"]
    assert RC.contracts_between("2026-01-01", "2026-12-31")[0] == "RCF26"


def test_business_day_counting():
    cal = KC.cal
    assert not cal.is_business_day("2025-11-27")       # Thanksgiving
    assert cal.bdays_between("2025-11-26", "2025-12-01") == 2
    assert cal.bdays_between("2025-12-01", "2025-11-26") == -2
    assert list(cal.bdays_between_vec(["2025-11-26"], ["2025-12-01"])) == [2]


def test_rc_dates_use_london_calendar():
    # 4 bdays before 1st bday of Jan 2027 (Mon 4 Jan): skips 1 Jan, 28 Dec (Boxing Day obs.), 25 Dec
    assert RC.first_notice_day(2027, 1) == pd.Timestamp("2026-12-24")
    assert RC.cal.is_business_day("2026-12-24") and not RC.cal.is_business_day("2026-12-28")
    assert RC.databento_raw_symbol(2027, 1) == "RC  FMF0027!"


def test_snapshot_loader_requires_probed_windows():
    import dataclasses
    import pytest
    from kcquant.data import SnapshotStatisticsLoader
    with pytest.raises(ValueError, match="probe_snapshot.py --root RC"):
        SnapshotStatisticsLoader(dataclasses.replace(RC, snapshot_windows=None), client=object())


def test_rc_snapshot_windows_follow_london_time(tmp_path):
    from kcquant.data import SnapshotStatisticsLoader
    ld = SnapshotStatisticsLoader(RC, cache_dir=tmp_path, client=object())
    assert ld.utc_window("2026-01-21")[0] == pd.Timestamp("2026-01-21 22:59", tz="UTC")   # GMT
    assert ld.utc_window("2026-07-15")[0] == pd.Timestamp("2026-07-15 21:59", tz="UTC")   # BST
    assert ld.utc_window("2026-07-15", ld.windows[1]) == (pd.Timestamp("2026-07-15 16:29:30", tz="UTC"),
                                                          pd.Timestamp("2026-07-15 17:00", tz="UTC"))


def test_ice_europe_trading_vs_contract_calendars():
    # trading days: ICE traded Easter Monday 2019, but it was still a UK bank holiday
    assert RC.tcal.is_business_day("2019-04-22") and not RC.cal.is_business_day("2019-04-22")
    for d in ["2022-09-19", "2023-05-08", "2022-06-02"]:       # closed in both senses
        assert not RC.tcal.is_business_day(d) and not RC.cal.is_business_day(d)
    assert RC.cal.is_business_day("2022-05-30")                # spring holiday moved in 2022
    # last trading days match the exchange (Databento definition.expiration)
    assert RC.last_trading_day(2019, 5) == pd.Timestamp("2019-05-24")   # 27 May 2019 bank hol.
    assert RC.last_trading_day(2022, 5) == pd.Timestamp("2022-05-25")   # 30 May 2022 not a hol.
    assert KC.tcal is KC.cal
