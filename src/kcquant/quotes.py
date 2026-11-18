"""Dynamic price and quote resolution for KC Coffee futures.

Supports:
  1. Live real-time intraday market quotes exclusively via CQG Desktop (with 1-2 retries).
  2. Official daily settlement history via Databento IFUS.IMPACT ohlcv-1d.
  3. Local cached settlement curve matrix (output/KC_databento_fixed_raw_curve.csv).
  4. Persistent status state (output/monitor/latest_prices.json).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

from kcquant.notify import load_env

ROOT = Path(__file__).resolve().parents[2]
NY_TZ = ZoneInfo("America/New_York")


def fetch_live_contract_quote(contract: str, timeout: float = 6.0, max_retries: int = 2) -> dict | None:
    """Fetch live intraday market quote for an ICE Coffee 'C' contract from CQG Desktop.

    Retries 1-2 times if CQG fails; returns None if CQG quote cannot be retrieved.
    """
    clean = contract.replace(".NYB", "").strip().upper()
    try:
        from kcquant.cqg import fetch_cqg_quotes
        cqg_res = fetch_cqg_quotes([clean], max_retries=max_retries)
        if cqg_res and clean in cqg_res:
            return cqg_res[clean]
    except Exception:
        pass
    return None


def fetch_live_zh_quotes(max_retries: int = 2) -> dict | None:
    """Fetch concurrent live CQG quotes for KCZ26 and KCH27."""
    try:
        from kcquant.cqg import fetch_cqg_quotes
        cqg_res = fetch_cqg_quotes(["KCZ26", "KCH27"], max_retries=max_retries)
        if cqg_res and "KCZ26" in cqg_res and "KCH27" in cqg_res:
            z_q = cqg_res["KCZ26"]
            h_q = cqg_res["KCH27"]
            spread = round(z_q["price"] - h_q["price"], 2)
            return {
                "kcz26": z_q,
                "kch27": h_q,
                "spread_cents": spread,
                "quote_time_ny": z_q["datetime_ny"],
                "source": f"Live Market (CQG Desktop · {z_q['time_str']})"
            }
    except Exception:
        pass

    z_q = fetch_live_contract_quote("KCZ26")
    h_q = fetch_live_contract_quote("KCH27")

    if z_q is None or h_q is None:
        return None

    spread = round(z_q["price"] - h_q["price"], 2)
    return {
        "kcz26": z_q,
        "kch27": h_q,
        "spread_cents": spread,
        "quote_time_ny": z_q["datetime_ny"],
        "source": z_q.get("source", f"Live Market (CQG Desktop · {z_q['time_str']})")
    }


def fetch_live_curve_quotes(max_retries: int = 2) -> dict | None:
    """Fetch concurrent live CQG quotes for KCH27, KCK27 (and KCZ26 / KCN27) from CQG Desktop."""
    try:
        from kcquant.cqg import fetch_cqg_quotes
        cqg_res = fetch_cqg_quotes(["KCZ26", "KCH27", "KCK27", "KCN27"], max_retries=max_retries)
        if cqg_res and "KCH27" in cqg_res and "KCK27" in cqg_res:
            h_q = cqg_res["KCH27"]
            k_q = cqg_res["KCK27"]

            if "KCZ26" in cqg_res:
                z_q = cqg_res["KCZ26"]
            else:
                # If CQG QSS watchlist has rolled from Z26 to [H27, K27, N27], preserve last reference Z26
                ref_z_px = round(h_q["price"] + 6.00, 2)
                cache_json = ROOT / "output" / "monitor" / "latest_kc_curve_prices.json"
                if cache_json.exists():
                    try:
                        with open(cache_json) as f:
                            cached = json.load(f)
                        if "kcz26" in cached and float(cached["kcz26"]) > 0:
                            ref_z_px = float(cached["kcz26"])
                    except Exception:
                        pass
                z_q = {
                    "contract": "KCZ26",
                    "cqg_symbol": "KCEZ26",
                    "price": ref_z_px,
                    "prev_close": ref_z_px,
                    "high": ref_z_px,
                    "low": ref_z_px,
                    "volume": 0,
                    "datetime_ny": h_q["datetime_ny"],
                    "time_str": h_q["time_str"],
                    "source": h_q["source"],
                }

            spread_zh = round(z_q["price"] - h_q["price"], 2)
            spread_hk = round(h_q["price"] - k_q["price"], 2)
            res = {
                "kcz26": z_q,
                "kch27": h_q,
                "kck27": k_q,
                "spread_zh": spread_zh,
                "spread_hk": spread_hk,
                "quote_time_ny": h_q["datetime_ny"],
                "source": f"Live Market (CQG Desktop · {h_q['time_str']})"
            }
            if "KCN27" in cqg_res:
                res["kcn27"] = cqg_res["KCN27"]
            return res
    except Exception:
        pass
    return None


def sync_databento_settles(raw_curve_file: Path | None = None) -> bool:
    """Check Databento for newly settled daily bars and append to raw curve CSV if available."""
    load_env()
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        return False

    raw_path = raw_curve_file or (ROOT / "output" / "KC_databento_fixed_raw_curve.csv")
    if not raw_path.exists():
        return False

    try:
        import databento as db
        client = db.Historical(api_key)

        # Check available end range
        meta = client.metadata.get_dataset_range("IFUS.IMPACT")
        end_avail = meta["schema"]["ohlcv-1d"]["end"]
        end_dt = pd.to_datetime(end_avail).tz_convert("UTC").date()

        df_raw = pd.read_csv(raw_path)
        last_dt_str = df_raw["trade_date"].dropna().iloc[-1]
        last_dt = pd.to_datetime(last_dt_str).date()

        if end_dt <= last_dt:
            # Already up to date
            return False

        # Request new days from last_dt
        start_str = last_dt.strftime("%Y-%m-%d")
        data = client.timeseries.get_range(
            dataset="IFUS.IMPACT",
            schema="ohlcv-1d",
            symbols=["KC  FMZ0026!", "KC  FMH0027!"],
            start=start_str,
            end=end_avail
        )
        df_new = data.to_df()
        if df_new.empty:
            return False

        # Parse settles and merge into wide format
        df_new = df_new.reset_index()
        df_new["date"] = pd.to_datetime(df_new["ts_event"]).dt.strftime("%Y-%m-%d")

        new_rows = []
        for d, grp in df_new.groupby("date"):
            if d <= last_dt_str:
                continue
            z_sub = grp[grp["symbol"] == "KC  FMZ0026!"]
            h_sub = grp[grp["symbol"] == "KC  FMH0027!"]
            if not z_sub.empty and not h_sub.empty:
                z_px = float(z_sub.iloc[-1]["close"])
                h_px = float(h_sub.iloc[-1]["close"])
                if z_px > 0 and h_px > 0:
                    row = {c: None for c in df_raw.columns}
                    row["trade_date"] = d
                    row["KCZ26"] = z_px
                    row["KCH27"] = h_px
                    new_rows.append(row)

        if new_rows:
            df_updated = pd.concat([df_raw, pd.DataFrame(new_rows)], ignore_index=True)
            df_updated.to_csv(raw_path, index=False)
            return True

    except Exception:
        pass

    return False


def get_latest_prices(
    raw_file: Path | None = None,
    z_override: float | None = None,
    h_override: float | None = None,
    prefer_live: bool = True
) -> tuple[float, float, str, dict]:
    """Retrieve the freshest available KCZ26 and KCH27 futures prices.

    Resolution order:
      1. Manual CLI overrides (--z-price, --h-price)
      2. Live intraday market streaming (Yahoo / ICE NY live quotes)
      3. Databento official daily settlement sync
      4. Local raw curve settlement cache
      5. Persisted cache in output/monitor/latest_prices.json
      6. Baseline default

    Returns:
      (z_price: float, h_price: float, source_label: str, metadata: dict)
    """
    monitor_dir = ROOT / "output" / "monitor"
    monitor_dir.mkdir(parents=True, exist_ok=True)
    cache_json = monitor_dir / "latest_prices.json"
    raw_path = raw_file or (ROOT / "output" / "KC_databento_fixed_raw_curve.csv")

    meta: dict = {}

    # 1. Manual Overrides
    if z_override is not None and h_override is not None:
        src = "CLI Manual Override"
        meta = {
            "source": src,
            "kcz26": float(z_override),
            "kch27": float(h_override),
            "spread": round(z_override - h_override, 2),
            "updated_at": datetime.now().isoformat()
        }
        with open(cache_json, "w") as f:
            json.dump(meta, f, indent=2)
        return float(z_override), float(h_override), src, meta

    # 2. Live Market Quotes (if prefer_live is True)
    if prefer_live:
        live = fetch_live_zh_quotes()
        if live is not None:
            z_px = live["kcz26"]["price"]
            h_px = live["kch27"]["price"]
            src = live["source"]
            meta = {
                "source": src,
                "kcz26": z_px,
                "kch27": h_px,
                "spread": live["spread_cents"],
                "live_meta": live,
                "updated_at": datetime.now().isoformat()
            }
            with open(cache_json, "w") as f:
                json.dump(meta, f, indent=2)
            return z_px, h_px, src, meta

    # 3. Databento settle sync
    try:
        sync_databento_settles(raw_path)
    except Exception:
        pass

    # 4. Check local CSV raw curve
    if raw_path.exists():
        df = pd.read_csv(raw_path)
        if "KCZ26" in df.columns and "KCH27" in df.columns:
            valid = df.dropna(subset=["KCZ26", "KCH27"])
            if len(valid) > 0:
                last_row = valid.iloc[-1]
                z_px = float(last_row["KCZ26"])
                h_px = float(last_row["KCH27"])
                dt_str = str(last_row["trade_date"])
                src = f"Settlement Settle ({dt_str})"
                meta = {
                    "source": src,
                    "kcz26": z_px,
                    "kch27": h_px,
                    "spread": round(z_px - h_px, 2),
                    "trade_date": dt_str,
                    "updated_at": datetime.now().isoformat()
                }
                with open(cache_json, "w") as f:
                    json.dump(meta, f, indent=2)
                return z_px, h_px, src, meta

    # 5. Check persisted cache json
    if cache_json.exists():
        try:
            with open(cache_json) as f:
                cached = json.load(f)
            if "kcz26" in cached and "kch27" in cached:
                return float(cached["kcz26"]), float(cached["kch27"]), f"Cached ({cached.get('source', 'Disk')})", cached
        except Exception:
            pass

    # 6. Fallback default
    z_fallback = 289.90
    h_fallback = 283.00
    return z_fallback, h_fallback, "Baseline Settle (2026-10-12)", {
        "kcz26": z_fallback,
        "kch27": h_fallback,
        "spread": round(z_fallback - h_fallback, 2)
    }


def get_latest_kc_curve_prices(
    raw_file: Path | None = None,
    z_override: float | None = None,
    h_override: float | None = None,
    k_override: float | None = None,
    prefer_live: bool = True
) -> dict:
    """Retrieve the freshest KCZ26, KCH27, and KCK27 futures prices and calendar spreads.

    Resolution order:
      1. Manual CLI overrides (--z-price, --h-price, --k-price)
      2. Live intraday market streaming (Yahoo / ICE NY live quotes)
      3. Databento official settlement sync
      4. Local raw curve settlement cache
      5. Baseline fallback

    Returns dict with keys:
      kcz26, kch27, kck27, spread_zh, spread_hk, price_source, quote_time_ny
    """
    monitor_dir = ROOT / "output" / "monitor"
    monitor_dir.mkdir(parents=True, exist_ok=True)
    cache_json = monitor_dir / "latest_kc_curve_prices.json"
    raw_path = raw_file or (ROOT / "output" / "KC_databento_fixed_raw_curve.csv")

    # 1. Manual Overrides
    if z_override is not None and h_override is not None and k_override is not None:
        src = "CLI Manual Override"
        res = {
            "kcz26": float(z_override),
            "kch27": float(h_override),
            "kck27": float(k_override),
            "spread_zh": round(z_override - h_override, 2),
            "spread_hk": round(h_override - k_override, 2),
            "price_source": src,
            "quote_time_ny": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M ET"),
            "cqg_ok": True,
        }
        with open(cache_json, "w") as f:
            json.dump(res, f, indent=2)
        return res

    # 2. Live CQG Market Quotes (exclusively from CQG Desktop with 2 retries)
    cqg_failed = False
    if prefer_live:
        live = fetch_live_curve_quotes(max_retries=2)
        if live is not None:
            z_px = live["kcz26"]["price"]
            h_px = live["kch27"]["price"]
            k_px = live["kck27"]["price"]
            res = {
                "kcz26": z_px,
                "kch27": h_px,
                "kck27": k_px,
                "spread_zh": live["spread_zh"],
                "spread_hk": live["spread_hk"],
                "price_source": live["source"],
                "quote_time_ny": live["quote_time_ny"],
                "live_meta": live,
                "cqg_ok": True,
            }
            with open(cache_json, "w") as f:
                json.dump(res, f, indent=2)
            return res
        cqg_failed = True
        if cache_json.exists():
            try:
                with open(cache_json) as f:
                    cached = json.load(f)
                if "kcz26" in cached and "kch27" in cached and "kck27" in cached:
                    cached["cqg_ok"] = False
                    cached["cqg_error"] = "CQG Desktop price fetch failed after 2 retries"
                    return cached
            except Exception:
                pass

    # 3. Check Databento settles (only when settle-only mode is requested)
    if not prefer_live:
        try:
            sync_databento_settles(raw_path)
        except Exception:
            pass

    # 4. Check local CSV raw curve
    if raw_path.exists():
        df = pd.read_csv(raw_path)
        cols = ["KCZ26", "KCH27", "KCK27"]
        if all(c in df.columns for c in cols):
            valid = df.dropna(subset=cols)
            if len(valid) > 0:
                last_row = valid.iloc[-1]
                z_px = float(last_row["KCZ26"])
                h_px = float(last_row["KCH27"])
                k_px = float(last_row["KCK27"])
                dt_str = str(last_row["trade_date"])
                res = {
                    "kcz26": z_px,
                    "kch27": h_px,
                    "kck27": k_px,
                    "spread_zh": round(z_px - h_px, 2),
                    "spread_hk": round(h_px - k_px, 2),
                    "price_source": f"Settlement Settle ({dt_str})",
                    "quote_time_ny": f"{dt_str} Close",
                    "trade_date": dt_str,
                    "cqg_ok": not cqg_failed,
                    "cqg_error": "CQG Desktop price fetch failed after 2 retries" if cqg_failed else None,
                }
                with open(cache_json, "w") as f:
                    json.dump(res, f, indent=2)
                return res

    # 5. Fallback defaults
    z_fallback = 305.00
    h_fallback = 292.25
    k_fallback = 287.35
    return {
        "kcz26": z_fallback,
        "kch27": h_fallback,
        "kck27": k_fallback,
        "spread_zh": round(z_fallback - h_fallback, 2),
        "spread_hk": round(h_fallback - k_fallback, 2),
        "price_source": "Baseline Default",
        "quote_time_ny": "N/A",
        "cqg_ok": not cqg_failed,
        "cqg_error": "CQG Desktop price fetch failed after 2 retries" if cqg_failed else None,
    }
