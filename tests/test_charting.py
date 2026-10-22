"""Tests for 5-minute intraday price tracker, chart generator, and photo dispatch."""
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from kcquant.charting import (
    snap_to_5min,
    record_cqg_intraday_snapshot,
    generate_intraday_5m_chart,
    get_prev_day_range,
)
from kcquant.notify import send_telegram_photo

NY_TZ = ZoneInfo("America/New_York")


def test_snap_to_5min():
    dt1 = datetime(2026, 10, 22, 10, 13, 45, tzinfo=NY_TZ)
    snapped1 = snap_to_5min(dt1)
    assert snapped1.minute == 10
    assert snapped1.second == 0

    dt2 = datetime(2026, 10, 22, 10, 17, 2, tzinfo=NY_TZ)
    snapped2 = snap_to_5min(dt2)
    assert snapped2.minute == 15
    assert snapped2.second == 0


def test_record_cqg_intraday_snapshot(tmp_path):
    csv_file = tmp_path / "test_intraday_5m.csv"
    data = {
        "kcz26_price": 304.50,
        "kch27_price": 291.10,
        "kck27_price": 286.75,
        "spread_zh_cents": 13.40,
        "spread_hk_cents": 4.35,
    }

    res_path = record_cqg_intraday_snapshot(data, csv_path=csv_file)
    assert res_path.exists()

    df = pd.read_csv(res_path)
    assert len(df) == 1
    assert df["kcz26"].iloc[0] == 304.50
    assert df["spread_zh"].iloc[0] == 13.40


def test_get_prev_day_range_fallback():
    with patch("requests.get", side_effect=Exception("Network error")):
        res = get_prev_day_range("KCZ26")
        assert res is not None
        assert res["contract"] == "KCZ26"
        assert res["high"] > 0
        assert res["low"] > 0
        assert res["high"] >= res["low"]


def test_get_prev_day_range_yahoo():
    mock_payload = {
        "chart": {
            "result": [{
                "timestamp": [1792468800, 1792555200],  # 2 days
                "indicators": {
                    "quote": [{
                        "high": [318.20, 317.90],
                        "low": [309.20, 308.35],
                        "close": [311.30, 316.15]
                    }]
                }
            }]
        }
    }
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload
        mock_get.return_value = mock_resp

        res = get_prev_day_range("KCZ26")
        assert res is not None
        assert res["high"] == 317.90
        assert res["low"] == 308.35


def test_generate_intraday_5m_chart(tmp_path):
    csv_file = tmp_path / "test_chart_data.csv"
    out_img = tmp_path / "test_chart.png"

    # Seed 3 5-minute points
    rows = [
        {"datetime_ny": "2026-10-22 09:30", "timestamp": 1729603800, "kcz26": 305.0, "kch27": 291.5, "kck27": 287.0, "spread_zh": 13.5, "spread_hk": 4.5},
        {"datetime_ny": "2026-10-22 09:35", "timestamp": 1729604100, "kcz26": 304.8, "kch27": 291.2, "kck27": 286.9, "spread_zh": 13.6, "spread_hk": 4.3},
        {"datetime_ny": "2026-10-22 09:40", "timestamp": 1729604400, "kcz26": 304.5, "kch27": 291.0, "kck27": 286.7, "spread_zh": 13.5, "spread_hk": 4.3},
    ]
    pd.DataFrame(rows).to_csv(csv_file, index=False)

    img_res = generate_intraday_5m_chart(
        csv_path=csv_file,
        output_image=out_img,
        prev_day_high=317.90,
        prev_day_low=308.35
    )
    assert img_res is not None
    assert img_res.exists()
    assert img_res.stat().st_size > 1000  # Non-trivial image file generated


def test_send_telegram_photo_success(tmp_path):
    fake_img = tmp_path / "chart.png"
    fake_img.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 999}}
        mock_post.return_value = mock_resp

        ok, msg = send_telegram_photo(
            fake_img,
            caption="☕ <b>KC Monitor</b>",
            token="fake_token",
            chat_id="fake_chat"
        )
        assert ok is True
        assert "successfully" in msg


def test_send_telegram_photo_missing_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    fake_img = tmp_path / "chart.png"
    fake_img.write_bytes(b"data")

    ok, msg = send_telegram_photo(fake_img, token="", chat_id="")
    assert ok is False
    assert "Missing" in msg
