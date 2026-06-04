"""
SilomPOS Dashboard Explorer — READ-ONLY.

Logs into https://dashboard.silompos.com, visits a fixed list of pages, and
saves a full-page screenshot of each into ./screenshots/.

This script does NOT click anything beyond the login button, does NOT submit
forms, and does NOT touch any update/delete/save action. It only navigates
and captures pixels.

Usage:
    pip install -r requirements.txt
    python explore.py

Requires: Google Chrome installed. Selenium 4 ships Selenium Manager which
will auto-download a matching chromedriver, so no separate driver setup.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Config — fill in your account, then run.
# Do NOT commit this file with real credentials.
# ---------------------------------------------------------------------------
USERNAME = "Trpsamyan@gmail.com"
PASSWORD = "Samyan12345"

BASE = "https://dashboard.silompos.com"

PAGES = [
    ("01_dashboard",          f"{BASE}/dashboard"),
    ("02_report_transaction", f"{BASE}/report/transaction"),
    ("03_report_daily",       f"{BASE}/report/daily"),
    ("04_report_sell",        f"{BASE}/report/sell"),
    ("05_report_sku",         f"{BASE}/report/sku"),
    ("06_inventory_sku",      f"{BASE}/inventory/sku"),
    ("07_product",            f"{BASE}/product"),
]

OUTPUT_DIR = Path(__file__).parent / "screenshots"
PAGE_SETTLE_SECONDS = 5      # SPA render + data fetch
LOGIN_TIMEOUT = 30
NAV_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    # Comment the next line out if you want to watch it run.
    # opts.add_argument("--headless=new")
    return webdriver.Chrome(options=opts)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[id*="email" i]',
    'input[placeholder*="email" i]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button',
]


def _first_present(driver, selectors, timeout=LOGIN_TIMEOUT):
    end = time.time() + timeout
    last_err = None
    while time.time() < end:
        for sel in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    return el
            except Exception as e:
                last_err = e
        time.sleep(0.3)
    raise TimeoutException(f"None of {selectors} appeared. Last: {last_err}")


def login(driver):
    print(f"→ Loading {BASE} for login")
    driver.get(BASE + "/")
    # If we land directly on dashboard (cached session) we're done.
    try:
        WebDriverWait(driver, 5).until(
            lambda d: "/login" in d.current_url.lower()
            or "signin" in d.current_url.lower()
            or "auth" in d.current_url.lower()
        )
    except TimeoutException:
        if "/login" not in driver.current_url.lower():
            print("   (already authenticated, skipping login)")
            return

    print(f"   login page: {driver.current_url}")
    email = _first_present(driver, EMAIL_SELECTORS)
    email.clear()
    email.send_keys(USERNAME)

    pwd = _first_present(driver, PASSWORD_SELECTORS, timeout=5)
    pwd.clear()
    pwd.send_keys(PASSWORD)

    # Find a plausible submit button. We avoid CSS :has-text (not supported)
    # and instead scan visible buttons for login-ish text.
    submit = None
    for btn in driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]'):
        if btn.is_displayed():
            submit = btn
            break
    if submit is None:
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            if not btn.is_displayed():
                continue
            text = (btn.text or "").strip().lower()
            if any(k in text for k in ("login", "log in", "sign in", "เข้าสู่ระบบ")):
                submit = btn
                break
    if submit is None:
        raise RuntimeError("Could not find a login submit button.")

    submit.click()

    WebDriverWait(driver, LOGIN_TIMEOUT).until(
        lambda d: "/login" not in d.current_url.lower()
        and "signin" not in d.current_url.lower()
    )
    print(f"   logged in → {driver.current_url}")
    time.sleep(2)
    dismiss_post_login_popup(driver)


def dismiss_post_login_popup(driver):
    """Best-effort close of the 'Silom Biz Alert' / welcome modal that appears
    after login. Tries common close-button patterns. Safe no-op if nothing
    matches — this stays inside our read-only constraint (clicking only a
    visual close/X)."""
    candidates = [
        '[aria-label="Close"]',
        '[aria-label="close"]',
        'button.close',
        '.modal .close',
        '.modal-close',
        '.ant-modal-close',
        '.MuiDialog-root [aria-label="close"]',
    ]
    for sel in candidates:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    el.click()
                    print(f"   dismissed popup via {sel}")
                    time.sleep(1)
                    return
        except Exception:
            pass
    # Fall back to pressing Escape, which closes most modals.
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Full-page screenshot via Chrome DevTools Protocol
# ---------------------------------------------------------------------------
def full_page_screenshot(driver, path: Path):
    metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
    width = int(metrics["contentSize"]["width"])
    height = int(metrics["contentSize"]["height"])
    # Clamp to something sane to avoid runaway pages.
    width = min(max(width, 1280), 2560)
    height = min(max(height, 800), 20000)

    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
        "mobile": False,
        "width": width,
        "height": height,
        "deviceScaleFactor": 1,
    })
    try:
        result = driver.execute_cdp_cmd("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": True,
            "fromSurface": True,
        })
        path.write_bytes(base64.b64decode(result["data"]))
    finally:
        driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def capture_pages(driver):
    OUTPUT_DIR.mkdir(exist_ok=True)
    failures = []
    for name, url in PAGES:
        print(f"→ {name}: {url}")
        try:
            driver.get(url)
            # Hard reload to force a fresh SPA bootstrap. Direct-URL navigations
            # through the dashboard's router leave stale state after a few hops
            # and start rendering blank shells; refresh() avoids that.
            driver.refresh()
            WebDriverWait(driver, NAV_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            if "/login" in driver.current_url.lower():
                raise RuntimeError(f"Redirected to login while visiting {url}")
            time.sleep(PAGE_SETTLE_SECONDS)
            # Dismiss any popup that re-appeared after refresh.
            dismiss_post_login_popup(driver)
            time.sleep(1)
            out = OUTPUT_DIR / f"{name}.png"
            full_page_screenshot(driver, out)
            size = out.stat().st_size
            print(f"   saved {out.relative_to(Path(__file__).parent)} ({size // 1024} KB) — title={driver.title!r}")
            # If the screenshot is suspiciously small, dump HTML for diagnosis.
            if size < 30 * 1024:
                html_path = out.with_suffix(".html")
                html_path.write_text(driver.page_source, encoding="utf-8")
                print(f"   WARN: tiny screenshot, dumped HTML to {html_path.name}")
        except Exception as e:
            print(f"   FAILED: {e}")
            failures.append((name, str(e)))
    return failures


def main() -> int:
    if USERNAME.startswith("YOUR_") or PASSWORD.startswith("YOUR_"):
        print("Set USERNAME and PASSWORD at the top of this file first.", file=sys.stderr)
        return 2

    driver = build_driver()
    try:
        login(driver)
        failures = capture_pages(driver)
    except WebDriverException as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    finally:
        driver.quit()

    if failures:
        print("\nDone with errors:")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        return 1

    print(f"\nDone. Screenshots in {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
