# SilomPOS Dashboard Explorer

Read-only Selenium script. Logs into `dashboard.silompos.com`, visits each
target page, and saves a full-page screenshot. Used to map out what the
in-house backoffice (backed by `backend_django/`) needs to replicate.

**Read-only guarantee:** the script does not click anything other than the
login button, does not submit forms beyond login, and does not touch any
update / delete / save action.

## Setup

```bash
cd tools/silom_dashboard_explorer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Selenium 4 auto-downloads a matching chromedriver, so only Chrome itself
needs to be installed system-wide.

## Run

Credentials are at the top of `explore.py`. After editing them:

```bash
python explore.py
```

PNGs land in `./screenshots/`, one per page. `screenshots/` is gitignored.

## Pages captured

- `/dashboard`
- `/report/transaction`
- `/report/daily`
- `/report/sell`
- `/report/sku`
- `/inventory/sku`
- `/product`

## Notes

- Headless is off by default so login captchas / 2FA prompts (if any) are
  visible. Uncomment the `--headless=new` line in `build_driver()` to run
  without a window.
- If the login form selectors don't match, edit `EMAIL_SELECTORS` /
  `PASSWORD_SELECTORS` near the top of `explore.py`.
- `PAGE_SETTLE_SECONDS` controls how long to wait after each navigation for
  the SPA to render and fetch data. Bump it if pages look half-loaded in
  the screenshots.
