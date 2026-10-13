"""DatabentoLoader date handling, with a stub client (no network)."""
import pandas as pd

from kcquant.contracts import KC
from kcquant.data import DatabentoLoader


class _Meta:
    def __init__(self):
        self.cost_calls = []

    def get_dataset_range(self, dataset):
        return {"start": "2018-12-23T00:00:00.000000000Z", "end": "2026-10-03T00:00:00.000000000Z",
                "schema": {"ohlcv-1d": {"start": "2018-12-23T00:00:00.000000000Z",
                                        "end": "2026-10-03T00:00:00.000000000Z"},
                           "statistics": {"start": "2018-12-23T00:00:00.000000000Z",
                                          "end": "2026-10-03T08:15:00.000000000Z"}}}

    def get_cost(self, **kw):
        self.cost_calls.append(kw)
        return 1.0


class _Client:
    def __init__(self):
        self.metadata = _Meta()


def test_requests_are_clamped_to_available_range(tmp_path):
    client = _Client()
    loader = DatabentoLoader(KC, cache_dir=tmp_path, client=client)
    costs = loader.estimate_costs("2018-01-01", "2026-10-04")
    assert set(costs) == set(DatabentoLoader.SCHEMAS)
    by_schema = {c["schema"]: c for c in client.metadata.cost_calls}
    assert by_schema["ohlcv-1d"]["start"] == "2018-12-23"
    assert by_schema["ohlcv-1d"]["end"] == "2026-10-03"
    assert by_schema["statistics"]["end"] == "2026-10-03"      # partial day rounded down
    assert by_schema["definition"]["end"] == "2026-10-03"      # falls back to dataset range


def test_in_range_request_unchanged(tmp_path):
    loader = DatabentoLoader(KC, cache_dir=tmp_path, client=_Client())
    assert loader.clamp("statistics", "2020-01-01", "2021-01-01") == (pd.Timestamp("2020-01-01"),
                                                                      pd.Timestamp("2021-01-01"))


def test_contract_windows_limit_history(tmp_path):
    loader = DatabentoLoader(KC, cache_dir=tmp_path, client=_Client())
    w = {c: (s, e) for c, _, s, e in loader.contract_windows("2019-01-01", "2026-10-03", 12)}
    assert w["KCH26"] == (pd.Timestamp("2025-03-01"), KC.last_trading_day(2026, 3) + pd.Timedelta(days=1))
    assert w["KCH19"][0] == pd.Timestamp("2019-01-01")           # clipped to requested start
    assert "KCH29" not in w                                      # window starts after requested end
    full = loader.contract_windows("2019-01-01", "2026-10-03", None)
    assert len(full) > len(w)


def test_windowed_cost_sums_per_contract(tmp_path):
    client = _Client()
    loader = DatabentoLoader(KC, cache_dir=tmp_path, client=client)
    c = loader.estimate_costs("2024-01-01", "2025-01-01", window_months=12)
    n = len(loader.contract_windows("2024-01-01", "2025-01-01", 12))
    assert c["statistics"] == n and c["definition"] == 1.0


class _PickyMeta(_Meta):
    """Raises for single-symbol requests on contracts with no data, like Databento does."""
    def get_cost(self, **kw):
        if len(kw["symbols"]) == 1 and kw["symbols"][0].endswith("28!"):
            raise RuntimeError("422 symbology_invalid_request\nNone of the symbols could be resolved")
        return super().get_cost(**kw)


def test_unpriceable_contracts_are_reported_not_fatal(tmp_path):
    client = _Client()
    client.metadata = _PickyMeta()
    loader = DatabentoLoader(KC, cache_dir=tmp_path, client=client)
    bd = loader.cost_breakdown("2019-01-01", "2026-10-03")
    bad = bd[bd["cost_usd"].isna()]
    assert len(bad) and bad["contract"].str.endswith("28").all()
    assert bad["note"].str.contains("422").all()
    c = loader.estimate_costs("2019-01-01", "2026-10-03", window_months=12)
    assert c["statistics"] > 0
