"""Tests for inventory-driven calendar spread relief signal."""
import numpy as np
import pandas as pd
import pytest

from kcquant.signals import inventory_spread_relief


def test_inventory_spread_relief_trigger():
    dates = pd.date_range("2026-08-01", periods=10, freq="B")
    avail = pd.date_range("2026-08-01 18:00", periods=10, freq="B", tz="UTC")

    # Features: spread starts low (contango), then inverts to +5.0c backwardation
    spreads = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.5, 4.0, 5.0, 6.0, 7.0]
    features = pd.DataFrame({
        "spread_12": spreads,
        "available_at": avail
    }, index=dates)

    # Cert stocks: low pending first, then sudden pending surge + stock recovery
    # pending ratio: 100/1000 = 0.10 (>= 0.05) on days 6..9
    cert = pd.DataFrame({
        "certified": [1000.0] * 5 + [1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        "pending": [20.0] * 5 + [100.0, 120.0, 150.0, 160.0, 180.0],
        "available_at": avail
    }, index=dates)

    sig = inventory_spread_relief(features, cert, min_backwardation=3.0, min_pending_ratio=0.05, stock_lookback=3)

    # Days 0..4: spread < 3.0 -> signal must be 0.0
    assert (sig.iloc[:5]["signal"] == 0.0).all()

    # Days 5..9: spread >= 3.0 and pending_ratio >= 0.05 -> signal must be -1.0 (bear spread)
    assert (sig.iloc[5:]["signal"] == -1.0).all()

    # Check available_at point-in-time preservation
    assert (sig["available_at"] == avail).all()
