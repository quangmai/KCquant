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


def get_cqg_credentials() -> dict[str, str] | None:
    """Check for CQG credentials in environment.

    Returns dict of credentials if configured, otherwise None.
    """
    load_env()
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


def fetch_cqg_quotes(contracts: list[str] | None = None) -> dict[str, dict] | None:
    """Log in to CQG Desktop and extract price quotes for requested contracts.

    Parameters
    ----------
    contracts : list of ICE contract codes, e.g. ["KCZ26", "KCH27", "KCK27"]
                Defaults to ["KCZ26", "KCH27", "KCK27"].

    Returns
    -------
    dict mapping ICE symbol -> quote dict, or None if credentials missing / unavailable.
    """
    creds = get_cqg_credentials()
    if not creds:
        return None

    target_contracts = contracts or ["KCZ26", "KCH27", "KCK27"]
    cqg_targets = {c: to_cqg_symbol(c) for c in target_contracts}

    driver = _create_headless_driver()
    if driver is None:
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        timeout = creds["timeout"]
        driver.set_page_load_timeout(timeout + 10)
        driver.get(creds["url"])

        # Wait for page elements to render
        wait = WebDriverWait(driver, timeout)

        # Check if on logon page
        time.sleep(3)
        current_url = driver.current_url.lower()

        if "logon" in current_url or driver.find_elements(By.NAME, "userName"):
            user_input = wait.until(EC.presence_of_element_located((By.NAME, "userName")))
            pass_input = driver.find_element(By.NAME, "password")

            user_input.clear()
            user_input.send_keys(creds["username"])
            pass_input.clear()
            pass_input.send_keys(creds["password"])

            # Submit form
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

            # Wait for logon redirect out of logon page
            time.sleep(5)

        # Inspect if login failed
        page_source = driver.page_source.lower()
        if "invalid username or password" in page_source or "authentication failed" in page_source:
            logger.error("CQG Desktop logon rejected: Invalid credentials or authentication failed.")
            return None

        # Look for quote board / tables / watchlist elements
        now_ny = datetime.now(NY_TZ)
        time_str = now_ny.strftime("%H:%M ET")
        datetime_str = now_ny.strftime("%Y-%m-%d %H:%M ET")

        results: dict[str, dict] = {}

        # Search for contract symbols in table rows or text containers
        for ice_sym, cqg_sym in cqg_targets.items():
            quote = _extract_quote_for_symbol(driver, cqg_sym, ice_sym, now_ny, time_str, datetime_str)
            if quote:
                results[ice_sym] = quote

        if len(results) > 0:
            return results

        return None
    except Exception as e:
        logger.warning(f"Error fetching quotes from CQG Desktop: {e}")
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


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
