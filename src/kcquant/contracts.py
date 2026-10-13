"""Contract specifications and exchange calendars.

Everything here is *static reference data*: it is knowable in advance, so using
it on any date does not introduce look-ahead (a contract's first notice day is
fixed by exchange rules long before the contract trades).

Calendar caveat: the ICE holiday list below is a rule-based approximation of
the ICE Futures US / ICE Futures Europe softs holiday schedules. Ad-hoc closures
(e.g. national days of mourning) are not included. Use `extra_holidays`, or the
`fnd_overrides` on a spec, if you need exact dates, and check computed last
trading days against Databento `definition.expiration` with
`kcquant.data.validate_expirations`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from dateutil.relativedelta import MO

import numpy as np
import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay, USMartinLutherKingJr,
    USMemorialDay, USPresidentsDay, USThanksgivingDay, nearest_workday,
    sunday_to_monday, EasterMonday, weekend_to_monday, next_monday_or_tuesday,
)
from pandas.tseries.offsets import CustomBusinessDay

MONTH_CODES = "FGHJKMNQUVXZ"
CODE_TO_MONTH = {c: i + 1 for i, c in enumerate(MONTH_CODES)}
MONTH_TO_CODE = {v: k for k, v in CODE_TO_MONTH.items()}


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
class ICEUSHolidayCalendar(AbstractHolidayCalendar):
    """Approximate ICE Futures US (New York softs) holiday calendar."""

    rules = [
        Holiday("NewYearsDay", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01",
                observance=nearest_workday),
        Holiday("IndependenceDay", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


class UKBankHolidayCalendar(AbstractHolidayCalendar):
    """England & Wales bank holidays (standard rules; one-off moves are listed below)."""

    rules = [
        Holiday("NewYearsDay", month=1, day=1, observance=weekend_to_monday),
        GoodFriday,
        EasterMonday,
        Holiday("EarlyMayBank", month=5, day=1, offset=pd.DateOffset(weekday=MO(1))),
        Holiday("SpringBank", month=5, day=31, offset=pd.DateOffset(weekday=MO(-1))),
        Holiday("SummerBank", month=8, day=31, offset=pd.DateOffset(weekday=MO(-1))),
        Holiday("Christmas", month=12, day=25, observance=weekend_to_monday),
        Holiday("BoxingDay", month=12, day=26, observance=next_monday_or_tuesday),
    ]


class ExchangeCalendar:
    """Business-day arithmetic on top of a holiday calendar."""

    def __init__(self, holiday_calendar: AbstractHolidayCalendar,
                 extra_holidays: list | None = None, removed_holidays: list | None = None,
                 start: str = "1990-01-01", end: str = "2040-12-31"):
        hol = holiday_calendar.holidays(start=start, end=end)
        if extra_holidays:      # closures the rules miss (state funerals, moved bank holidays, ...)
            hol = hol.union(pd.DatetimeIndex(pd.to_datetime(extra_holidays)))
        if removed_holidays:    # rule-based holidays on which the exchange actually traded
            hol = hol.difference(pd.DatetimeIndex(pd.to_datetime(removed_holidays)))
        self.holidays = hol
        self.bday = CustomBusinessDay(holidays=list(hol))
        self._start, self._end = pd.Timestamp(start), pd.Timestamp(end)

    def is_business_day(self, d) -> bool:
        d = pd.Timestamp(d).normalize()
        return d.weekday() < 5 and d not in self.holidays

    def add_bdays(self, d, n: int) -> pd.Timestamp:
        """Move n business days from d (d need not be a business day)."""
        d = pd.Timestamp(d).normalize()
        if n == 0:
            return self.rollforward(d)
        return d + n * self.bday

    def rollback(self, d) -> pd.Timestamp:
        return self.bday.rollback(pd.Timestamp(d).normalize())

    def rollforward(self, d) -> pd.Timestamp:
        return self.bday.rollforward(pd.Timestamp(d).normalize())

    def first_bday_of_month(self, year: int, month: int) -> pd.Timestamp:
        return self.rollforward(pd.Timestamp(year=year, month=month, day=1))

    def last_bday_of_month(self, year: int, month: int) -> pd.Timestamp:
        return self.rollback(pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0))

    def business_days(self, start, end) -> pd.DatetimeIndex:
        return pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq=self.bday)

    def bdays_between_vec(self, start, end) -> np.ndarray:
        """Vectorised bdays_between: business days in (start, end], signed."""
        hol = self.holidays.values.astype("datetime64[D]")
        s = (pd.DatetimeIndex(np.atleast_1d(pd.to_datetime(start))).values.astype("datetime64[D]") + 1)
        e = (pd.DatetimeIndex(np.atleast_1d(pd.to_datetime(end))).values.astype("datetime64[D]") + 1)
        return np.busday_count(s, e, holidays=hol)

    def bdays_between(self, start, end) -> int:
        """Number of business days in (start, end]. Negative if end < start."""
        start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        if end >= start:
            return len(self.business_days(start + pd.Timedelta(days=1), end))
        return -len(self.business_days(end + pd.Timedelta(days=1), start))


# One-off moves of England & Wales bank holidays (gov.uk). These define "business days"
# for ICE Europe contract dates (notice / last trading days).
UK_BANK_EXTRA = ["2020-05-08",                 # VE Day (early-May holiday moved)
                 "2022-06-02", "2022-06-03",   # Platinum Jubilee (spring holiday moved + extra)
                 "2022-09-19",                 # State funeral of Queen Elizabeth II
                 "2023-05-08"]                 # Coronation of King Charles III
UK_BANK_REMOVED = ["2020-05-04", "2022-05-30"]  # the regular dates of the moved holidays

# ICE Futures Europe softs TRADING days differ from UK bank holidays. Verified against
# Databento RC settlements 2019-01 .. 2026-10 (weekdays with / without a settle).
# Since mid-2020 ICE closes on UK bank holidays; before that it traded on several.
# Add future exceptions when ICE announces them (ICE publishes holiday circulars).
ICE_EU_EXTRA_CLOSED = ["2021-12-24"]          # closed Fri 24 Dec 2021, traded Tue 28 instead
ICE_EU_TRADED_ON_UK_HOLIDAY = [
    "2019-04-22", "2019-05-27", "2019-08-26", "2019-12-26",  # pre-2020 bank holidays
    "2020-04-13", "2020-05-08",                              # Easter Monday, VE Day 2020
    "2021-12-28",                                            # Boxing Day substitute 2021
    "2022-06-03",                                            # Jubilee extra holiday
    "2023-01-02",                                            # New Year substitute 2023
]


@lru_cache(maxsize=None)
def get_calendar(name: str) -> ExchangeCalendar:
    if name == "ICE_US":
        return ExchangeCalendar(ICEUSHolidayCalendar())
    if name == "UK_BANK":
        return ExchangeCalendar(UKBankHolidayCalendar(), extra_holidays=UK_BANK_EXTRA,
                                removed_holidays=UK_BANK_REMOVED)
    if name == "ICE_EU":
        return ExchangeCalendar(UKBankHolidayCalendar(),
                                extra_holidays=UK_BANK_EXTRA + ICE_EU_EXTRA_CLOSED,
                                removed_holidays=UK_BANK_REMOVED + ICE_EU_TRADED_ON_UK_HOLIDAY)
    raise KeyError(name)


# ---------------------------------------------------------------------------
# Contract specs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ContractSpec:
    root: str                      # internal root, e.g. "KC"
    exchange_root: str             # Databento/ICE root (space padded to 4 for IFUS)
    months: str                    # listed month codes, e.g. "HKNUZ"
    multiplier: float              # units per contract (lbs, tonnes)
    unit: str
    price_unit: str
    price_to_usd_per_unit: float   # factor to convert a quote to USD per `unit`
    calendar: str                  # business days for contract dates (FND, LTD, ...)
    timezone: str
    # Notice/expiry rules: offsets in business days relative to the first/last
    # business day of the delivery month (negative = before).
    fnd_offset_from_first_bday: int
    lnd_offset_from_last_bday: int
    ltd_offset_from_lnd: int
    settle_time: str               # local time the daily settlement is published (approx.)
    oi_publish_time: str           # local time on the NEXT business day when OI is published (approx.)
    databento_dataset: str
    tick_size: float = 0.0         # minimum price increment, quote units (for cost estimates)
    decision_time: str = "13:00"   # local time of daily roll decisions (before the close, TAS)
    trading_calendar: str | None = None   # days the market trades; None = same as `calendar`
    # Databento daily statistics snapshot windows (local time), tried in order; found
    # with scripts/probe_snapshot.py. None = not yet probed for this root.
    snapshot_windows: tuple | None = None
    fnd_overrides: dict = field(default_factory=dict)   # {"KCH26": "2026-02-19"}
    ltd_overrides: dict = field(default_factory=dict)

    @property
    def cal(self) -> ExchangeCalendar:
        """Business days for contract dates (notice / last trading days)."""
        return get_calendar(self.calendar)

    @property
    def tcal(self) -> ExchangeCalendar:
        """Trading days (sessions): roll timing, next-session logic, data checks."""
        return get_calendar(self.trading_calendar or self.calendar)

    @property
    def point_value_usd(self) -> float:
        """USD value of a 1.0 move in the quoted price for one contract."""
        return self.multiplier * self.price_to_usd_per_unit

    # -- naming ------------------------------------------------------------
    def contract_id(self, year: int, month: int) -> str:
        return f"{self.root}{MONTH_TO_CODE[month]}{year % 100:02d}"

    def parse(self, contract_id: str) -> tuple[int, int]:
        """'KCH26' -> (2026, 3). Two-digit years map to 1990-2089."""
        m = re.fullmatch(rf"{self.root}([{MONTH_CODES}])(\d{{2}}|\d{{4}})", contract_id)
        if not m:
            raise ValueError(f"bad contract id {contract_id!r}")
        yy = int(m.group(2))
        year = yy if yy > 999 else (1900 + yy if yy >= 90 else 2000 + yy)
        return year, CODE_TO_MONTH[m.group(1)]

    def databento_raw_symbol(self, year: int, month: int) -> str:
        """IFUS.IMPACT raw symbol, e.g. 'KC  FMH0026!' (root padded to 4)."""
        return f"{self.exchange_root:<4}FM{MONTH_TO_CODE[month]}00{year % 100:02d}!"

    # -- key dates ---------------------------------------------------------
    def first_notice_day(self, year: int, month: int) -> pd.Timestamp:
        cid = self.contract_id(year, month)
        if cid in self.fnd_overrides:
            return pd.Timestamp(self.fnd_overrides[cid])
        return self.cal.add_bdays(self.cal.first_bday_of_month(year, month),
                                  self.fnd_offset_from_first_bday)

    def last_notice_day(self, year: int, month: int) -> pd.Timestamp:
        return self.cal.add_bdays(self.cal.last_bday_of_month(year, month),
                                  self.lnd_offset_from_last_bday)

    def last_trading_day(self, year: int, month: int) -> pd.Timestamp:
        cid = self.contract_id(year, month)
        if cid in self.ltd_overrides:
            return pd.Timestamp(self.ltd_overrides[cid])
        return self.cal.add_bdays(self.last_notice_day(year, month), self.ltd_offset_from_lnd)

    def contracts_between(self, start, end) -> list[str]:
        """Listed contract ids whose delivery month lies in [start, end]."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        out = []
        for y in range(start.year, end.year + 1):
            for c in self.months:
                mo = CODE_TO_MONTH[c]
                if start <= pd.Timestamp(year=y, month=mo, day=1) + pd.offsets.MonthEnd(0) and \
                        pd.Timestamp(year=y, month=mo, day=1) <= end:
                    out.append(self.contract_id(y, mo))
        return out

    def contract_table(self, contract_ids) -> pd.DataFrame:
        """Static metadata for a set of contracts, sorted by delivery month."""
        rows = []
        for cid in sorted(set(contract_ids), key=lambda c: self.parse(c)):
            y, m = self.parse(cid)
            rows.append(dict(
                contract=cid, year=y, month=m, month_code=MONTH_TO_CODE[m],
                delivery_start=pd.Timestamp(year=y, month=m, day=1),
                first_notice_day=self.first_notice_day(y, m),
                last_notice_day=self.last_notice_day(y, m),
                last_trading_day=self.last_trading_day(y, m),
            ))
        df = pd.DataFrame(rows).set_index("contract")
        df["seq"] = np.arange(len(df))
        return df


KC = ContractSpec(
    root="KC", exchange_root="KC", months="HKNUZ",
    multiplier=37_500, unit="lb", price_unit="USc/lb", price_to_usd_per_unit=0.01,
    calendar="ICE_US", timezone="America/New_York",
    # ICE Coffee "C": FND = 7 bdays before 1st bday of delivery month;
    # LND = 7 bdays before last bday; LTD = 1 bday before LND.
    fnd_offset_from_first_bday=-7, lnd_offset_from_last_bday=-7, ltd_offset_from_lnd=-1,
    settle_time="14:00", oi_publish_time="10:00",
    databento_dataset="IFUS.IMPACT",
    tick_size=0.05,          # 0.05 c/lb = $18.75 per contract
    decision_time="13:00",   # KC closes 13:30 NY
    # probe 2019-2026: evening snapshot 18:00 (once 17:59:59, once missing); 13:30 snapshot
    # every day; wide window catches early-close days (24 Dec)
    snapshot_windows=(("17:59", "18:01"), ("13:29:30", "13:30:30"), ("11:00", "17:59")),
)

# Robusta (ICE Futures Europe). ICE spec: FND = 4th bday before 1st bday of delivery
# month; LTD = 4th bday before last bday (12:30), LND same day. Databento raw symbol
# format verified: 'RC  FMX0026!'.
RC = ContractSpec(
    root="RC", exchange_root="RC", months="FHKNUX",
    multiplier=10, unit="tonne", price_unit="USD/t", price_to_usd_per_unit=1.0,
    calendar="UK_BANK", trading_calendar="ICE_EU", timezone="Europe/London",
    fnd_offset_from_first_bday=-4, lnd_offset_from_last_bday=-4, ltd_offset_from_lnd=0,
    # probe 2019-2026: official settle ~17:34-17:48 London (after the close), OI ~11:05-11:10 next day
    settle_time="17:50", oi_publish_time="11:15",
    databento_dataset="IFEU.IMPACT",
    tick_size=1.0,           # $1/t = $10 per contract
    decision_time="17:00",   # RC trades 09:00-17:30 London; that day's settle comes after this
    # probe 2019-2026: 23:00:00 London snapshot on 15/16 days (missing 2019-07-17); the
    # 17:30-18:00 window holds the post-close settle snapshot every day; wide window for early closes
    snapshot_windows=(("22:59", "23:01"), ("17:29:30", "18:00"), ("11:00", "22:59")),
)

SPECS = {"KC": KC, "RC": RC}
