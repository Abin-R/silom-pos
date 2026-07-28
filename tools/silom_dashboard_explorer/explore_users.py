"""
SilomPOS Dashboard Explorer — user management & audit log pages. READ-ONLY.

Second pass over `dashboard.silompos.com` (the first pass, `explore.py`,
captured reports/products). This one:

  1. logs in,
  2. dumps every nav/anchor link it can see into `links.json` so we can find
     where "user management" and "audit log" actually live, and
  3. screenshots each discovered candidate page.

Like `explore.py` this never submits a form or clicks a save/delete action —
it navigates, closes modals, and captures pixels.

Usage:
    python explore_users.py            # discover + capture
    python explore_users.py --links    # discovery only, no screenshots
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from selenium.webdriver.common.by import By

# Reuse the login/screenshot plumbing from the first-pass explorer.
sys.path.insert(0, str(Path(__file__).parent))
from explore import (  # noqa: E402
    BASE,
    NAV_TIMEOUT,
    PAGE_SETTLE_SECONDS,
    build_driver,
    dismiss_post_login_popup,
    full_page_screenshot,
    login,
)

import time  # noqa: E402

from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

OUTPUT_DIR = Path(__file__).parent / "screenshots"
LINKS_PATH = Path(__file__).parent / "links.json"

# Words that mark a link as worth capturing for this pass.
KEYWORDS = (
    "user", "users", "employee", "staff", "member", "permission", "role",
    "audit", "log", "logs", "history", "activity", "setting", "settings",
    "account", "ผู้ใช้", "พนักงาน", "สิทธิ", "ประวัติ", "บันทึก", "ตั้งค่า",
)

# Direct guesses, tried in addition to whatever the sidebar reveals. A 404 or
# a redirect back to /dashboard just gets skipped.
CANDIDATE_PATHS = [
    "/setting/user",
    "/setting/users",
    "/setting/employee",
    "/setting/permission",
    "/user",
    "/users",
    "/employee",
    "/staff",
    "/setting/log",
    "/setting/auditlog",
    "/report/log",
    "/report/staff",
    "/log",
    "/auditlog",
    "/activity",
]


def expand_sidebar(driver):
    """Nav groups are collapsed accordions; clicking the group header reveals
    its child links. Clicking a nav header only expands a menu — still
    read-only."""
    headers = driver.find_elements(
        By.CSS_SELECTOR,
        "aside li, aside a, nav li, nav a, .sidebar li, .sidebar a, [class*='menu'] li",
    )
    for el in headers[:80]:
        try:
            if el.is_displayed() and el.text.strip():
                el.click()
                time.sleep(0.4)
        except Exception:
            pass


def collect_links(driver):
    found = {}
    for a in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = a.get_attribute("href") or ""
            text = (a.text or "").strip()
        except Exception:
            continue
        if not href or not href.startswith(BASE):
            continue
        found[href] = text
    return found


def discover(driver):
    driver.get(f"{BASE}/dashboard")
    time.sleep(PAGE_SETTLE_SECONDS)
    dismiss_post_login_popup(driver)
    links = collect_links(driver)
    expand_sidebar(driver)
    time.sleep(1)
    links.update(collect_links(driver))
    return links


def interesting(links: dict[str, str]) -> list[tuple[str, str]]:
    hits = []
    for href, text in links.items():
        blob = f"{href} {text}".lower()
        if any(k in blob for k in KEYWORDS):
            hits.append((href, text))
    return sorted(hits)


def capture(driver, name: str, url: str) -> bool:
    print(f"→ {name}: {url}")
    try:
        driver.get(url)
        driver.refresh()
        WebDriverWait(driver, NAV_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        if "/login" in driver.current_url.lower():
            print("   redirected to login — skipping")
            return False
        time.sleep(PAGE_SETTLE_SECONDS)
        dismiss_post_login_popup(driver)
        time.sleep(1)
        landed = driver.current_url
        if landed.rstrip("/").endswith("/dashboard") and not url.endswith("/dashboard"):
            print(f"   bounced to dashboard — page does not exist")
            return False
        out = OUTPUT_DIR / f"{name}.png"
        full_page_screenshot(driver, out)
        (OUTPUT_DIR / f"{name}.html").write_text(driver.page_source, encoding="utf-8")
        print(f"   saved {out.name} ({out.stat().st_size // 1024} KB) — {driver.title!r} @ {landed}")
        return True
    except Exception as e:
        print(f"   FAILED: {e}")
        return False


def main() -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    links_only = "--links" in sys.argv

    # Explicit paths on the command line skip discovery entirely.
    explicit = [a for a in sys.argv[1:] if a.startswith("/")]

    driver = build_driver()
    try:
        login(driver)
        if explicit:
            OUTPUT_DIR.mkdir(exist_ok=True)
            ok = 0
            for i, path in enumerate(explicit, start=1):
                name = f"u{i:02d}_" + (path.strip("/").replace("/", "_") or "root")
                if capture(driver, name, BASE + path):
                    ok += 1
            print(f"\nDone — {ok}/{len(explicit)} captured into {OUTPUT_DIR}")
            return 0

        links = discover(driver)
        LINKS_PATH.write_text(json.dumps(links, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nDiscovered {len(links)} links → {LINKS_PATH.name}")

        hits = interesting(links)
        print(f"\n{len(hits)} look user/audit related:")
        for href, text in hits:
            print(f"  {text or '(no text)':<40} {href}")

        if links_only:
            return 0

        targets: list[tuple[str, str]] = []
        seen = set()
        for href, text in hits:
            path = href[len(BASE):] or "/"
            if path in seen:
                continue
            seen.add(path)
            targets.append((path.strip("/").replace("/", "_") or "root", href))
        for path in CANDIDATE_PATHS:
            if path in seen:
                continue
            seen.add(path)
            targets.append((path.strip("/").replace("/", "_"), BASE + path))

        print(f"\nCapturing {len(targets)} pages…")
        ok = 0
        for i, (name, url) in enumerate(targets, start=1):
            if capture(driver, f"u{i:02d}_{name}", url):
                ok += 1
        print(f"\nDone — {ok}/{len(targets)} captured into {OUTPUT_DIR}")
    finally:
        driver.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
