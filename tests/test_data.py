import numpy as np
import pandas as pd
import pytest

from kcquant.contracts import KC
from kcquant.data import (PANEL_COLUMNS, normalize_databento_statistics, raw_symbol_to_contract,
                          validate_panel)


def test_synthetic_panel_schema(synth):
    assert list(synth.columns[:len(PANEL_COLUMNS)]) == PANEL_COLUMNS
    # OI for day t is only published on the next business day
    assert (synth["oi_available_at"].dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
            > synth["trade_date"]).all()
    assert (synth["settle_available_at"] > synth["trade_date"].dt.tz_localize("UTC")).all()


def test_validate_rejects_duplicates(synth):
    with pytest.raises(ValueError):
        validate_panel(pd.concat([synth.head(5), synth.head(1)]))


def test_raw_symbol_mapping():
    assert raw_symbol_to_contract("KC  FMH0026!", KC) == "KCH26"
    assert raw_symbol_to_contract("KC  FMH0026!_OMPA0000011002091525", KC) is None   # option
    assert raw_symbol_to_contract("SB  FMN0025!", KC) is None


def _ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_normalize_databento_statistics_is_point_in_time():
    sym = "KC  FMH0026!"
    td = _ts("2026-01-05 05:00")  # ts_ref: the session date
    recs = [
        # preliminary settle, then official settle (different value), then a re-send
        dict(ts_recv=_ts("2026-01-05 18:40"), stat_type=3, price=350.10, quantity=0, stat_flags=0),
        dict(ts_recv=_ts("2026-01-05 19:30"), stat_type=3, price=350.25, quantity=0, stat_flags=1),
        dict(ts_recv=_ts("2026-01-06 08:00"), stat_type=3, price=350.25, quantity=0, stat_flags=1),
        # OI for Jan 5 published next morning
        dict(ts_recv=_ts("2026-01-06 14:00"), stat_type=9, price=np.nan, quantity=81000, stat_flags=0),
        # cleared volume
        dict(ts_recv=_ts("2026-01-05 22:00"), stat_type=6, price=np.nan, quantity=25000, stat_flags=0),
    ]
    df = pd.DataFrame(recs)
    df["ts_event"] = df["ts_recv"]
    df["ts_ref"] = td
    # Databento stamps OI's session date at midnight UTC (= previous evening in New York)
    df.loc[df["stat_type"] == 9, "ts_ref"] = _ts("2026-01-05 00:00")
    df["update_action"] = 1
    df["symbol"] = sym
    spread = df.iloc[[0]].assign(symbol="KC   10  30951651")
    deleted = df.iloc[[1]].assign(update_action=2, price=999.0)
    p = normalize_databento_statistics(pd.concat([df, spread, deleted]), KC)
    assert len(p) == 1
    row = p.iloc[0]
    assert row["trade_date"] == pd.Timestamp("2026-01-05")
    assert row["settle"] == 350.25                                   # official value
    assert row["settle_available_at"] == _ts("2026-01-05 19:30")    # first time THAT value was seen
    assert row["open_interest"] == 81000 and row["oi_available_at"] == _ts("2026-01-06 14:00")
    assert row["volume"] == 25000


def test_validate_expirations_uses_utc_date():
    from kcquant.data import validate_expirations
    defs = pd.DataFrame({"raw_symbol": ["KC  FMZ0025!", "KC  FMK0019!"],
                         # exchange LTDs 2025-12-18 (Thu) and 2019-05-20 (Mon), stamped 00:00 UTC
                         "expiration": [_ts("2025-12-18 00:00"), _ts("2019-05-20 00:00")]})
    assert validate_expirations(defs, KC).empty


def test_snapshot_oi_dated_to_previous_actual_session():
    """OI in a snapshot belongs to the previous session the exchange actually traded,
    even if the calendar thinks that day was a holiday."""
    rows = []
    for day, oi in [("2019-04-18", 100), ("2019-04-22", 200), ("2019-04-23", 300)]:
        t = pd.Timestamp(day + " 23:00", tz="Europe/London").tz_convert("UTC")
        base = dict(ts_recv=t, ts_event=t, ts_ref=pd.NaT, update_action=1, stat_flags=0, symbol="RC  FMK0019!")
        rows += [dict(base, stat_type=3, price=1400.0, quantity=0),
                 dict(base, stat_type=9, price=np.nan, quantity=oi)]
    from kcquant.contracts import RC
    p = normalize_databento_statistics(pd.DataFrame(rows), RC).set_index("trade_date")
    # Tue 23 Apr's snapshot OI (300) belongs to Mon 22 Apr (Easter Monday, traded in 2019)
    assert p.loc["2019-04-22", "open_interest"] == 300
    assert p.loc["2019-04-18", "open_interest"] == 200
