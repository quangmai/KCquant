import json

import numpy as np
import pandas as pd

from kcquant import fundamentals as fu
from kcquant import signals


def _ts(s, tz):
    return pd.Timestamp(s, tz=tz).tz_convert("UTC")


def test_cftc_release_dates_normal_holiday_and_shutdown():
    asof = pd.DatetimeIndex(["2024-03-05", "2021-06-15", "2024-11-26", "2025-10-14", "2019-01-22"])
    rel = fu.cftc_release_dates(asof)
    assert rel[0] == _ts("2024-03-08 15:30", fu.NY)       # normal: Friday 15:30 NY
    assert rel[1] == _ts("2021-06-21 15:30", fu.NY)       # Juneteenth (Fri 18th) -> Mon 21st (CFTC confirmed)
    assert rel[2] == _ts("2024-12-02 15:30", fu.NY)       # Thanksgiving week -> Monday
    assert rel[3] == _ts("2025-11-25 15:30", fu.NY)       # 2025 shutdown catch-up table
    assert rel[4] == _ts("2019-02-15 15:30", fu.NY)       # 2019 shutdown catch-up


def test_parse_cftc_and_signal_uses_release_time():
    weeks = pd.date_range("2020-01-07", periods=200, freq="7D")
    rng = np.random.default_rng(0)
    recs = [{"report_date_as_yyyy_mm_dd": f"{d:%Y-%m-%d}T00:00:00.000", "open_interest_all": "200000",
             "m_money_positions_long_all": str(50000 + int(rng.normal(0, 5000))),
             "m_money_positions_short_all": "20000", "prod_merc_positions_long": "30000",
             "prod_merc_positions_short": "60000"} for d in weeks]
    recs[-1]["m_money_positions_long_all"] = "120000"     # extreme crowding in the last report
    cot = fu.parse_cftc(recs)
    assert (cot["available_at"] > cot.index.tz_localize("UTC")).all()
    s = signals.cot_positioning(cot)
    assert s["signal"].iloc[-1] == -1.0                    # crowded long -> short
    assert s["available_at"].iloc[-1] == cot["available_at"].iloc[-1]


def test_parse_ice_cot_flexible_columns():
    csv = ("Market_and_Exchange_Names,As_of_Date_Form_MM/DD/YYYY,FutOnly_or_Combined,Open_Interest_All,"
           "Prod_Merc_Positions_Long_All,Prod_Merc_Positions_Short_All,M_Money_Positions_Long_All,"
           "M_Money_Positions_Short_All\n"
           "ICE Robusta Coffee Futures - ICE Futures Europe,07/14/2026,FutOnly,150000,40000,70000,30000,10000\n"
           "ICE Robusta Coffee Futures - ICE Futures Europe,07/14/2026,Combined,170000,45000,75000,31000,11000\n"
           "ICE Brent Crude - ICE Futures Europe,07/14/2026,FutOnly,2500000,1,1,1,1\n")
    cot = fu.parse_ice_cot(csv)
    assert len(cot) == 1 and cot["open_interest"].iloc[0] == 150000
    assert cot["mm_net_pct_oi"].iloc[0] == (30000 - 10000) / 150000
    assert cot["available_at"].iloc[0] == _ts("2026-07-17 18:30", fu.LDN)


def test_ice_cot_real_layout_keeps_futures_only_row():
    """Real ICE file: BOM, no FutOnly column, futures and futures+options rows per week."""
    hdr = ("\ufeffMarket_and_Exchange_Names,As_of_Date_In_Form_YYMMDD,As_of_Date_Form_MM/DD/YYYY,"
           "CFTC_Contract_Market_Code,CFTC_Market_Code,CFTC_Region_Code,CFTC_Commodity_Code,Open_Interest_All,"
           "Prod_Merc_Positions_Long_All,Prod_Merc_Positions_Short_All,Swap_Positions_Long_All,"
           "Swap_Positions_Short_All,Swap_Positions_Spread_All,M_Money_Positions_Long_All,M_Money_Positions_Short_All\n")
    rows = ("ICE Robusta Coffee Futures - ICE Futures Europe,260106,01/06/2026,,ICEU,6,RC,71465,25318,18460,2383,10400,901,21820,15280\n"
            "ICE Robusta Coffee Futures and Options - ICE Futures Europe,260106,01/06/2026,,ICEU,7,RC,83428,27535,20319,2383,10316,1214,20190,16676\n")
    cot = fu.parse_ice_cot(hdr + rows)
    assert len(cot) == 1 and cot["open_interest"].iloc[0] == 71465
    assert np.isclose(cot["mm_net_pct_oi"].iloc[0], (21820 - 15280) / 71465)


def test_cftc_history_file_layout():
    hdr = ("Market_and_Exchange_Names,As_of_Date_In_Form_YYMMDD,Report_Date_as_YYYY-MM-DD,"
           "CFTC_Contract_Market_Code,Open_Interest_All,Prod_Merc_Positions_Long_All,"
           "Prod_Merc_Positions_Short_All,M_Money_Positions_Long_All,M_Money_Positions_Short_All\n")
    rows = ("COFFEE C - ICE FUTURES U.S.,260714,2026-07-14,083731,166843,34093,64817,43208,10374\n"
            "COCOA - ICE FUTURES U.S.,260714,2026-07-14,073732,1,1,1,1,1\n")
    cot = fu.parse_cot_csv(hdr + rows, fu.COFFEE_C_NAME, fu.cftc_release_dates)
    assert len(cot) == 1 and cot["mm_long"].iloc[0] == 43208     # matches the CFTC API record
    assert cot["available_at"].iloc[0] == _ts("2026-07-17 15:30", fu.NY)


def test_ptax_available_same_day_after_fixing():
    p = fu.parse_ptax([{"data": "02/01/2024", "valor": "4.8916"}, {"data": "03/01/2024", "valor": "4.9200"}])
    assert p["usdbrl"].iloc[0] == 4.8916
    assert p["available_at"].iloc[0] == _ts("2024-01-02 13:30", fu.SAO)


def test_rain_anomaly_uses_only_prior_years():
    idx = pd.date_range("2000-01-01", "2003-12-31")
    rain = pd.Series(1.0, index=idx)
    rain[idx.year == 2003] = 0.5                          # dry year
    reg = pd.DataFrame({"precip": rain, "tmin": 15.0}, index=idx)
    a = fu.rain_anomaly(reg, 60)
    assert a.loc["2000"].isna().all()                     # no prior years -> no climatology
    assert np.isclose(a.loc["2003-06-01"], 0.5)           # 2003 judged against 2000-2002 only
    # changing 2003 weather can't change 2002's anomaly
    rain2 = rain.copy(); rain2[idx.year == 2003] = 5.0
    a2 = fu.rain_anomaly(pd.DataFrame({"precip": rain2, "tmin": 15.0}, index=idx), 60)
    pd.testing.assert_series_equal(a.loc[:"2002"], a2.loc[:"2002"])


def test_weather_brazil_frost_and_availability():
    idx = pd.date_range("2021-06-01", "2021-09-30")
    tmin = pd.Series(10.0, index=idx); tmin["2021-07-20"] = 0.5           # the July 2021 frost
    reg = pd.DataFrame({"tmin": tmin, "precip": 1.0}, index=idx)
    reg["available_at"] = (idx + pd.Timedelta(days=fu.WEATHER_LAG_DAYS)).tz_localize("UTC")
    s = signals.weather_brazil(reg, pd.Series(1.0, index=idx))
    assert s.loc["2021-07-19", "signal"] == 0 and s.loc["2021-07-20", "signal"] == 1
    assert s.loc["2021-07-20", "available_at"] == pd.Timestamp("2021-07-27", tz="UTC")
    assert s.loc["2021-08-20", "signal"] == 0                             # 28-day hold over


def test_parse_meteo():
    payload = {"daily": {"time": ["2024-01-01", "2024-01-02"], "temperature_2m_min": [10, 11],
                         "temperature_2m_max": [25, 26], "precipitation_sum": [0.0, 12.5]}}
    w = fu.parse_meteo(payload, "dak_lak").assign(region="VN_robusta")
    r = fu.regional_weather(w, "VN_robusta")
    assert r["precip"].iloc[1] == 12.5 and r["available_at"].iloc[0] == pd.Timestamp("2024-01-08", tz="UTC")


def test_parse_real_cert_stock_reports():
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures"
    s, long = fu.parse_cert_stock(fx / "coffee_cert_stock_20261002.xls")
    assert s["asof"] == pd.Timestamp("2026-10-02 13:24:26")
    assert s["certified"] == 260239 and s["transition"] == 201898
    assert s["pending"] == 12646 and s["rebag"] == 15798
    assert s["passed"] == 0 and s["failed"] == 0                      # "No Bags Passed Today"
    cert = long[long.section == "certified"]
    assert cert["bags"].sum() == 260239                                 # origins x ports add up
    assert cert.loc[(cert.origin == "Honduras") & (cert.port == "HOU"), "bags"].item() == 36733
    s2, _ = fu.parse_cert_stock(fx / "coffee_cert_stock_20260820.xls")
    assert s2["passed"] == 1600 and s2["failed"] == 2930


def test_cert_stock_available_after_report_time(tmp_path):
    import shutil
    from pathlib import Path
    d = tmp_path / "ice_cert_stock"; d.mkdir()
    shutil.copy(Path(__file__).parent / "fixtures" / "coffee_cert_stock_20261002.xls", d)
    c = fu.load_cert_stocks(tmp_path)
    assert c["available_at"].iloc[0] == _ts("2026-10-02 13:39:26", fu.NY)   # 13:24:26 + 15 min
    # the KC decision at 13:00 NY that day must not see it
    assert c["available_at"].iloc[0] > _ts("2026-10-02 13:00", fu.NY)


def _fake_cert(n=400, seed=0):
    from kcquant.contracts import KC
    idx = KC.tcal.business_days("2023-10-06", "2026-10-02")[:n]
    rng = np.random.default_rng(seed)
    cert = 250000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({"certified": cert, "transition": cert * 0.78,
                       "pending": np.abs(rng.normal(15000, 4000, n))}, index=idx)
    df["available_at"] = (idx + pd.Timedelta(hours=13, minutes=40)).tz_localize(fu.NY).tz_convert("UTC")
    return df


def test_h6_h8_warm_up_direction_and_no_lookahead():
    from kcquant.contracts import KC
    cert = _fake_cert()
    s6 = signals.cert_stock_change(cert, KC.tcal)
    assert s6.index[0] >= cert.index[120 + 20]           # 20-report change + 120 ranks of warm-up
    assert s6["signal"].between(-1, 1).all()
    # a sharp stock draw-down -> long; a big pending pile -> short
    c2 = cert.copy()
    c2.iloc[-1, c2.columns.get_loc("certified")] *= 0.6
    c2.iloc[-1, c2.columns.get_loc("pending")] = 200000
    assert signals.cert_stock_change(c2, KC.tcal)["signal"].iloc[-1] > 0.9
    assert signals.pending_grading(c2)["signal"].iloc[-1] < -0.9
    # changing the last report can't change any earlier signal
    pd.testing.assert_series_equal(signals.pending_grading(c2)["signal"].iloc[:-1],
                                   signals.pending_grading(cert)["signal"].iloc[:-1])


def test_h6_blank_across_report_gaps():
    from kcquant.contracts import KC
    cert = _fake_cert()
    gappy = cert.drop(cert.index[250:262])               # 12 trading days of missing files
    s = signals.cert_stock_change(gappy, KC.tcal)
    assert s.loc[gappy.index[250]:gappy.index[268]].empty  # no signal bridging the gap
