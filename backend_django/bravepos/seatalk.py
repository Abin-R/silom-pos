"""SeaTalk group alerts — the wire only.

A trimmed port of the Rolling Pinn Shopify app's ``seatalk/client.py``: the
same Open Platform app, the same two calls (fetch an app access token, post to
a group chat), and nothing else — this project has no bot, no cards and no
single chats.  Uses ``httpx`` rather than ``requests`` because that is what the
production virtualenv already has (see ``gateways``).

Config is three env vars, all read at call time:

* ``SEATALK_APP_ID`` / ``SEATALK_APP_SECRET`` — the Open Platform app.
* ``SEATALK_DISCOUNT_GROUP_ID`` — the group "Other" discount alerts go to.

Unset means "no alerts": :func:`send_group_text` quietly does nothing, so a dev
box or a fresh install never fails a sale over a chat message.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import httpx
from django.core.cache import cache

log = logging.getLogger(__name__)

API_HOST = "https://openapi.seatalk.io"
TOKEN_CACHE_KEY = "bravepos_seatalk_access_token"
TIMEOUT_S = 10

# SeaTalk rejects very long messages; everything we post is short, but a
# cashier's reason is free text and could in principle be anything.
MESSAGE_LIMIT = 3000
TRIM_NOTE = "\n\n[trimmed to fit one message]"

OK = 0
ACCESS_TOKEN_EXPIRED = 100


def _app() -> tuple:
    return (os.environ.get("SEATALK_APP_ID") or "",
            os.environ.get("SEATALK_APP_SECRET") or "")


def discount_group_id() -> str:
    return (os.environ.get("SEATALK_DISCOUNT_GROUP_ID") or "").strip()


def is_configured() -> bool:
    app_id, secret = _app()
    return bool(app_id and secret)


def fit_message(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Force ``text`` into one SeaTalk message, trimming from the end."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - len(TRIM_NOTE)].rstrip() + TRIM_NOTE


def _refresh_token() -> str:
    app_id, secret = _app()
    res = httpx.post(f"{API_HOST}/auth/app_access_token",
                     json={"app_id": app_id, "app_secret": secret},
                     timeout=TIMEOUT_S)
    res.raise_for_status()
    data = res.json()
    if data.get("code") != OK:
        raise ValueError(f"SeaTalk token error: code {data.get('code')}")
    token = data.get("app_access_token") or ""
    # ``expire`` is an absolute unix time in SeaTalk's reply; a flat hour is
    # safely inside the token's two-hour life and needs no clock maths.
    cache.set(TOKEN_CACHE_KEY, token, 60 * 60)
    return token


def _post_group(group_id: str, text: str, token: str) -> dict:
    res = httpx.post(
        f"{API_HOST}/messaging/v2/group_chat",
        json={"group_id": group_id,
              "message": {"tag": "text", "text": {"content": text}}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_S,
    )
    res.raise_for_status()
    return res.json()


def send_group_text(group_id: str, text: str) -> bool:
    """Post ``text`` to a SeaTalk group.  Returns True when SeaTalk accepted it.

    Never raises: callers sit behind a sale that has already been paid for, and
    a chat message is not worth an error there.  Failures are logged (and so
    reach Sentry through its logging integration).
    """
    if not group_id or not is_configured():
        return False
    text = fit_message(text)
    try:
        token = cache.get(TOKEN_CACHE_KEY) or _refresh_token()
        data = _post_group(group_id, text, token)
        if data.get("code") == ACCESS_TOKEN_EXPIRED:
            data = _post_group(group_id, text, _refresh_token())
        if data.get("code") != OK:
            log.error("SeaTalk group message refused: code %s", data.get("code"))
            return False
        return True
    except Exception:  # noqa: BLE001 — see docstring
        log.exception("SeaTalk group message failed")
        return False


def send_group_text_async(group_id: str, text: str) -> Optional[threading.Thread]:
    """:func:`send_group_text` on a background thread.

    The alert is raised from inside the checkout request; up to two SeaTalk
    round trips must not sit between the cashier and the success screen.
    """
    if not group_id or not is_configured():
        return None
    t = threading.Thread(target=send_group_text, args=(group_id, text), daemon=True)
    t.start()
    return t
