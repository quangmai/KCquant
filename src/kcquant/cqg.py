"""CQG Desktop quote provider for ICE Coffee 'C' futures.

Interacts with CQG Desktop (https://m.cqg.com/cqg/desktop/main) using headless
browser automation via Selenium to retrieve real-time quotes when credentials
are provided in the environment.

Environment Variables:
  CQG_USERNAME: CQG account username
  CQG_PASSWORD: CQG account password
  CQG_BROKER:   Optional broker/partner code (e.g. 'cqg', 'amp', 'ironbeam'). Default: 'cqg'
  CQG_URL:      Optional custom logon/desktop URL. Default: 'https://m.cqg.com/cqg/desktop/main'
  CQG_TIMEOUT:  Optional page load/wait timeout in seconds. Default: 15
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from kcquant.notify import load_env

logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")

# Standard ICE to CQG symbol mapping for Coffee 'C'
# ICE uses 'KC' (e.g. KCZ26), CQG uses 'KCE' (e.g. KCEZ26)
ICE_TO_CQG_SYMBOL = {
    "KCZ26": "KCEZ26",
    "KCH27": "KCEH27",
    "KCK27": "KCEK27",
    "KCN27": "KCEN27",
    "KCU27": "KCEU27",
    "KCZ27": "KCEZ27",
}

CQG_TO_ICE_SYMBOL = {v: k for k, v in ICE_TO_CQG_SYMBOL.items()}


def to_cqg_symbol(contract: str) -> str:
    """Convert ICE contract symbol (e.g. KCZ26) to CQG symbol (e.g. KCEZ26)."""
    clean = contract.replace(".NYB", "").strip().upper()
    if clean in ICE_TO_CQG_SYMBOL:
        return ICE_TO_CQG_SYMBOL[clean]
    # Fallback heuristic: KC + month + year -> KCE + month + year
    if clean.startswith("KC") and not clean.startswith("KCE"):
        return "KCE" + clean[2:]
    return clean


def to_ice_symbol(cqg_symbol: str) -> str:
    """Convert CQG contract symbol (e.g. KCEZ26) to standard ICE symbol (e.g. KCZ26)."""
    clean = cqg_symbol.strip().upper()
    if clean in CQG_TO_ICE_SYMBOL:
        return CQG_TO_ICE_SYMBOL[clean]
    if clean.startswith("KCE"):
        return "KC" + clean[3:]
    return clean


_ENV_LOADED = False
_SHARED_DRIVER = None


def get_cqg_credentials(reload_env: bool = False) -> dict[str, str] | None:
    """Check for CQG credentials in environment.

    Returns dict of credentials if configured, otherwise None.
    """
    global _ENV_LOADED
    if not _ENV_LOADED or reload_env:
        load_env()
        _ENV_LOADED = True

    username = os.environ.get("CQG_USERNAME") or os.environ.get("CQG_USER")
    password = os.environ.get("CQG_PASSWORD") or os.environ.get("CQG_PASS")

    if not username or not password:
        return None

    broker = os.environ.get("CQG_BROKER", "cqg").strip().lower()
    custom_url = (os.environ.get("CQG_URL") or "").strip()
    if custom_url and custom_url != "https://m.cqg.com/cqg/desktop/main":
        url = custom_url
    elif broker and broker != "cqg":
        url = f"https://m.cqg.com/{broker}/desktop/main"
    else:
        url = custom_url or "https://m.cqg.com/cqg/desktop/main"
    timeout_str = os.environ.get("CQG_TIMEOUT", "15").strip()
    try:
        timeout = float(timeout_str)
    except ValueError:
        timeout = 15.0

    return {
        "username": username.strip(),
        "password": password.strip(),
        "broker": broker,
        "url": url,
        "timeout": timeout,
    }


def _create_headless_driver():
    """Initialize Selenium headless Chrome driver with production options."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-extensions")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        driver = webdriver.Chrome(options=options)
        return driver
    except Exception as e:
        logger.warning(f"Could not create Selenium headless Chrome driver: {e}")
        return None


def _ensure_cqg_authenticated(driver, creds: dict) -> bool:
    """Ensure the CQG Desktop driver is logged in and the QSS quote widget is loaded."""
    from selenium.webdriver.common.by import By

    timeout = max(float(creds.get("timeout", 15.0)), 12.0)
    max_polls = int(timeout / 0.3)

    # 1. Wait until either QSS quotes are already visible OR the logon form appears
    for _ in range(max_polls):
        qss = driver.find_elements(By.TAG_NAME, "wpfe-quote-spread-sheet")
        if qss and any("KCE" in (el.text or "") for el in qss):
            return True
        if driver.find_elements(By.NAME, "userName"):
            break
        time.sleep(0.3)

    # 2. If logon form is present, submit credentials
    user_inputs = driver.find_elements(By.NAME, "userName")
    if user_inputs:
        user_input = user_inputs[0]
        pass_input = driver.find_element(By.NAME, "password")
        user_input.clear()
        user_input.send_keys(creds["username"])
        pass_input.clear()
        pass_input.send_keys(creds["password"])

        submit_btn = None
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            text = (btn.text or "").strip().lower()
            btype = btn.get_attribute("type")
            if "log on" in text or "login" in text or btype == "submit":
                submit_btn = btn
                break

        if submit_btn:
            submit_btn.click()
        else:
            pass_input.submit()

    # 3. Poll until redirected to desktop/main AND QSS widget renders live KCE prices
    for _ in range(max_polls + 10):
        time.sleep(0.3)
        if "desktop/main" in driver.current_url.lower():
            qss = driver.find_elements(By.TAG_NAME, "wpfe-quote-spread-sheet")
            if qss and any("KCE" in (el.text or "") and any(ch.isdigit() for ch in (el.text or "")) for el in qss):
                time.sleep(0.4)
                return True

    return False


def get_warm_cqg_driver(force_restart: bool = False):
    """Retrieve or initialize a persistent headless Chrome driver logged in to CQG Desktop.

    Reusing an active session allows subsequent quote queries to run in < 60ms.
    """
    global _SHARED_DRIVER
    creds = get_cqg_credentials()
    if not creds:
        return None

    if _SHARED_DRIVER is not None and not force_restart:
        try:
            from selenium.webdriver.common.by import By
            _ = _SHARED_DRIVER.current_url
            qss = _SHARED_DRIVER.find_elements(By.TAG_NAME, "wpfe-quote-spread-sheet")
            if qss and any("KCE" in (el.text or "") for el in qss):
                return _SHARED_DRIVER
            if _ensure_cqg_authenticated(_SHARED_DRIVER, creds):
                return _SHARED_DRIVER
        except Exception:
            pass
        try:
            _SHARED_DRIVER.quit()
        except Exception:
            pass
        _SHARED_DRIVER = None

    if _SHARED_DRIVER is not None and force_restart:
        try:
            _SHARED_DRIVER.quit()
        except Exception:
            pass
        _SHARED_DRIVER = None

    driver = _create_headless_driver()
    if driver is None:
        return None

    try:
        timeout = max(float(creds.get("timeout", 15.0)), 12.0)
        driver.set_page_load_timeout(timeout + 10)
        driver.get(creds["url"])

        if _ensure_cqg_authenticated(driver, creds):
            _SHARED_DRIVER = driver
            return _SHARED_DRIVER

        # Even if QSS took extra time, keep driver if on desktop/main
        if "desktop/main" in driver.current_url.lower():
            _SHARED_DRIVER = driver
            return _SHARED_DRIVER

        driver.quit()
        return None
    except Exception as e:
        logger.warning(f"Error creating warm CQG driver: {e}")
        try:
            driver.quit()
        except Exception:
            pass
        return None


def fetch_instant_cqg_quote(contract: str = "KCZ26") -> dict | None:
    """Instantly fetch real-time quote for a contract from CQG Desktop."""
    clean = contract.replace(".NYB", "").upper()
    res = fetch_cqg_quotes([clean], max_retries=1)
    if res and clean in res:
        return res[clean]
    return None


def fetch_cqg_quotes(contracts: list[str] | None = None, max_retries: int = 2) -> dict[str, dict] | None:
    """Extract price quotes for requested contracts from the warm CQG Desktop session.

    Reuses the persistent warm headless Chrome driver (`_SHARED_DRIVER`) so quote reads
    complete in < 200ms. If reading fails, retries up to `max_retries` times (1-2 retries)
    before returning None.
    """
    creds = get_cqg_credentials()
    if not creds:
        return None

    target_contracts = contracts or ["KCZ26", "KCH27", "KCK27"]
    cqg_targets = {c: to_cqg_symbol(c) for c in target_contracts}

    for attempt in range(max_retries + 1):
        force_restart = (attempt > 0)
        if attempt > 0:
            time.sleep(0.5)

        driver = get_warm_cqg_driver(force_restart=force_restart)
        if driver is None:
            continue

        try:
            now_ny = datetime.now(NY_TZ)
            time_str = now_ny.strftime("%H:%M ET")
            datetime_str = now_ny.strftime("%Y-%m-%d %H:%M ET")

            # 1. Primary extractor: Parse CQG Desktop QSS (Quote SpreadSheet) from warm DOM
            qss_results = _extract_qss_quotes(driver, now_ny, time_str, datetime_str)
            if qss_results:
                matched = {k: v for k, v in qss_results.items() if k in target_contracts}
                # Return immediately if all targets found OR active H27 + K27 curve found in QSS
                if len(matched) == len(target_contracts) or ("KCH27" in qss_results and "KCK27" in qss_results):
                    return matched if len(matched) > 0 else qss_results

            results: dict[str, dict] = dict(qss_results or {})
            for ice_sym, cqg_sym in cqg_targets.items():
                if ice_sym not in results:
                    quote = _extract_quote_for_symbol(driver, cqg_sym, ice_sym, now_ny, time_str, datetime_str)
                    if quote:
                        results[ice_sym] = quote

            matched = {k: v for k, v in results.items() if k in target_contracts}
            if len(matched) > 0:
                return matched
        except Exception as e:
            logger.warning(f"CQG quote attempt {attempt + 1}/{max_retries + 1} failed: {e}")

    return None


def _extract_qss_quotes(
    driver,
    now_ny: datetime,
    time_str: str,
    datetime_str: str
) -> dict[str, dict]:
    """Extract quotes from CQG Desktop's Quote SpreadSheet (QSS) widget."""
    from selenium.webdriver.common.by import By

    results: dict[str, dict] = {}
    qss_elems = driver.find_elements(By.TAG_NAME, "wpfe-quote-spread-sheet")
    if not qss_elems:
        qss_elems = driver.find_elements(
            By.XPATH,
            "//*[contains(@class, 'wpfe-quote-spread-sheet') or contains(@class, 'wpfe-quotes-pane')]"
        )

    for el in qss_elems:
        text = el.text
        # Matches KCEZ26 or F.US.KCEZ26
        sym_matches = re.findall(r"(?:F\.US\.)?(KCE[FGHJKMNQUVXZ]\d{1,2})\b", text)
        if not sym_matches:
            continue

        # Extract numeric lines after the last symbol
        last_sym_pos = max(text.find(s) for s in sym_matches)
        num_text = text[last_sym_pos:]
        num_lines = [
            line.strip()
            for line in num_text.splitlines()
            if re.match(r"^[-+]?\d+\.?\d*$", line.strip())
        ]

        if len(num_lines) >= len(sym_matches) * 4:
            for i, cqg_sym in enumerate(sym_matches):
                ice_sym = to_ice_symbol(cqg_sym)
                chunk = num_lines[i * 4 : (i + 1) * 4]
                try:
                    trade = float(chunk[0])
                    bid = float(chunk[1])
                    ask = float(chunk[2])
                    change = float(chunk[3])
                    prev_close = round(trade - change, 2)
                    high = max(trade, bid, ask)
                    low = min(trade, bid, ask)

                    results[ice_sym] = {
                        "contract": ice_sym,
                        "cqg_symbol": cqg_sym,
                        "price": trade,
                        "bid": bid,
                        "ask": ask,
                        "change": change,
                        "prev_close": prev_close,
                        "high": high,
                        "low": low,
                        "volume": 0,
                        "datetime_ny": datetime_str,
                        "time_str": time_str,
                        "source": f"Live Market (CQG Desktop · {time_str})",
                    }
                except (ValueError, IndexError):
                    continue

            if len(results) > 0:
                return results

    return results


def _extract_quote_for_symbol(
    driver,
    cqg_sym: str,
    ice_sym: str,
    now_ny: datetime,
    time_str: str,
    datetime_str: str
) -> dict | None:
    """Extract quote values for a given symbol from the current CQG Desktop DOM."""
    from selenium.webdriver.common.by import By

    # Alternative symbol formats (e.g. KCEZ26 or KCEZ6)
    sym_variants = [cqg_sym]
    if len(cqg_sym) == 6 and cqg_sym[-2].isdigit():
        sym_variants.append(cqg_sym[:-2] + cqg_sym[-1])  # KCEZ26 -> KCEZ6

    # 1. Search for table rows containing the symbol
    rows = driver.find_elements(By.TAG_NAME, "tr")
    for row in rows:
        text = row.text
        if any(v in text for v in sym_variants):
            nums = _parse_numbers_from_text(text)
            if nums:
                price = nums[0]
                prev_close = nums[1] if len(nums) > 1 else price
                high = max(nums[:3]) if len(nums) >= 3 else price
                low = min(nums[:3]) if len(nums) >= 3 else price
                return {
                    "contract": ice_sym,
                    "cqg_symbol": cqg_sym,
                    "price": float(price),
                    "prev_close": float(prev_close),
                    "high": float(high),
                    "low": float(low),
                    "volume": 0,
                    "datetime_ny": datetime_str,
                    "time_str": time_str,
                    "source": f"Live Market (CQG Desktop · {time_str})",
                }

    # 2. Search for div or span elements with the symbol text
    for v in sym_variants:
        xpath = f"//*[contains(text(), '{v}')]"
        elems = driver.find_elements(By.XPATH, xpath)
        for el in elems:
            parent = el
            for _ in range(3):
                try:
                    parent = parent.find_element(By.XPATH, "..")
                    nums = _parse_numbers_from_text(parent.text)
                    # Filter for numbers in typical Coffee price range (e.g. 100 - 450)
                    coffee_prices = [n for n in nums if 150.0 <= n <= 450.0]
                    if coffee_prices:
                        price = coffee_prices[0]
                        return {
                            "contract": ice_sym,
                            "cqg_symbol": cqg_sym,
                            "price": float(price),
                            "prev_close": float(coffee_prices[1]) if len(coffee_prices) > 1 else price,
                            "high": float(max(coffee_prices)),
                            "low": float(min(coffee_prices)),
                            "volume": 0,
                            "datetime_ny": datetime_str,
                            "time_str": time_str,
                            "source": f"Live Market (CQG Desktop · {time_str})",
                        }
                except Exception:
                    break

    return None


def _parse_numbers_from_text(text: str) -> list[float]:
    """Parse floats/decimals from text block."""
    matches = re.findall(r"[-+]?\d*\.\d+|\b\d+\b", text)
    res = []
    for m in matches:
        try:
            val = float(m)
            res.append(val)
        except ValueError:
            continue
    return res
