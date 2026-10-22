"""Unit tests for quotes module."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kcquant.quotes import (
    fetch_live_contract_quote,
    fetch_live_zh_quotes,
    get_latest_prices,
)


@pytest.fixture(autouse=True)
def disable_cqg_in_unit_tests(monkeypatch):
    """Ensure unit tests do not hit live browser automation."""
    monkeypatch.setattr("kcquant.cqg.fetch_cqg_quotes", lambda *args, **kwargs: None)


def test_fetch_live_contract_quote_mock_success():
    mock_payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 292.50,
                        "chartPreviousClose": 290.00,
                        "regularMarketDayHigh": 293.00,
                        "regularMarketDayLow": 289.50,
                        "regularMarketVolume": 4200,
                        "regularMarketTime": 1791888800,
                    }
                }
            ]
        }
    }
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload
        mock_get.return_value = mock_resp

        q = fetch_live_contract_quote("KCZ26")
        assert q is not None
        assert q["price"] == 292.50
        assert q["prev_close"] == 290.00
        assert q["high"] == 293.00
        assert q["volume"] == 4200


def test_fetch_live_contract_quote_error():
    with patch("requests.get", side_effect=Exception("Network error")):
        q = fetch_live_contract_quote("KCZ26")
        assert q is None


def test_fetch_live_zh_quotes_mock():
    with patch("kcquant.quotes.fetch_live_contract_quote") as mock_single:
        mock_single.side_effect = lambda sym: {
            "contract": sym,
            "ticker": f"{sym}.NYB",
            "price": 292.00 if sym == "KCZ26" else 285.00,
            "prev_close": 290.00,
            "high": 293.00,
            "low": 284.00,
            "volume": 1000,
            "timestamp": 1791888800,
            "datetime_ny": "2026-10-13 07:00 ET",
            "time_str": "07:00 ET",
        }
        res = fetch_live_zh_quotes()
        assert res is not None
        assert res["spread_cents"] == 7.00
        assert res["kcz26"]["price"] == 292.00
        assert res["kch27"]["price"] == 285.00


def test_get_latest_prices_cli_override(tmp_path):
    z, h, src, meta = get_latest_prices(
        raw_file=tmp_path / "dummy.csv",
        z_override=295.0,
        h_override=288.0,
    )
    assert z == 295.0
    assert h == 288.0
    assert "Override" in src
    assert meta["spread"] == 7.0


def test_get_latest_prices_live_fallback(tmp_path):
    # When live fails, fallback to raw curve
    raw_csv = tmp_path / "curve.csv"
    raw_csv.write_text(
        "trade_date,KCZ26,KCH27\n2026-10-12,289.90,283.00\n"
    )

    with patch("kcquant.quotes.fetch_live_zh_quotes", return_value=None), \
         patch("kcquant.quotes.sync_databento_settles", return_value=False):
        z, h, src, meta = get_latest_prices(raw_file=raw_csv, prefer_live=True)
        assert z == 289.90
        assert h == 283.00
        assert "2026-10-12" in src
