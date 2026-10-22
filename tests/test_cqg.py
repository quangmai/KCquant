"""Tests for CQG Desktop quote resolution and fallback."""
from unittest.mock import MagicMock, patch
import pytest

from kcquant.cqg import (
    to_cqg_symbol,
    to_ice_symbol,
    get_cqg_credentials,
    fetch_cqg_quotes,
    _parse_numbers_from_text,
)
from kcquant.quotes import fetch_live_contract_quote, fetch_live_curve_quotes


def test_symbol_conversion():
    assert to_cqg_symbol("KCZ26") == "KCEZ26"
    assert to_cqg_symbol("KCH27") == "KCEH27"
    assert to_cqg_symbol("KCK27") == "KCEK27"
    assert to_cqg_symbol("KCZ26.NYB") == "KCEZ26"

    assert to_ice_symbol("KCEZ26") == "KCZ26"
    assert to_ice_symbol("KCEH27") == "KCH27"
    assert to_ice_symbol("KCEK27") == "KCK27"


def test_get_cqg_credentials_empty(monkeypatch):
    import kcquant.cqg as cqg_mod
    monkeypatch.setattr(cqg_mod, "_ENV_LOADED", True)
    monkeypatch.delenv("CQG_USERNAME", raising=False)
    monkeypatch.delenv("CQG_USER", raising=False)
    monkeypatch.delenv("CQG_PASSWORD", raising=False)
    monkeypatch.delenv("CQG_PASS", raising=False)

    assert get_cqg_credentials() is None


def test_get_cqg_credentials_configured(monkeypatch):
    import kcquant.cqg as cqg_mod
    monkeypatch.setattr(cqg_mod, "_ENV_LOADED", True)
    monkeypatch.setenv("CQG_USERNAME", "trader_joe")
    monkeypatch.setenv("CQG_PASSWORD", "secret123")
    monkeypatch.setenv("CQG_BROKER", "ironbeam")
    monkeypatch.delenv("CQG_URL", raising=False)

    creds = get_cqg_credentials()
    assert creds is not None
    assert creds["username"] == "trader_joe"
    assert creds["password"] == "secret123"
    assert creds["broker"] == "ironbeam"
    assert "ironbeam" in creds["url"]


def test_parse_numbers_from_text():
    sample = "KCEZ26 304.55 +1.20 305.00 302.10 12450"
    nums = _parse_numbers_from_text(sample)
    assert 304.55 in nums
    assert 1.20 in nums
    assert 305.00 in nums


def test_fetch_cqg_quotes_missing_credentials(monkeypatch):
    import kcquant.cqg as cqg_mod
    monkeypatch.setattr(cqg_mod, "_ENV_LOADED", True)
    monkeypatch.delenv("CQG_USERNAME", raising=False)
    monkeypatch.delenv("CQG_USER", raising=False)
    monkeypatch.delenv("CQG_PASSWORD", raising=False)
    monkeypatch.delenv("CQG_PASS", raising=False)

    assert fetch_cqg_quotes(["KCZ26", "KCH27"]) is None


def test_quotes_fallback_when_cqg_unconfigured(monkeypatch):
    import kcquant.cqg as cqg_mod
    monkeypatch.setattr(cqg_mod, "_ENV_LOADED", True)
    monkeypatch.delenv("CQG_USERNAME", raising=False)
    monkeypatch.delenv("CQG_USER", raising=False)
    monkeypatch.delenv("CQG_PASSWORD", raising=False)
    monkeypatch.delenv("CQG_PASS", raising=False)

    # Calling fetch_live_contract_quote should not fail even without CQG
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "chart": {
                "result": [{
                    "meta": {
                        "regularMarketPrice": 304.50,
                        "chartPreviousClose": 303.00,
                        "regularMarketDayHigh": 306.00,
                        "regularMarketDayLow": 302.00,
                        "regularMarketVolume": 1000,
                        "regularMarketTime": 1729600000
                    }
                }]
            }
        }
        mock_get.return_value = mock_resp

        q = fetch_live_contract_quote("KCZ26")
        assert q is not None
        assert q["price"] == 304.50
        assert "Yahoo" in q["source"]


def test_quotes_prioritizes_cqg_when_available():
    mock_cqg_data = {
        "KCZ26": {
            "contract": "KCZ26",
            "cqg_symbol": "KCEZ26",
            "price": 305.25,
            "prev_close": 303.00,
            "high": 306.00,
            "low": 304.00,
            "volume": 2500,
            "datetime_ny": "2026-10-22 09:30 ET",
            "time_str": "09:30 ET",
            "source": "Live Market (CQG Desktop · 09:30 ET)",
        },
        "KCH27": {
            "contract": "KCH27",
            "cqg_symbol": "KCEH27",
            "price": 292.00,
            "prev_close": 290.00,
            "high": 293.00,
            "low": 291.00,
            "volume": 1800,
            "datetime_ny": "2026-10-22 09:30 ET",
            "time_str": "09:30 ET",
            "source": "Live Market (CQG Desktop · 09:30 ET)",
        },
        "KCK27": {
            "contract": "KCK27",
            "cqg_symbol": "KCEK27",
            "price": 287.50,
            "prev_close": 286.00,
            "high": 288.50,
            "low": 286.50,
            "volume": 900,
            "datetime_ny": "2026-10-22 09:30 ET",
            "time_str": "09:30 ET",
            "source": "Live Market (CQG Desktop · 09:30 ET)",
        }
    }

    with patch("kcquant.cqg.fetch_cqg_quotes", return_value=mock_cqg_data):
        curve = fetch_live_curve_quotes()
        assert curve is not None
        assert curve["kcz26"]["price"] == 305.25
        assert curve["kch27"]["price"] == 292.00
        assert curve["kck27"]["price"] == 287.50
        assert curve["spread_zh"] == 13.25
        assert curve["spread_hk"] == 4.50
        assert "CQG Desktop" in curve["source"]
