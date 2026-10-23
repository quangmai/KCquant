"""Contract-level daily data: canonical panel, loaders, synthetic generator.

Canonical panel ("long" format, one row per trade_date x contract):

    trade_date           datetime64[ns]  exchange trading-session date (naive)
    contract             str             e.g. "KCH26"
    settle               float           daily settlement, quoted units (KC: cents/lb)
    volume               float           contracts
    open_interest        float           contracts, *as of the end of trade_date*
    settle_available_at  datetime64[ns, UTC]  first moment the settle was knowable
    volume_available_at  datetime64[ns, UTC]
    oi_available_at      datetime64[ns, UTC]

The `*_available_at` columns are the backbone of point-in-time discipline: any
downstream computation may only use a value at times >= its available_at.
For Databento they come from `ts_recv` (when Databento captured the message);
for CSV/synthetic data they are filled by `apply_default_availability` from
the spec's publication schedule (settle same day after the close, OI on the
morning of the next business day).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .contracts import ContractSpec, CODE_TO_MONTH

log = logging.getLogger(__name__)

PANEL_COLUMNS = ["trade_date", "contract", "settle", "volume", "open_interest",
                 "settle_available_at", "volume_available_at", "oi_available_at"]
AVAIL_COLUMNS = ["settle_available_at", "volume_available_at", "oi_available_at"]


# ---------------------------------------------------------------------------
# Canonical panel helpers
# ---------------------------------------------------------------------------
def _local_time_to_utc(dates: pd.Series, hhmm: str, tz: str) -> pd.Series:
    h, m = map(int, hhmm.split(":"))
    local = pd.to_datetime(dates).dt.normalize() + pd.Timedelta(hours=h, minutes=m)
    return local.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous=False).dt.tz_convert("UTC")


def apply_default_availability(panel: pd.DataFrame, spec: ContractSpec,
                               overwrite: bool = False) -> pd.DataFrame:
    """Fill missing *_available_at columns from the spec's publication schedule.

    settle/volume: trade_date at spec.settle_time (exchange local time)
    open interest: NEXT business day at spec.oi_publish_time (ICE publishes
                   OI for session t on the morning of t+1)
    """
    out = panel.copy()
    td = pd.to_datetime(out["trade_date"])
    uniq = pd.DatetimeIndex(td.unique())
    next_bday = td.map(dict(zip(uniq, [d + spec.tcal.bday for d in uniq])))
    defaults = {
        "settle_available_at": _local_time_to_utc(td, spec.settle_time, spec.timezone),
        "volume_available_at": _local_time_to_utc(td, spec.settle_time, spec.timezone),
        "oi_available_at": _local_time_to_utc(next_bday, spec.oi_publish_time, spec.timezone),
    }
    for col, val in defaults.items():
        if overwrite or col not in out.columns:
            out[col] = val
        else:
            out[col] = pd.to_datetime(out[col], utc=True).fillna(val)
    return out


def validate_panel(panel: pd.DataFrame, spec: ContractSpec | None = None) -> pd.DataFrame:
    """Check schema and basic PIT sanity; returns a sorted copy."""
    missing = set(PANEL_COLUMNS) - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing columns: {sorted(missing)}")
    p = panel[PANEL_COLUMNS + [c for c in panel.columns if c not in PANEL_COLUMNS]].copy()
    p["trade_date"] = pd.to_datetime(p["trade_date"]).dt.normalize()
    for c in AVAIL_COLUMNS:
        p[c] = pd.to_datetime(p[c], utc=True)
    if p.duplicated(["trade_date", "contract"]).any():
        raise ValueError("duplicate (trade_date, contract) rows")
    if (p["settle"] <= 0).any():
        raise ValueError("non-positive settle prices")
    # A value can never be known before its session starts (midnight UTC of
    # trade_date is a conservative lower bound for ICE US/EU sessions).
    floor = p["trade_date"].dt.tz_localize("UTC")
    for c in AVAIL_COLUMNS:
        bad = p[c].notna() & (p[c] < floor)
        if bad.any():
            raise ValueError(f"{c} earlier than its trade_date for {int(bad.sum())} rows")
    if spec is not None:
        for cid in p["contract"].unique():
            spec.parse(cid)
    return p.sort_values(["trade_date", "contract"]).reset_index(drop=True)


def to_wide(panel: pd.DataFrame, field: str = "settle") -> pd.DataFrame:
    """trade_date x contract matrix of one field (the raw curve)."""
    w = panel.pivot(index="trade_date", columns="contract", values=field)
    return w


def load_csv(path: str | Path, spec: ContractSpec) -> pd.DataFrame:
    """Load a long-format CSV/Parquet with at least trade_date, contract, settle,
    volume, open_interest. Missing availability columns get defaults."""
    path = Path(path)
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    return validate_panel(apply_default_availability(df, spec), spec)


# ---------------------------------------------------------------------------
# Databento
# ---------------------------------------------------------------------------
STAT_SETTLEMENT = 3
STAT_CLEARED_VOLUME = 6
STAT_OPEN_INTEREST = 9
UPDATE_ADD = 1
STATFLAG_FINAL = 1  # bit 0; IFUS normalises ICE "IsOfficial" into stat_flags. Verify on first pull.


def raw_symbol_to_contract(raw_symbol: str, spec: ContractSpec) -> str | None:
    """'KC  FMH0026!' -> 'KCH26'. Returns None for spreads/options/other roots."""
    m = re.fullmatch(rf"{re.escape(spec.exchange_root)}\s*FM([A-Z])\d{{2}}(\d{{2}})!?", raw_symbol.strip())
    if not m or m.group(1) not in CODE_TO_MONTH:
        return None
    return f"{spec.root}{m.group(1)}{m.group(2)}"


def _to_utc(s: pd.Series) -> pd.Series:
    if pd.api.types.is_integer_dtype(s):
        s = s.where(s < np.iinfo(np.int64).max)  # UNDEF_TIMESTAMP -> NaN
        return pd.to_datetime(s, unit="ns", utc=True)
    return pd.to_datetime(s, utc=True)


def normalize_databento_statistics(stats: pd.DataFrame, spec: ContractSpec,
                                   ohlcv: pd.DataFrame | None = None,
                                   oi_without_ts_ref: str = "previous_bday") -> pd.DataFrame:
    """Turn a Databento `statistics` DataFrame (from DBNStore.to_df(), with a
    `symbol` column and ts_recv as index or column) into the canonical panel.

    Point-in-time rules applied here:
      * The value kept per (contract, trade_date) is the final/official one if
        flagged, else the latest received; its available_at is the earliest
        ts_recv at which *that same value* was published (never earlier).
      * trade_date comes from ts_ref (the session the statistic refers to). If
        ts_ref is missing: settle -> local date of ts_event; OI -> previous
        business day of the local ts_event date (OI is published next morning),
        controlled by `oi_without_ts_ref` ("previous_bday" | "same_day").
    """
    df = stats.reset_index() if "ts_recv" not in stats.columns else stats.copy()
    df = df[df["update_action"] == UPDATE_ADD]
    df["contract"] = df["symbol"].map(lambda s: raw_symbol_to_contract(str(s), spec))
    df = df[df["contract"].notna()]
    df["ts_recv"] = _to_utc(df["ts_recv"])
    df["ts_event"] = _to_utc(df["ts_event"])
    ts_ref = _to_utc(df["ts_ref"]) if "ts_ref" in df.columns else pd.Series(pd.NaT, index=df.index)
    # ts_ref of 0 (epoch) also means "not set"
    ts_ref = ts_ref.where(ts_ref > pd.Timestamp("1990-01-01", tz="UTC"))
    local_event_date = df["ts_event"].dt.tz_convert(spec.timezone).dt.tz_localize(None).dt.normalize()
    # ts_ref encodes a session DATE (OI: 00:00 UTC, settle: 12:00 UTC). Take the UTC
    # calendar date; converting to New York first would shift midnight refs a day back.
    ref_date = ts_ref.dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()

    def pick(stat_type: int, value_col: str, fallback_date: pd.Series, name: str,
             prefer_final: bool) -> pd.DataFrame:
        sub = df[df["stat_type"] == stat_type].copy()
        if sub.empty:
            return pd.DataFrame(columns=["trade_date", "contract", name, f"{name}_available_at"])
        sub["trade_date"] = ref_date.loc[sub.index].fillna(fallback_date.loc[sub.index])
        sub["val"] = sub[value_col].astype(float)
        sub["final"] = (sub["stat_flags"].astype(int) & STATFLAG_FINAL) > 0 if prefer_final else True
        sub = sub.sort_values("ts_recv")
        rows = []
        for (td, c), g in sub.groupby(["trade_date", "contract"], sort=False):
            pool = g[g["final"]] if g["final"].any() else g
            v = pool["val"].iloc[-1]
            first_seen = pool.loc[np.isclose(pool["val"], v), "ts_recv"].min()
            rows.append((td, c, v, first_seen))
        return pd.DataFrame(rows, columns=["trade_date", "contract", name, f"{name}_available_at"])

    oi_fallback = local_event_date
    if oi_without_ts_ref == "previous_bday":
        # The previous session is taken from the days that actually have settlements in
        # the data (the exchange's real trading days), falling back to the calendar when
        # the data has no session in the prior week. Only past days are used: no look-ahead.
        st = df[df["stat_type"] == STAT_SETTLEMENT]
        sessions = pd.DatetimeIndex(sorted(set(
            ref_date.loc[st.index].fillna(local_event_date.loc[st.index]).dropna())))

        def prev_session(d):
            if pd.isna(d):
                return d
            i = sessions.searchsorted(d) - 1          # last session strictly before d
            if i >= 0 and (d - sessions[i]).days <= 7:
                return sessions[i]
            return d - spec.tcal.bday
        oi_fallback = local_event_date.map(prev_session)
    settle = pick(STAT_SETTLEMENT, "price", local_event_date, "settle", prefer_final=True)
    oi = pick(STAT_OPEN_INTEREST, "quantity", oi_fallback, "open_interest", prefer_final=False)
    vol = pick(STAT_CLEARED_VOLUME, "quantity", local_event_date, "volume", prefer_final=False)
    oi = oi.rename(columns={"open_interest_available_at": "oi_available_at"})

    if vol.empty and ohlcv is not None:
        o = ohlcv.reset_index() if "ts_event" not in ohlcv.columns else ohlcv.copy()
        o["contract"] = o["symbol"].map(lambda s: raw_symbol_to_contract(str(s), spec))
        o = o[o["contract"].notna()]
        o["trade_date"] = _to_utc(o["ts_event"]).dt.tz_localize(None).dt.normalize()
        vol = o.groupby(["trade_date", "contract"], as_index=False)["volume"].sum()
        # bar closes at end of UTC day; that's when the total is knowable
        vol["volume_available_at"] = (vol["trade_date"] + pd.Timedelta(days=1)).dt.tz_localize("UTC")

    panel = settle.merge(oi, on=["trade_date", "contract"], how="left") \
                  .merge(vol, on=["trade_date", "contract"], how="left")
    for c in ["volume", "open_interest"]:
        if c not in panel:
            panel[c] = np.nan
    return validate_panel(apply_default_availability(panel, spec), spec)


class DatabentoLoader:
    """Download + cache contract-level daily data from Databento.

    Requests outright futures by explicit raw symbol (e.g. 'KC  FMH0026!'),
    which avoids relying on parent-symbol conventions for ICE datasets.
    Raw DBN files are cached under `cache_dir`; re-runs read from disk.
    Requires `pip install databento` and DATABENTO_API_KEY in the environment.
    """

    def __init__(self, spec: ContractSpec, cache_dir: str | Path = "data/raw",
                 api_key: str | None = None, max_cost_usd: float = 50.0, client=None,
                 window_months: int | None = None):
        """window_months: if set, request each contract only from `window_months`
        before its delivery month until its last trading day (one cached file
        per contract; finished contracts are never downloaded again)."""
        self.spec, self.cache_dir = spec, Path(cache_dir)
        self.api_key, self.max_cost_usd = api_key, max_cost_usd
        self.window_months = window_months
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client, self._range = client, None

    @property
    def client(self):
        if self._client is None:
            import databento as db
            self._client = db.Historical(self.api_key)
        return self._client

    def symbols(self, start, end, months_ahead: int = 24) -> list[str]:
        end_delivery = pd.Timestamp(end) + pd.DateOffset(months=months_ahead)
        out = []
        for cid in self.spec.contracts_between(start, end_delivery):
            y, m = self.spec.parse(cid)
            out.append(self.spec.databento_raw_symbol(y, m))
        return out

    SCHEMAS = ("statistics", "ohlcv-1d", "definition")

    def available_range(self, schema: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        """(first date, exclusive end date) Databento has for this dataset/schema."""
        if self._range is None:
            self._range = self.client.metadata.get_dataset_range(dataset=self.spec.databento_dataset)
        r = self._range
        sch = (r.get("schema") or {}).get(schema) or r
        lo = pd.Timestamp(sch["start"]).tz_convert(None) if pd.Timestamp(sch["start"]).tzinfo \
            else pd.Timestamp(sch["start"])
        hi = pd.Timestamp(sch["end"]).tz_convert(None) if pd.Timestamp(sch["end"]).tzinfo \
            else pd.Timestamp(sch["end"])
        # whole days only: start rounds up, (exclusive) end rounds down
        return lo.ceil("D"), hi.floor("D")

    def clamp(self, schema: str, start, end) -> tuple[pd.Timestamp, pd.Timestamp]:
        lo, hi = self.available_range(schema)
        s, e = max(pd.Timestamp(start), lo), min(pd.Timestamp(end), hi)
        if (s, e) != (pd.Timestamp(start), pd.Timestamp(end)):
            log.info("%s: requested %s..%s, using available %s..%s (end exclusive)", schema,
                     pd.Timestamp(start).date(), pd.Timestamp(end).date(), s.date(), e.date())
        if s >= e:
            raise ValueError(f"{schema}: no data between {start} and {end} (available {lo.date()}..{hi.date()})")
        return s, e

    def _request(self, schema, start, end, symbols):
        return dict(dataset=self.spec.databento_dataset, schema=schema, symbols=symbols,
                    stype_in="raw_symbol", start=pd.Timestamp(start).strftime("%Y-%m-%d"),
                    end=pd.Timestamp(end).strftime("%Y-%m-%d"))

    # -- per-contract windows ------------------------------------------------
    def contract_windows(self, start, end, window_months: int | None) -> list[tuple]:
        """[(contract, raw_symbol, start, end)] limited to each contract's window."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        out = []
        for sym in self.symbols(start, end):
            cid = raw_symbol_to_contract(sym, self.spec)
            y, m = self.spec.parse(cid)
            s, e = start, end
            if window_months is not None:
                s = max(s, pd.Timestamp(year=y, month=m, day=1) - pd.DateOffset(months=window_months))
                e = min(e, self.spec.last_trading_day(y, m) + pd.Timedelta(days=1))
            if s < e:
                out.append((cid, sym, s, e))
        return out

    def _cost(self, schema, start, end, symbols) -> float:
        s, e = self.clamp(schema, start, end)
        return self.client.metadata.get_cost(**self._request(schema, s, e, symbols))

    def _cost_or_none(self, schema, start, end, symbols):
        """Like _cost, but returns (None, reason) when Databento can't price the request
        (e.g. a single symbol with no data in the window)."""
        try:
            return self._cost(schema, start, end, symbols), ""
        except Exception as err:  # BentoClientError / ValueError from clamp
            return None, str(err).splitlines()[0][:120]

    def estimate_costs(self, start, end, window_months: int | None = "default") -> dict:
        """USD cost estimate per schema; downloads nothing (get_cost is free)."""
        wm = self.window_months if window_months == "default" else window_months
        out = {}
        for sch in self.SCHEMAS:
            if wm is None or sch == "definition":
                out[sch] = self._cost(sch, start, end, self.symbols(start, end))
            else:
                costs = [self._cost_or_none(sch, s, e, [sym])[0]
                         for _, sym, s, e in self.contract_windows(start, end, wm)]
                out[sch] = sum(c for c in costs if c is not None)
        return out

    def cost_breakdown(self, start, end, schema: str = "statistics",
                       window_months: int | None = None) -> pd.DataFrame:
        """Free per-contract cost estimate, to see where the money goes."""
        rows = []
        for cid, sym, s, e in self.contract_windows(start, end, window_months):
            cost, why = self._cost_or_none(schema, s, e, [sym])
            rows.append((cid, s.date(), e.date(), cost, why))
        df = pd.DataFrame(rows, columns=["contract", "start", "end", "cost_usd", "note"])
        return df.sort_values("cost_usd", ascending=False, na_position="last").reset_index(drop=True)

    # -- download --------------------------------------------------------------
    def _path(self, schema, start, end, label) -> Path:
        try:
            s, e = self.clamp(schema, start, end)
        except ValueError:
            return self.cache_dir / "__none__"
        return self.cache_dir / f"{label}_{schema}_{s:%Y%m%d}_{e:%Y%m%d}.dbn.zst"

    def _get(self, schema: str, start, end, symbols: list[str], label: str | None = None):
        import databento as db
        start, end = self.clamp(schema, start, end)
        label = label or self.spec.root
        path = self._path(schema, start, end, label)
        if not path.exists():
            kw = self._request(schema, start, end, symbols)
            cost = self.client.metadata.get_cost(**kw)
            log.info("Databento %s %s cost estimate: $%.2f", label, schema, cost)
            if cost > self.max_cost_usd:
                raise RuntimeError(f"{label} {schema} would cost ${cost:.2f} > max_cost_usd "
                                   f"(${self.max_cost_usd:.2f}); raise --max-cost to proceed")
            self.client.timeseries.get_range(path=str(path), **kw)
        return db.DBNStore.from_file(str(path))

    def _get_df(self, schema, start, end) -> pd.DataFrame:
        if self.window_months is None:
            return self._get(schema, start, end, self.symbols(start, end)).to_df()
        wins = self.contract_windows(start, end, self.window_months)
        todo = [(cid, sym, s, e) for cid, sym, s, e in wins
                if not self._path(schema, s, e, f"{self.spec.root}_{cid}").exists()]
        priced = [(w, self._cost_or_none(schema, w[2], w[3], [w[1]])) for w in todo]
        for (cid, *_), (c, why) in priced:
            if c is None:
                log.info("skip %s %s: %s", cid, schema, why)
        todo = [w for w, (c, _) in priced if c is not None]
        total = sum(c for _, (c, _) in priced if c is not None)
        log.info("Databento %s: %d contract windows, %d to download, est. $%.2f",
                 schema, len(wins), len(todo), total)
        if total > self.max_cost_usd:
            raise RuntimeError(f"{schema} download would cost ${total:.2f} > max_cost_usd "
                               f"(${self.max_cost_usd:.2f}); raise --max-cost to proceed")
        frames = []
        skipped = {w[0] for w in wins} - {w[0] for w in todo}
        for cid, sym, s, e in wins:
            if cid in skipped and not self._path(schema, s, e, f"{self.spec.root}_{cid}").exists():
                continue
            frames.append(self._get(schema, s, e, [sym], label=f"{self.spec.root}_{cid}").to_df())
        frames = [f for f in frames if len(f)]
        return pd.concat(frames) if frames else pd.DataFrame()

    def load(self, start, end, with_definitions: bool = True) -> pd.DataFrame:
        stats = self._get_df("statistics", start, end)
        ohlcv = self._get_df("ohlcv-1d", start, end)
        panel = normalize_databento_statistics(stats, self.spec, ohlcv=ohlcv)
        if with_definitions:
            try:
                defs = self._get("definition", start, end, self.symbols(start, end)).to_df()
                mism = validate_expirations(defs, self.spec)
                if len(mism):
                    log.warning("Calendar vs Databento expiration mismatches:\n%s", mism)
            except Exception as e:  # definitions are a cross-check only
                log.warning("definition check skipped: %s", e)
        return panel


class SnapshotStatisticsLoader(DatabentoLoader):
    """Cheap variant: fetch only a short daily window of the `statistics` feed.

    Databento's ICE US feed carries a once-a-day snapshot (observed at 18:00
    New York time) holding settle, open interest, cleared volume, open, high,
    low and VWAP for every contract: ~7 records per contract per day instead
    of ~19,000 in the full feed. We request only [window_start, window_end)
    local time on each weekday, so the cost is a tiny fraction of the full pull.

    Point-in-time: values are stamped with the snapshot's ts_recv (18:00), not
    the earlier intraday publication, so they're slightly conservative.
    Snapshot records carry no ts_ref: settle/volume are dated by the snapshot's
    local date, OI by the previous business day (it is the OI published that
    morning for the prior session). `normalize_databento_statistics` does this.

    Cache: one parquet per month plus the list of days already requested, so
    re-runs only fetch new days. Weekdays the calendar treats as holidays are
    requested too (they come back empty and cost nothing), so a calendar error
    can't silently drop a trading day.
    """

    # Probe of 16 days 2019-2026: the evening snapshot ran at 18:00:00 (once 17:59:59,
    # once missing); the 13:30:00 snapshot (once 13:29:59) was present every day.
    # A third, wider window catches early-close days (e.g. 24 Dec), when neither
    # snapshot runs at its usual time. Later windows are only tried if earlier
    # ones returned no settle, so on normal days they cost nothing.
    DEFAULT_WINDOWS = (("17:59", "18:01"), ("13:29:30", "13:30:30"), ("11:00", "17:59"))
    CACHE_WINDOWS = 2   # cache file names use only the first N windows, so adding
                        # fallbacks later doesn't orphan an existing cache

    def __init__(self, spec: ContractSpec, cache_dir: str | Path = "data/raw",
                 windows=None, workers: int = 30, **kw):
        """windows: tried in order each day; a later window is requested only if
        the earlier ones returned no settlement records."""
        super().__init__(spec, cache_dir=cache_dir, **kw)
        windows = windows or spec.snapshot_windows
        if not windows:
            raise ValueError(f"no snapshot windows known for {spec.root}: run "
                             f"scripts/probe_snapshot.py --root {spec.root}, or pass --window")
        if isinstance(windows[0], str):          # a single (start, end) pair
            windows = (tuple(windows),)
        self.windows = tuple(tuple(w) for w in windows)
        self.window = self.windows[0]
        self.workers = workers
        self.fallback_days: list[pd.Timestamp] = []
        self.empty_days: list[pd.Timestamp] = []      # weekdays with no settle in any window
        self.degraded_days: set[str] = set()

    # -- days & windows --------------------------------------------------------
    def request_days(self, start, end) -> pd.DatetimeIndex:
        s, e = self.clamp("statistics", start, end)
        days = pd.bdate_range(s, e - pd.Timedelta(days=1))      # weekdays, end exclusive
        # drop the last day if its window hasn't fully passed the available end
        _, avail_hi = self.available_range("statistics")
        return pd.DatetimeIndex([d for d in days
                                 if max(self.utc_window(d, w)[1] for w in self.windows)
                                 <= avail_hi.tz_localize("UTC")])

    def utc_window(self, day, window=None) -> tuple[pd.Timestamp, pd.Timestamp]:
        """[start, end) of a window on `day`, converted to UTC (DST-aware).
        Times are exchange-local 'HH:MM' or 'HH:MM:SS'."""
        day = pd.Timestamp(day).normalize()
        out = []
        for t in (window or self.window):
            t = t if t.count(":") == 2 else t + ":00"
            out.append((day + pd.Timedelta(t)).tz_localize(self.spec.timezone).tz_convert("UTC"))
        return out[0], out[1]

    def day_symbols(self, day) -> list[str]:
        """Contracts plausibly listed on `day` (delivery up to ~30 months ahead)."""
        day = pd.Timestamp(day)
        return self.symbols(day, day, months_ahead=30)

    def _day_request(self, day, window=None) -> dict:
        s, e = self.utc_window(day, window)
        return dict(dataset=self.spec.databento_dataset, schema="statistics", stype_in="raw_symbol",
                    symbols=self.day_symbols(day), start=s.isoformat(), end=e.isoformat())

    # -- cache -------------------------------------------------------------------
    def _month_paths(self, month: pd.Period):
        w = "_".join(f"{a.replace(':', '')}-{b.replace(':', '')}" for a, b in self.windows[:self.CACHE_WINDOWS])
        tag = f"{self.spec.root}_statsnap_{w}_{month.strftime('%Y%m')}"
        return self.cache_dir / f"{tag}.parquet", self.cache_dir / f"{tag}.days.txt"

    def _cached_days(self, month) -> set:
        _, dp = self._month_paths(month)
        return set(dp.read_text().split()) if dp.exists() else set()

    def missing_days(self, start, end) -> list[pd.Timestamp]:
        days = self.request_days(start, end)
        out = []
        for m, ds in pd.Series(days, index=days).groupby(days.to_period("M")):
            have = self._cached_days(m)
            out += [d for d in ds if d.strftime("%Y-%m-%d") not in have]
        return out

    # -- cost ----------------------------------------------------------------------
    def estimate_costs(self, start, end, sample: int = 12, window_months=None) -> dict:
        """Cost of the days not yet cached, extrapolated from `sample` days
        spread over the range (get_cost is free), plus definitions."""
        todo = self.missing_days(start, end)
        out = {"statistics (snapshot)": 0.0}
        if todo:
            idx = sorted(set(np.linspace(0, len(todo) - 1, min(sample, len(todo))).round().astype(int)))
            per_day = []
            for i in idx:
                try:
                    per_day.append(self.client.metadata.get_cost(**self._day_request(todo[i])))
                except Exception:      # e.g. no symbol resolvable that day
                    per_day.append(0.0)
            out["statistics (snapshot)"] = float(np.mean(per_day)) * len(todo)
        out["definition"] = self._cost("definition", start, end, self.symbols(start, end))
        out["_days_to_fetch"] = len(todo)
        return out

    # -- download --------------------------------------------------------------
    def fetch(self, start, end, progress_every: int = 50) -> None:
        """Download missing days, `self.workers` requests in parallel.

        Each request takes several seconds on Databento's side, so running them
        concurrently is what makes a multi-year pull practical. Results are
        written month by month as soon as every day of that month is done, so an
        interrupted run resumes where it stopped.
        """
        import time
        import warnings
        from concurrent.futures import ThreadPoolExecutor, as_completed

        todo = self.missing_days(start, end)
        if not todo:
            self._summary(start, end)
            return
        est = self.estimate_costs(start, end)["statistics (snapshot)"]
        log.info("snapshot fetch: %d days, est. $%.2f, %d parallel requests", len(todo), est, self.workers)
        if est > self.max_cost_usd:
            raise RuntimeError(f"snapshot fetch would cost ~${est:.2f} > max_cost_usd "
                               f"(${self.max_cost_usd:.2f}); raise --max-cost to proceed")
        # Expected and harmless: holidays / missing snapshot ("No data found"), and
        # Databento's per-day quality notes (summarised at the end instead).
        warnings.filterwarnings("ignore", message=".*No data found.*")
        warnings.filterwarnings("ignore", message=".*reduced quality.*")
        # Each day asks for every contract that could be listed ~30 months out; ones not
        # listed yet (or already expired) don't resolve. Missing data shows up in the check.
        warnings.filterwarnings("ignore", message=".*did not resolve.*")

        months = pd.DatetimeIndex(todo).to_period("M")
        pending = {m: int((months == m).sum()) for m in months.unique()}
        results: dict = {m: [] for m in pending}
        t0, n = time.time(), 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(self._fetch_day, d, self.windows): d for d in todo}
            for fut in as_completed(futs):
                d = futs[fut]
                m = d.to_period("M")
                results[m].append((d, fut.result()))
                pending[m] -= 1
                n += 1
                if pending[m] == 0:
                    self._write_month(m, results.pop(m))
                if n % progress_every == 0 or n == len(todo):
                    rate = (time.time() - t0) / n
                    log.info("  %d/%d days  (~%.0f min left)", n, len(todo), rate * (len(todo) - n) / 60)
        self._summary(start, end)

    def _write_month(self, month, day_frames) -> None:
        pq, dp = self._month_paths(month)
        frames = [pd.read_parquet(pq)] if pq.exists() else []
        frames += [df for _, df in day_frames if len(df)]
        done = self._cached_days(month) | {d.strftime("%Y-%m-%d") for d, _ in day_frames}
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(pq)
        dp.write_text("\n".join(sorted(done)))     # written after the data: crash-safe

    def _summary(self, start=None, end=None):
        if self.fallback_days:
            fb = sorted(self.fallback_days)
            log.info("fallback window used on %d days: %s%s", len(fb),
                     ", ".join(f"{d:%Y-%m-%d}" for d in fb[:20]), " ..." if len(fb) > 20 else "")
        cal_days = sorted(d for d in self.empty_days if self.spec.tcal.is_business_day(d))
        if cal_days:
            log.warning("no settle in any window on %d calendar trading days: %s", len(cal_days),
                        ", ".join(f"{d:%Y-%m-%d}" for d in cal_days[:20]))
        if start is not None:
            try:
                s, e = self.clamp("statistics", start, end)
                cond = self.client.metadata.get_dataset_condition(
                    dataset=self.spec.databento_dataset, start_date=s.strftime("%Y-%m-%d"),
                    end_date=(e - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
                bad = [c["date"] for c in cond if c.get("condition") not in ("available", None)]
                self.degraded_days = set(bad)
                if bad:
                    log.info("Databento flags reduced data quality on %d days: %s%s", len(bad),
                             ", ".join(bad[:15]), " ..." if len(bad) > 15 else "")
            except Exception as err:      # informational only
                log.info("dataset condition check skipped: %s", err)

    def _fetch_day(self, day, windows) -> pd.DataFrame:
        """Try windows in order; return the first result containing settles."""
        df = pd.DataFrame()
        for w in windows:
            df = self._fetch_window(day, w)
            if len(df) and (df["stat_type"] == STAT_SETTLEMENT).any():
                if w != self.windows[0]:
                    self.fallback_days.append(day)
                return df
        self.empty_days.append(day)
        return df

    def _thread_client(self):
        """One Databento client per worker thread (an injected test client is shared)."""
        if self._client is not None:
            return self._client
        import threading
        import databento as db
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        if not hasattr(self._tls, "client"):
            self._tls.client = db.Historical(self.api_key)
        return self._tls.client

    def _fetch_window(self, day, window, retries: int = 5) -> pd.DataFrame:
        import time
        for attempt in range(retries):
            try:
                return self._thread_client().timeseries.get_range(
                    **self._day_request(day, window)).to_df().reset_index()
            except Exception as err:
                msg = str(err).lower()
                if any(k in msg for k in ("symbology", "resolve", "no data")):
                    return pd.DataFrame()          # nothing listed / no data that day
                transient = any(k in msg for k in ("429", "rate", "timeout", "timed out", "connection",
                                                   "502", "503", "504", "reset"))
                if not transient or attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)           # back off: 1, 2, 4, 8 s
        return pd.DataFrame()

    def repair(self, start, end) -> None:
        """Retry fallback windows (beyond the cached ones) for already-cached trading
        days that have no settle, e.g. early-close days fetched before the wider
        window existed. Each day is retried once (logged in <month>.repair.txt)."""
        extra = self.windows[self.CACHE_WINDOWS:]
        if not extra:
            return
        days = self.request_days(start, end)
        for m, ds in pd.Series(days, index=days).groupby(days.to_period("M")):
            pq, dp = self._month_paths(m)
            if not dp.exists():
                continue
            rp = dp.with_name(dp.name.replace(".days.txt", ".repair.txt"))
            tried = set(rp.read_text().split()) if rp.exists() else set()
            cached = self._cached_days(m)
            have = pd.read_parquet(pq) if pq.exists() else pd.DataFrame()
            with_settle = set()
            if len(have):
                st = have[have["stat_type"] == STAT_SETTLEMENT]
                with_settle = set(pd.to_datetime(st["ts_recv"], utc=True).dt.tz_convert(self.spec.timezone)
                                  .dt.strftime("%Y-%m-%d"))
            todo = [d for d in ds if d.strftime("%Y-%m-%d") in cached
                    and d.strftime("%Y-%m-%d") not in with_settle
                    and d.strftime("%Y-%m-%d") not in tried
                    and self.spec.tcal.is_business_day(d)]
            if not todo:
                continue
            new = []
            for d in todo:
                df = self._fetch_day(d, extra)
                if len(df) and (df["stat_type"] == STAT_SETTLEMENT).any():
                    new.append(df)
                tried.add(d.strftime("%Y-%m-%d"))
            if new:
                pd.concat([have] + new, ignore_index=True).to_parquet(pq)
            rp.write_text("\n".join(sorted(tried)))
            log.info("repair %s: retried %d day(s), recovered %d", m, len(todo), len(new))

    def cached_statistics(self, start, end) -> pd.DataFrame:
        months = pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="M")
        frames = [pd.read_parquet(self._month_paths(m)[0]) for m in months if self._month_paths(m)[0].exists()]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        t = pd.to_datetime(df["ts_recv"], utc=True)
        return df[(t >= pd.Timestamp(start, tz="UTC")) & (t < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1))]

    def load(self, start, end, with_definitions: bool = True, offline: bool = False) -> pd.DataFrame:
        """offline=True: use only what is cached in data/raw (no Databento calls)."""
        if not offline:
            self.fetch(start, end)
            self.repair(start, end)
        stats = self.cached_statistics(start, end)
        if stats.empty:
            raise RuntimeError("no snapshot records found - check the window with scripts/probe_snapshot.py")
        panel = normalize_databento_statistics(stats, self.spec)
        if with_definitions:
            try:
                if offline:
                    import databento as db
                    files = sorted(self.cache_dir.glob(f"{self.spec.root}_definition_*.dbn.zst"))
                    if not files:
                        raise FileNotFoundError("no cached definition file")
                    defs = db.DBNStore.from_file(str(files[-1])).to_df()
                else:
                    defs = self._get("definition", start, end, self.symbols(start, end)).to_df()
                mism = validate_expirations(defs, self.spec)
                if len(mism):
                    log.warning("Calendar vs Databento expiration mismatches:\n%s", mism)
                else:
                    log.info("calendar agrees with exchange last trading days for all %d contracts",
                             defs["raw_symbol"].map(lambda x: raw_symbol_to_contract(str(x), self.spec)).nunique())
            except Exception as e:
                log.warning("definition check skipped: %s", e)
        return panel


def validate_expirations(definitions: pd.DataFrame, spec: ContractSpec) -> pd.DataFrame:
    """Compare computed last trading days with Databento definition `expiration`.
    Returns the mismatching rows (empty = calendar agrees with the exchange)."""
    d = definitions.reset_index()
    d["contract"] = d["raw_symbol"].map(lambda s: raw_symbol_to_contract(str(s), spec))
    d = d[d["contract"].notna()].drop_duplicates("contract", keep="last")
    # Databento stamps ICE expirations at 00:00 UTC of the last trading day; take the
    # UTC date (converting to New York first would shift it a day back).
    d["exp_local"] = _to_utc(d["expiration"]).dt.tz_localize(None).dt.normalize()
    d["ltd_calc"] = d["contract"].map(lambda c: spec.last_trading_day(*spec.parse(c)))
    return d.loc[d["exp_local"] != d["ltd_calc"], ["contract", "exp_local", "ltd_calc"]]


# ---------------------------------------------------------------------------
# Synthetic data (for tests and demos only)
# ---------------------------------------------------------------------------
def make_synthetic_panel(spec: ContractSpec, start="2019-01-01", end="2026-09-30",
                         seed: int = 4, s0: float = 120.0, vol: float = 0.32,
                         listed_months: int = 24) -> pd.DataFrame:
    """Simulated futures curve with regime-switching carry (contango <-> backwardation)
    and an OI life-cycle that produces realistic OI crossovers ~3-5 weeks before FND.
    NOT market data."""
    rng = np.random.default_rng(seed)
    dates = spec.tcal.business_days(start, end)
    n = len(dates)
    dt = 1 / 252
    logS = np.log(s0) + np.cumsum(rng.normal(0.08 * dt, vol * np.sqrt(dt), n))
    t_years = np.arange(n) * dt
    # annualised log carry (slope of ln F vs T): + contango, - backwardation
    ar = np.zeros(n)
    for i in range(1, n):
        ar[i] = 0.995 * ar[i - 1] + rng.normal(0, 0.006)
    carry = 0.03 + 0.12 * np.sin(2 * np.pi * t_years / 3.3 + 0.4) + ar

    contracts = spec.contract_table(spec.contracts_between(start, pd.Timestamp(end) + pd.DateOffset(months=listed_months)))
    rows = []
    for cid, c in contracts.iterrows():
        list_date = c["delivery_start"] - pd.DateOffset(months=listed_months)
        mask = (dates >= list_date) & (dates <= c["last_trading_day"])
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d = dates[idx]
        T = (c["last_trading_day"] - d).days.values / 365.25
        basis_noise = np.zeros(len(idx))   # mean-reverting contract-specific basis
        eps = rng.normal(0, 0.0015, len(idx))
        for i in range(1, len(idx)):
            basis_noise[i] = 0.97 * basis_noise[i - 1] + eps[i]
        F = np.exp(logS[idx] + carry[idx] * T + basis_noise)
        x = spec.tcal.bdays_between_vec(d, np.full(len(d), c["first_notice_day"].to_datetime64())).astype(float)
        peak = 120_000 * rng.uniform(0.7, 1.3)
        oi = np.where(x >= 30, peak * np.exp(-(x - 30) / 60.0),
                      peak * np.clip(x / 30.0, 0.03, None) ** 1.5)
        oi = np.maximum(oi * np.exp(rng.normal(0, 0.03, len(idx))), 50).round()
        volume = (oi * 0.12 * np.exp(rng.normal(0, 0.3, len(idx)))).round()
        rows.append(pd.DataFrame(dict(trade_date=d, contract=cid, settle=np.round(F, 2),
                                      volume=volume, open_interest=oi)))
    panel = pd.concat(rows, ignore_index=True)
    return validate_panel(apply_default_availability(panel, spec), spec)
