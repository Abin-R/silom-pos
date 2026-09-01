"""Rolling Pinn CRM (crm.rollingpinn.com) integration.

The CRM keeps its own list of shops — the one a customer sees in the loyalty
app, with a neighbourhood, opening hours and a photo.  The POS keeps its own,
because here a branch is what a till signs in to.  They are the *same* shops,
so a POS branch records which CRM branch it is (``Branch.crm_branch_id``) and
the two lists stop drifting apart: an order rung up on a till can be attributed
to the right shop on the customer's side without matching on a typed name.

Two calls, both authenticated with ``X-API-Key``:

* :func:`list_branches` — ``GET /branches/``.  The choices the branch form's
  CRM dropdown is built from.
* :func:`create_branch` — ``POST /branches/``.  Used for the dropdown's last
  entry, when the shop being added has no CRM row yet; returns the
  ``branch_id`` to store.

Config is two env vars.  ``CRM_API_KEY`` is required — with no key there is no
dropdown at all, and the branch form behaves exactly as it did before this
existed.  ``CRM_API_BASE_URL`` defaults to production and is there so a dev box
can be pointed at a local CRM.  The key is a secret and this repo is public, so
it is never written down here.

Every failure — no network, a 500 from the CRM, a body that isn't the shape we
expect — is raised as :class:`CrmError` with a sentence fit to show an admin.
Nothing in here is allowed to 500 the branch form: the CRM being down must
still leave a branch editable and savable.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://crm.rollingpinn.com/api"

# The CRM answers in well under a second when it is up; the point of a short
# timeout is that the branch form is rendered behind this call, so a CRM that
# has stopped answering costs the admin a few seconds and a warning banner
# rather than a hung page.
LIST_TIMEOUT = 8.0
# Creating is a write, and the reply carries the new id we have to store, so it
# is given longer than a read before we give up on it.
CREATE_TIMEOUT = 20.0


class CrmError(RuntimeError):
    """A CRM call did not produce a usable answer.

    The message is written for the admin standing in front of the branch form,
    not for a log line — it is rendered straight into the page.
    """


def _base_url() -> str:
    return (os.environ.get("CRM_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    return (os.environ.get("CRM_API_KEY") or "").strip()


def is_configured() -> bool:
    """True when a key is set, i.e. when CRM syncing should be offered at all.

    The branch form asks this first: an unconfigured deployment (a dev box, a
    fresh install) renders no CRM panel rather than a broken one.
    """
    return bool(_api_key())


def _headers() -> dict[str, str]:
    return {
        "X-API-Key": _api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _payload(resp: httpx.Response) -> dict[str, Any]:
    """The JSON body of a CRM reply, or a :class:`CrmError` explaining why not.

    A CRM that is misrouted or behind a login answers HTML with a 200 or a 404
    just as readily as it answers JSON, so the status code alone is not enough
    to trust the body.
    """
    try:
        body = resp.json()
    except ValueError:
        raise CrmError(
            f"The CRM answered {resp.status_code} but not in JSON — check that "
            f"{_base_url()} is the right address for its API."
        ) from None
    if not isinstance(body, dict):
        raise CrmError("The CRM sent back something this page can't read.")
    if resp.status_code >= 400 or body.get("ok") is False:
        detail = body.get("error") or body.get("detail") or f"HTTP {resp.status_code}"
        raise CrmError(f"The CRM refused the request: {detail}")
    return body


def list_branches() -> list[dict[str, Any]]:
    """Every branch the CRM knows about, in the order it wants them shown.

    Rows are passed through as the CRM sent them — ``id`` and ``name`` are the
    two fields anything here relies on, and the rest (``neighborhood``,
    ``hours_display``, ``active``) only decorate the dropdown, so a CRM that
    adds or drops a field doesn't break the page.
    """
    url = f"{_base_url()}/branches/"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=LIST_TIMEOUT)
    except httpx.HTTPError as exc:
        raise CrmError(f"Couldn't reach the CRM ({exc.__class__.__name__}).") from exc

    rows = _payload(resp).get("branches")
    if not isinstance(rows, list):
        raise CrmError("The CRM's branch list came back in an unexpected shape.")

    clean = [row for row in rows if isinstance(row, dict) and row.get("id") is not None]
    # display_order is the CRM's own ordering; name is the tiebreak so a batch
    # of branches sharing an order doesn't shuffle between page loads.
    clean.sort(key=lambda row: (row.get("display_order") or 0, str(row.get("name") or "")))
    return clean


def create_branch(name: str, *, neighborhood: str = "", hours_display: str = "",
                  active: bool = True) -> int:
    """Create a branch in the CRM and return its new ``id``.

    Called only for the dropdown's "none of the above" choice, and only once
    the rest of the branch form has already passed validation — a CRM branch
    created for a POS save that is then refused would be a stray row nobody
    asked for, and a second attempt would make another.
    """
    name = (name or "").strip()
    if not name:
        raise CrmError("A CRM branch needs a name.")

    url = f"{_base_url()}/branches/"
    body = {
        "name": name,
        "neighborhood": (neighborhood or "").strip(),
        "hours_display": (hours_display or "").strip(),
        "active": bool(active),
    }
    try:
        resp = httpx.post(url, headers=_headers(), json=body, timeout=CREATE_TIMEOUT)
    except httpx.HTTPError as exc:
        raise CrmError(f"Couldn't reach the CRM ({exc.__class__.__name__}).") from exc

    branch_id = _payload(resp).get("branch_id")
    try:
        return int(branch_id)
    except (TypeError, ValueError):
        raise CrmError(
            "The CRM accepted the branch but didn't say what its id is, so "
            "there is nothing to link to. Check the CRM and pick the branch "
            "from the list instead."
        ) from None
