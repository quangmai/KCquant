#!/usr/bin/env python3
"""Interactive test script for CQG Desktop connection and quote extraction.

Run with:
    python scripts/test_cqg_connection.py
"""
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from kcquant.notify import load_env
from kcquant.cqg import get_cqg_credentials, to_cqg_symbol, _create_headless_driver

NY_TZ = ZoneInfo("America/New_York")


def test_cqg():
    load_env()
    creds = get_cqg_credentials()

    print("=" * 60)
    print("CQG Desktop Integration Test")
    print("=" * 60)

    if not creds:
        print("[!] ERROR: No CQG credentials found in .env!")
        print("    Please ensure you have saved (.env):")
        print("      CQG_USERNAME=your_username")
        print("      CQG_PASSWORD=your_password")
        print("      CQG_BROKER=cqg  (or your broker, e.g. amp, ironbeam)")
        sys.exit(1)

    masked_user = creds["username"][:2] + "***" + creds["username"][-1:] if len(creds["username"]) > 3 else "***"
    print(f"[*] Username: {masked_user}")
    print(f"[*] Broker:   {creds['broker']}")
    print(f"[*] URL:      {creds['url']}")
    print(f"[*] Timeout:  {creds['timeout']}s")
    print("-" * 60)

    print("[*] Launching headless Chrome...")
    driver = _create_headless_driver()
    if not driver:
        print("[!] ERROR: Could not initialize Selenium Chrome driver.")
        sys.exit(1)

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        print(f"[*] Navigating to {creds['url']}...")
        driver.get(creds["url"])
        time.sleep(4)

        current_url = driver.current_url
        print(f"[*] Landed at URL: {current_url}")
        print(f"[*] Page Title:   {driver.title}")

        wait = WebDriverWait(driver, creds["timeout"])

        # Check if on logon page
        user_inputs = driver.find_elements(By.NAME, "userName")
        if user_inputs:
            print("[*] Logon form detected. Entering credentials...")
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
                print("[*] Clicking 'Log on' button...")
                submit_btn.click()
            else:
                print("[*] Submitting password form...")
                pass_input.submit()

            print("[*] Waiting for authentication response and redirect to desktop/main...")
            for _ in range(int(creds["timeout"] * 2)):
                time.sleep(0.5)
                if "desktop/main" in driver.current_url:
                    break

            print("[*] Giving streaming quote table 3 seconds to update...")
            time.sleep(3)

        new_url = driver.current_url
        print(f"[*] Post-login URL: {new_url}")
        print(f"[*] Post-login Title: {driver.title}")

        # Check for error banners
        page_text = driver.page_source.lower()
        if "invalid username or password" in page_text or "authentication failed" in page_text:
            print("[!] AUTHENTICATION FAILED: Server rejected username or password.")
            sys.exit(1)

        # Inspect page content for Coffee quotes
        print("[*] Inspecting page for Coffee 'C' quotes (KCEZ26, KCEH27, KCEK27)...")
        from kcquant.cqg import _extract_qss_quotes, _extract_quote_for_symbol

        now_ny = datetime.now(NY_TZ)
        time_str = now_ny.strftime("%H:%M ET")
        datetime_str = now_ny.strftime("%Y-%m-%d %H:%M ET")

        targets = ["KCZ26", "KCH27", "KCK27"]
        found_any = False

        # 1. Try QSS extractor first
        qss_quotes = _extract_qss_quotes(driver, now_ny, time_str, datetime_str)
        if qss_quotes:
            for t in targets:
                if t in qss_quotes:
                    found_any = True
                    q = qss_quotes[t]
                    cqg_sym = q["cqg_symbol"]
                    print(f"    [+] {t} ({cqg_sym}): Last={q['price']}¢ | Bid={q.get('bid')}¢ | Ask={q.get('ask')}¢ | NetΔ={q.get('change')}¢ | Source={q['source']}")

        if not found_any:
            for t in targets:
                cqg_sym = to_cqg_symbol(t)
                q = _extract_quote_for_symbol(driver, cqg_sym, t, now_ny, time_str, datetime_str)
                if q:
                    found_any = True
                    print(f"    [+] {t} ({cqg_sym}): Price = {q['price']}¢ | High = {q['high']}¢ | Low = {q['low']}¢ | Source = {q['source']}")
                else:
                    print(f"    [-] {t} ({cqg_sym}): Quote element not currently in DOM view.")

        if found_any:
            print("=" * 60)
            print("[*] SUCCESS! Live quotes extracted from CQG Desktop!")
            print("=" * 60)
        else:
            print("-" * 60)
            print("[i] Logged in successfully, but quotes are not displayed on the default dashboard view.")
            print("    Please check what widgets or symbols are on your default CQG Desktop workspace.")
            # Print visible text summary to assist debugging
            body_text = driver.find_element(By.TAG_NAME, "body").text[:800]
            print(f"[i] Visible page text preview:\n{body_text}")
    finally:
        driver.quit()


if __name__ == "__main__":
    test_cqg()
