"""Fundamental data, point in time. Every loader returns a DataFrame indexed by the
date the value refers to, with an `available_at` (UTC) column = when it was published.

Sources (all free):
  * CFTC Commitments of Traders, disaggregated futures-only (Coffee C, code 083731)
  * ICE Futures Europe Commitments of Traders (Robusta Coffee)
  * Banco Central do Brasil PTAX USD/BRL (SGS series 1)
  * Open-Meteo archive (ERA5 reanalysis) daily weather for coffee regions

Raw downloads are cached under data/fundamentals/; parsing is separate from
downloading so it can be tested offline.
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .contracts import get_calendar

log = logging.getLogger(__name__)
NY, LDN, SAO = "America/New_York", "Europe/London", "America/Sao_Paulo"


def _at(dates, hhmm: str, tz: str) -> pd.DatetimeIndex:
    h, m = map(int, hhmm.split(":"))
    d = pd.DatetimeIndex(pd.to_datetime(dates)).normalize() + pd.Timedelta(hours=h, minutes=m)
    return d.tz_localize(tz).tz_convert("UTC")


UA = [{"User-Agent": "kcquant-research/0.1 (python-requests)", "Accept": "*/*"},
      {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "*/*"}]


def _get(url: str, params: dict | None = None, timeout: int = 60):
    """GET with a descriptive User-Agent, retrying once with a browser-style one
    (some government sites reject the default python-requests agent with 403)."""
    import requests
    last = None
    for h in UA:
        r = requests.get(url, params=params, timeout=timeout, headers=h)
        if r.status_code != 403:
            r.raise_for_status()
            return r
        last = r
    last.raise_for_status()


# ---------------------------------------------------------------------------
# CFTC Commitments of Traders
# ---------------------------------------------------------------------------
CFTC_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"   # disaggregated, futures only
COFFEE_C_CODE = "083731"

# Releases that did not follow the normal Friday 15:30 NY schedule (as-of date -> release date).
# 2018-19 shutdown: CFTC release 7864-19 - first catch-up report Fri 1 Feb 2019, then one report
#   each Tuesday and Friday until current (derived dates below).
# 2025 shutdown: CFTC "Historical Special Announcements", 9 Dec 2025 catch-up table.
CFTC_RELEASE_OVERRIDES = {
    "2018-12-24": "2019-02-01", "2018-12-31": "2019-02-05", "2019-01-08": "2019-02-08",
    "2019-01-15": "2019-02-12", "2019-01-22": "2019-02-15", "2019-01-29": "2019-02-19",
    "2019-02-05": "2019-02-22", "2019-02-12": "2019-02-26", "2019-02-19": "2019-03-01",
    "2019-02-26": "2019-03-05",
    "2025-09-30": "2025-11-19", "2025-10-07": "2025-11-21", "2025-10-14": "2025-11-25",
    "2025-10-21": "2025-12-02", "2025-10-28": "2025-12-05", "2025-11-04": "2025-12-09",
    "2025-11-10": "2025-12-10", "2025-11-18": "2025-12-12", "2025-11-25": "2025-12-15",
    "2025-12-02": "2025-12-17", "2025-12-09": "2025-12-19", "2025-12-16": "2025-12-23",
    "2025-12-23": "2025-12-29",
}


def _friday_after(d: pd.Timestamp) -> pd.Timestamp:
    """First Friday strictly after d (as-of dates are Tuesdays, or Mondays in holiday weeks)."""
    return d + pd.Timedelta(days=(4 - d.weekday()) % 7 or 7)


def cftc_release_dates(asof: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Normal rule: the Friday after the as-of date at 15:30 NY. If a US federal holiday falls between
    the as-of date and that Friday (inclusive), assume a delay to the next business day after
    the Friday (conservative: CFTC shifts by one business day). Known exceptions override."""
    from pandas.tseries.holiday import USFederalHolidayCalendar
    from .contracts import ExchangeCalendar
    cal = ExchangeCalendar(USFederalHolidayCalendar())      # CFTC = federal agency calendar
    out = []
    for d in pd.DatetimeIndex(asof).normalize():
        key = d.strftime("%Y-%m-%d")
        if key in CFTC_RELEASE_OVERRIDES:
            out.append(pd.Timestamp(CFTC_RELEASE_OVERRIDES[key]))
            continue
        fri = _friday_after(d)
        week = pd.date_range(d + pd.Timedelta(days=1), fri)
        if any(not cal.is_business_day(x) for x in week if x.weekday() < 5):
            fri = fri + cal.bday
        out.append(fri)
    return _at(out, "15:30", NY)


def parse_cftc(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    df["asof"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"]).dt.normalize()
    num = ["open_interest_all", "m_money_positions_long_all", "m_money_positions_short_all",
           "prod_merc_positions_long", "prod_merc_positions_short"]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    out = pd.DataFrame({
        "open_interest": df["open_interest_all"].values,
        "mm_long": df["m_money_positions_long_all"].values,
        "mm_short": df["m_money_positions_short_all"].values,
        "pm_long": df["prod_merc_positions_long"].values,
        "pm_short": df["prod_merc_positions_short"].values,
    }, index=pd.DatetimeIndex(df["asof"], name="asof")).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out["available_at"] = cftc_release_dates(out.index)
    return _cot_derived(out)


def _cot_derived(out: pd.DataFrame) -> pd.DataFrame:
    out["mm_net"] = out["mm_long"] - out["mm_short"]
    out["mm_net_pct_oi"] = out["mm_net"] / out["open_interest"]
    return out


CFTC_ZIP_URL = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
COFFEE_C_NAME = r"(?i)^coffee c - ice futures u\.?s\.?"


def fetch_cftc(cache_dir: Path, start="2015-01-01") -> pd.DataFrame:
    """Try the CFTC Socrata API; if refused, fall back to the yearly history zip files."""
    try:
        params = {"cftc_contract_market_code": COFFEE_C_CODE,
                  "$order": "report_date_as_yyyy_mm_dd", "$limit": 50000}
        recs = [r for r in _get(CFTC_URL, params).json() if r["report_date_as_yyyy_mm_dd"][:10] >= start]
        (cache_dir / "cftc_coffee_c.json").write_text(json.dumps(recs))
        return parse_cftc(recs)
    except Exception as err:
        log.warning("CFTC API failed (%s); using yearly history zips", str(err)[:120])
    d = cache_dir / "cftc"
    d.mkdir(parents=True, exist_ok=True)
    for y in range(pd.Timestamp(start).year, pd.Timestamp.today().year + 1):
        f = d / f"fut_disagg_txt_{y}.zip"
        if not f.exists() or y == pd.Timestamp.today().year:
            f.write_bytes(_get(CFTC_ZIP_URL.format(year=y), timeout=120).content)
    return load_cftc_zips(cache_dir)


def load_cftc_zips(cache_dir: Path) -> pd.DataFrame:
    import zipfile
    parts = []
    for f in sorted((cache_dir / "cftc").glob("*.zip")):
        with zipfile.ZipFile(f) as z:
            text = z.read(z.namelist()[0]).decode("latin-1")
        parts.append(parse_cot_csv(text, COFFEE_C_NAME, cftc_release_dates))
    out = pd.concat(parts).sort_index()
    return out[~out.index.duplicated(keep="last")]


# ---------------------------------------------------------------------------
# ICE Futures Europe Commitments of Traders (robusta)
# ---------------------------------------------------------------------------
ICE_COT_URL = "https://www.ice.com/publicdocs/futures/COTHist{year}.csv"   # best guess; see README


def ice_release_dates(asof: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """ICE Futures Europe: Friday of the same week at 18:30 London; if that Friday is not a
    trading day, the next one (assumption, conservative)."""
    cal = get_calendar("ICE_EU")
    out = []
    for d in pd.DatetimeIndex(asof).normalize():
        fri = _friday_after(d)
        out.append(fri if cal.is_business_day(fri) else cal.rollforward(fri + pd.Timedelta(days=1)))
    return _at(out, "18:30", LDN)


def _find(cols, *patterns):
    for p in patterns:
        for c in cols:
            if re.fullmatch(p, c):
                return c
    raise KeyError(f"no column matching {patterns}; columns: {list(cols)[:40]}")


def parse_cot_csv(csv_text: str, include: str, release_fn, exclude: str = r"(?i).*option.*") -> pd.DataFrame:
    """Parse a CFTC-style disaggregated COT CSV (CFTC history files and ICE Futures Europe use the
    same layout). Keeps rows whose market name matches `include` and not `exclude`: ICE lists
    'Futures' and 'Futures and Options' rows with no other flag, so options rows are dropped."""
    df = pd.read_csv(io.StringIO(csv_text.lstrip("\ufeff")), low_memory=False)
    df.columns = [c.strip().lstrip("\ufeff").lower().replace(" ", "_") for c in df.columns]
    name = _find(df.columns, r"market_and_exchange_names?", r".*market.*name.*")
    date = _find(df.columns, r"report_date_as_yyyy-mm-dd", r"as_of_date_form_mm/dd/yyyy", r"report_date.*")
    oi = _find(df.columns, r"open_interest_all", r"open_interest.*")
    mml = _find(df.columns, r"m_money_positions?_long_all", r"m_money.*long.*")
    mms = _find(df.columns, r"m_money_positions?_short_all", r"m_money.*short.*")
    pml = _find(df.columns, r"prod_merc_positions?_long(_all)?", r"prod_merc.*long.*")
    pms = _find(df.columns, r"prod_merc_positions?_short(_all)?", r"prod_merc.*short.*")
    names = df[name].astype(str).str.strip()
    df = df[names.str.match(include) & ~names.str.match(exclude)]
    fo = [c for c in df.columns if "futonly" in c or "combined" in c]
    if fo:
        df = df[df[fo[0]].astype(str).str.lower().str.contains("fut")]
    out = pd.DataFrame({
        "open_interest": pd.to_numeric(df[oi], errors="coerce").values,
        "mm_long": pd.to_numeric(df[mml], errors="coerce").values,
        "mm_short": pd.to_numeric(df[mms], errors="coerce").values,
        "pm_long": pd.to_numeric(df[pml], errors="coerce").values,
        "pm_short": pd.to_numeric(df[pms], errors="coerce").values,
    }, index=pd.DatetimeIndex(pd.to_datetime(df[date], dayfirst=False), name="asof").normalize())
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out["available_at"] = release_fn(out.index)
    return _cot_derived(out)


def parse_ice_cot(csv_text: str) -> pd.DataFrame:
    """ICE Futures Europe COT, robusta futures only."""
    return parse_cot_csv(csv_text, r"(?i)^ice robusta coffee futures\b", ice_release_dates)


def fetch_ice_cot(cache_dir: Path, years) -> pd.DataFrame:
    """Download ICE history files; any year that fails can be dropped manually into
    data/fundamentals/ice_cot/ as COTHist<year>.csv (from ICE Report Center)."""
    d = cache_dir / "ice_cot"
    d.mkdir(parents=True, exist_ok=True)
    for y in years:
        f = d / f"COTHist{y}.csv"
        if f.exists() and y < pd.Timestamp.today().year:
            continue
        try:
            f.write_bytes(_get(ICE_COT_URL.format(year=y)).content)
        except Exception as err:
            log.warning("ICE COT %s not downloaded (%s) - place the CSV in %s manually", y, err, d)
    return load_ice_cot(cache_dir)


def load_ice_cot(cache_dir: Path) -> pd.DataFrame:
    files = sorted((cache_dir / "ice_cot").glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"no ICE COT CSVs in {cache_dir / 'ice_cot'}")
    parts = []
    for f in files:
        try:
            parts.append(parse_ice_cot(f.read_text(errors="replace")))
        except Exception as err:
            log.warning("could not parse %s: %s", f.name, err)
    out = pd.concat(parts).sort_index()
    return out[~out.index.duplicated(keep="last")]


# ---------------------------------------------------------------------------
# BCB PTAX USD/BRL
# ---------------------------------------------------------------------------
PTAX_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados"


def parse_ptax(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    idx = pd.DatetimeIndex(pd.to_datetime(df["data"], format="%d/%m/%Y"), name="date")
    out = pd.DataFrame({"usdbrl": pd.to_numeric(df["valor"], errors="coerce").values}, index=idx)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out["available_at"] = _at(out.index, "13:30", SAO)          # PTAX closes ~13:10 São Paulo
    return out


def fetch_ptax(cache_dir: Path, start="2015-01-01", end=None) -> pd.DataFrame:
    end = pd.Timestamp(end or pd.Timestamp.today())
    recs = []
    for y in range(pd.Timestamp(start).year, end.year + 1):     # API limits the span per call
        p = {"formato": "json", "dataInicial": f"01/01/{y}", "dataFinal": f"31/12/{y}"}
        recs += _get(PTAX_URL, p).json()
    (cache_dir / "ptax_usdbrl.json").write_text(json.dumps(recs))
    return parse_ptax(recs)


# ---------------------------------------------------------------------------
# Weather (Open-Meteo archive, ERA5)
# ---------------------------------------------------------------------------
METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_LAG_DAYS = 7     # ERA5 appears with ~5 days delay; use 7 to be safe
REGIONS = {
    "BR_arabica": {"sul_de_minas": (-21.55, -45.43), "cerrado_mineiro": (-18.94, -46.99),
                   "mogiana": (-20.54, -47.40)},
    "VN_robusta": {"dak_lak": (12.67, 108.04), "gia_lai": (13.98, 108.00), "dak_nong": (12.00, 107.69)},
}


def parse_meteo(payload: dict, location: str) -> pd.DataFrame:
    d = payload["daily"]
    out = pd.DataFrame({"tmin": d["temperature_2m_min"], "tmax": d["temperature_2m_max"],
                        "precip": d["precipitation_sum"]},
                       index=pd.DatetimeIndex(pd.to_datetime(d["time"]), name="date"))
    out["location"] = location
    return out


def fetch_weather(cache_dir: Path, start="2000-01-01", end=None) -> pd.DataFrame:
    # the ERA5 archive runs ~5 days behind; asking for later dates returns 400 Bad Request
    end = pd.Timestamp(end or pd.Timestamp.today() - pd.Timedelta(days=WEATHER_LAG_DAYS)).strftime("%Y-%m-%d")
    import time
    import requests
    for region, locs in REGIONS.items():
        for name, (lat, lon) in locs.items():
            f = cache_dir / f"weather_{name}.json"
            if f.exists():      # skip locations already downloaded up to ~2 weeks ago
                last = parse_meteo(json.loads(f.read_text()), name).index.max()
                if last >= pd.Timestamp(end) - pd.Timedelta(days=14):
                    continue
            p = {"latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
                 "daily": "temperature_2m_min,temperature_2m_max,precipitation_sum", "timezone": "UTC"}
            # Long histories count as many "calls" against Open-Meteo's free per-minute limit:
            # on 429, wait for the limit window to reset and retry.
            for attempt in range(6):
                try:
                    payload = _get(METEO_URL, p, timeout=120).json()
                    break
                except requests.HTTPError as err:
                    if err.response is None or err.response.status_code != 429 or attempt == 5:
                        raise
                    log.info("Open-Meteo rate limit; waiting 65 s (%s, attempt %d)", name, attempt + 1)
                    time.sleep(65)
            f.write_text(json.dumps(payload))
            log.info("weather %s downloaded", name)
            time.sleep(10)      # be gentle between locations
    return load_weather(cache_dir)


def load_weather(cache_dir: Path) -> pd.DataFrame:
    frames = []
    for region, locs in REGIONS.items():
        for name in locs:
            f = cache_dir / f"weather_{name}.json"
            if f.exists():
                frames.append(parse_meteo(json.loads(f.read_text()), name).assign(region=region))
    if not frames:
        raise FileNotFoundError(f"no weather files in {cache_dir}")
    return pd.concat(frames)


def regional_weather(wx: pd.DataFrame, region: str) -> pd.DataFrame:
    """Daily regional series: coldest min temperature and mean rainfall across the region's
    locations, with available_at = date + WEATHER_LAG_DAYS (00:00 UTC)."""
    w = wx[wx["region"] == region]
    out = pd.DataFrame({"tmin": w.groupby(level=0)["tmin"].min(),
                        "precip": w.groupby(level=0)["precip"].mean()})
    out = out.dropna(how="all").sort_index()
    out["available_at"] = (out.index + pd.Timedelta(days=WEATHER_LAG_DAYS)).tz_localize("UTC")
    return out


def rain_anomaly(regional: pd.DataFrame, window: int) -> pd.Series:
    """rain over the last `window` days / average rain for the same calendar window in PRIOR
    years only (expanding climatology), so the ratio never uses future weather."""
    roll = regional["precip"].rolling(window, min_periods=int(window * 0.9)).sum()
    df = pd.DataFrame({"r": roll, "doy": roll.index.dayofyear, "yr": roll.index.year})
    clim = pd.Series(np.nan, index=df.index)
    for y in sorted(df["yr"].unique()):
        past = df[df["yr"] < y].groupby("doy")["r"].mean()
        if past.empty:
            continue
        past = past.reindex(range(1, 367)).interpolate(limit_direction="both")
        m = df["yr"] == y
        clim[m] = past.reindex(df.loc[m, "doy"]).values
    return roll / clim


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------
def load_all(cache_dir: Path) -> dict:
    """Load whatever has been downloaded (missing sources are skipped with a warning)."""
    out = {}
    loaders = {
        "cot_kc": lambda: (parse_cftc(json.loads((cache_dir / "cftc_coffee_c.json").read_text()))
                           if (cache_dir / "cftc_coffee_c.json").exists() else load_cftc_zips(cache_dir)),
        "cot_rc": lambda: load_ice_cot(cache_dir),
        "ptax": lambda: parse_ptax(json.loads((cache_dir / "ptax_usdbrl.json").read_text())),
        "weather": lambda: load_weather(cache_dir),
        "cert_kc": lambda: load_cert_stocks(cache_dir),
    }
    for k, f in loaders.items():
        try:
            out[k] = f()
        except Exception as err:
            log.warning("%s not available: %s", k, err)
    return out


# ---------------------------------------------------------------------------
# ICE Coffee "C" certified warehouse stocks (daily .xls reports)
# ---------------------------------------------------------------------------
CERT_SECTIONS = {                       # report heading -> output column
    "TOTAL BAGS CERTIFIED": "certified",
    "TRANSITION BAGS CERTIFIED": "transition",
    "BAGS PASSED GRADING": "passed",
    "BAGS FAILED GRADING": "failed",
    "PENDING GRADING REPORT": "pending",
    "FLAGGED FOR REBAGGING": "rebag",
}
CERT_PUBLISH_BUFFER = pd.Timedelta(minutes=15)   # report is stamped ~13:15-13:25 NY; allow for upload


def _cert_rows(path) -> list[list[str]]:
    import xlrd
    sh = xlrd.open_workbook(str(path)).sheet_by_index(0)
    out = []
    for r in range(sh.nrows):
        out.append([str(v).strip() for v in sh.row_values(r)])
    return out


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return np.nan


def parse_cert_stock(path) -> tuple[dict, pd.DataFrame]:
    """One daily report -> (summary dict, long table of bags by section/origin/port).

    Sections are located by heading text, tables read until their 'Total in Bags' row.
    'No Bags Passed/Failed Today' -> 0. A section missing from the report -> NaN.
    """
    rows = _cert_rows(path)
    cells = [[c for c in r if c] for r in rows]
    asof = None
    for r in cells[:6]:
        line = " ".join(r)
        m = re.search(r"As of:\s*(.+)$", line)
        if m:
            asof = pd.to_datetime(re.sub(r"\s+", " ", m.group(1)), format="mixed")
    summary = {"asof": asof}
    long = []
    heads = {i: " ".join(r).upper() for i, r in enumerate(cells) if r}
    for i, text in heads.items():
        for title, col in CERT_SECTIONS.items():
            if text.strip() != title:
                continue
            ports = None
            for k in range(i + 1, min(i + 60, len(cells))):
                r = cells[k]
                if not r:
                    continue
                if ports is None:
                    if r[-1].lower() == "total":
                        ports = r                                   # header: ports ... Total
                        continue
                    if r[0].upper() in CERT_SECTIONS or "NO BAGS" in r[0].upper():
                        break
                    continue
                if r[0].lower().startswith("total in bags"):
                    summary[col] = _num(r[-1])
                    break
                vals = r[1:]
                for p, v in zip(ports[:-1], vals[:-1]):
                    long.append({"section": col, "origin": r[0], "port": p, "bags": _num(v)})
    for t in ("passed", "failed"):
        if t not in summary and any(f"NO BAGS {t.upper()}" in h for h in heads.values()):
            summary[t] = 0.0
    if "pending" not in summary and any("PENDING GRADING" in h for h in heads.values()):
        summary["pending"] = 0.0
    return summary, pd.DataFrame(long)


def load_cert_stocks(cache_dir: Path, with_detail: bool = False):
    """All downloaded daily reports -> DataFrame indexed by report date with available_at."""
    d = cache_dir / "ice_cert_stock"
    rows, details = [], []
    for f in sorted(d.glob("coffee_cert_stock_*.xls")):
        day = pd.Timestamp(f.stem.split("_")[-1])
        try:
            s, long = parse_cert_stock(f)
        except Exception as err:
            log.warning("cannot parse %s: %s", f.name, err)
            continue
        asof = s.pop("asof") or (day + pd.Timedelta(hours=13, minutes=30))
        if asof.normalize() != day:
            log.warning("%s: as-of %s differs from file date", f.name, asof)
        s.update(date=day, asof_local=asof)
        rows.append(s)
        if with_detail and len(long):
            details.append(long.assign(date=day))
    if not rows:
        raise FileNotFoundError(f"no certified stock reports in {d}")
    out = pd.DataFrame(rows).set_index("date").sort_index()
    out["available_at"] = (pd.DatetimeIndex(out["asof_local"]) + CERT_PUBLISH_BUFFER) \
        .tz_localize(NY).tz_convert("UTC")
    out = out.drop(columns="asof_local")
    if with_detail:
        return out, (pd.concat(details, ignore_index=True) if details else pd.DataFrame())
    return out
