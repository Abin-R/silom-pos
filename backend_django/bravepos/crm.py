"""Rolling Pinn CRM (crm.rollingpinn.com) integration.

The CRM keeps its own list of shops — the one a customer sees in the loyalty
app, with a neighbourhood, opening hours and a photo.  The POS keeps its own,
because here a branch is what a till signs in to.  They are the *same* shops,
so a POS branch records which CRM branch it is (``Branch.crm_branch_id``) and
the two lists stop drifting apart: an order rung up on a till can be attributed
to the right shop on the customer's side without matching on a typed name.

Six calls, all authenticated with ``X-API-Key``.  Two link the shops:

* :func:`list_branches` — ``GET /branches/``.  The choices the branch form's
  CRM dropdown is built from.
* :func:`create_branch` — ``POST /branches/``.  Used for the dropdown's last
  entry, when the shop being added has no CRM row yet; returns the
  ``branch_id`` to store.

The other four are the customer loyalty programme, driven from the till by
:mod:`bravepos.loyalty` — this module is only the wire, it holds no policy
about when any of them should be called:

* :func:`lookup_member` — ``GET /members/<phone>/``.  Points, tier and
  vouchers for one phone number.  A non-member is a *successful* answer with
  ``member: None``, not an error.
* :func:`register_member` — ``POST /members/``.  Signs a customer up.  Safe to
  repeat: a number that already has a membership comes back unchanged.
* :func:`record_order` — ``POST /orders/``.  Files a finished sale so it earns
  points and confirms the vouchers it consumed.
* :func:`void_order` — ``POST /orders/<id>/void/``.  Reverses one, taking the
  CRM's own order id — never our receipt number.

Money crosses the wire as decimal *strings* in both directions.  Amounts are
sent as strings for the same reason they are parsed as :class:`~decimal.Decimal`
here: a float baht total is a rounding bug waiting for a big enough bill.

Config is two env vars.  ``CRM_API_KEY`` is required — with no key there is no
dropdown and no loyalty at all, and both the branch form and the till behave
exactly as they did before any of this existed.  ``CRM_API_BASE_URL`` defaults to production and is there so a dev box
can be pointed at a local CRM.  The key is a secret and this repo is public, so
it is never written down here.

Every failure — no network, a 500 from the CRM, a body that isn't the shape we
expect — is raised as :class:`CrmError` with a sentence fit to show an admin.
Nothing in here is allowed to 500 the branch form: the CRM being down must
still leave a branch editable and savable.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any
from urllib.parse import quote

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

# The loyalty calls are made with a cashier standing at a counter, so they get
# shorter fuses than the branch form's.  A CRM that has stopped answering must
# cost the queue a few seconds, not twenty — and ``Branch.crm_loyalty_enabled``
# is there to stop it costing anything at all.
MEMBER_TIMEOUT = 6.0
ORDER_TIMEOUT = 8.0


class CrmError(RuntimeError):
    """A CRM call did not produce a usable answer.

    The message is written for the admin standing in front of the branch form,
    not for a log line — it is rendered straight into the page.
    """


class MemberNotFound(CrmError):
    """The phone number on this request has no membership in the CRM.

    Its own class because it is the one CRM failure that is *ordinary*: most
    people who buy a coffee are not in the loyalty programme, and the caller's
    reaction to that ("sign them up, then try again") is nothing like its
    reaction to a 500.  ``member_not_found`` is the only machine-readable error
    code the CRM promises; everything else arrives as a sentence.
    """


def _base_url() -> str:
    return (os.environ.get("CRM_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    return (os.environ.get("CRM_API_KEY") or "").strip()


def _allowlist() -> list[str]:
    """``CRM_BRANCHES`` split into entries, each a branch name or a branch id."""
    return [part.strip() for part in (os.environ.get("CRM_BRANCHES") or "").split(",")
            if part.strip()]


def branch_allowed(branch) -> bool:
    """May this branch use the CRM at all?

    The rollout control, and deliberately an env var rather than a checkbox:
    during the testing period exactly one branch is meant to touch the CRM, and
    a setting nobody can widen from inside the backoffice is what makes that
    true.  Ticking a box on the wrong branch cannot reach a live shop; adding
    one takes a deploy.

    Fail-closed — unset means *no* branch, so a deployment that sets a key but
    forgets this stays dark rather than opening on every shop.  ``*`` lifts the
    restriction, which is the one-word edit that ends the testing period.

    Entries match a branch's name (case-insensitively, since that is how an
    admin thinks of it) or its id (which a rename cannot break).
    """
    allow = _allowlist()
    if not allow:
        return False
    if "*" in allow:
        return True
    name = (getattr(branch, "name", "") or "").strip().lower()
    branch_id = str(getattr(branch, "id", "") or "")
    # A branch still being added has no name yet, so it matches nothing and
    # gets no panel — during the testing period a link is made by editing the
    # test branch, not by creating a new one.
    return any(entry.lower() == name or entry == branch_id
               for entry in allow if name or branch_id)


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
        if detail == "member_not_found":
            raise MemberNotFound("That phone number isn't a member yet.")
        raise CrmError(f"The CRM refused the request: {detail}")
    return body


def _request(method: str, path: str, *, timeout: float,
             json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One CRM call, with the transport failures turned into :class:`CrmError`.

    ``httpx`` raises a different exception for a DNS failure, a refused
    connection and a timeout; none of them mean anything to the caller here,
    which only ever has one reaction — give up on the CRM for this request and
    let the sale carry on without it.
    """
    url = f"{_base_url()}{path}"
    try:
        resp = httpx.request(
            method, url, headers=_headers(), json=json_body, timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise CrmError(f"Couldn't reach the CRM ({exc.__class__.__name__}).") from exc
    return _payload(resp)


def list_branches() -> list[dict[str, Any]]:
    """Every branch the CRM knows about, in the order it wants them shown.

    Rows are passed through as the CRM sent them — ``id`` and ``name`` are the
    two fields anything here relies on, and the rest (``neighborhood``,
    ``hours_display``, ``active``) only decorate the dropdown, so a CRM that
    adds or drops a field doesn't break the page.
    """
    rows = _request("GET", "/branches/", timeout=LIST_TIMEOUT).get("branches")
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

    body = {
        "name": name,
        "neighborhood": (neighborhood or "").strip(),
        "hours_display": (hours_display or "").strip(),
        "active": bool(active),
    }
    branch_id = _request(
        "POST", "/branches/", timeout=CREATE_TIMEOUT, json_body=body,
    ).get("branch_id")
    try:
        return int(branch_id)
    except (TypeError, ValueError):
        raise CrmError(
            "The CRM accepted the branch but didn't say what its id is, so "
            "there is nothing to link to. Check the CRM and pick the branch "
            "from the list instead."
        ) from None


# ─── Loyalty ────────────────────────────────────────────────────────────────
# Everything below serves the till.  The CRM identifies a customer by *phone
# number* and nothing else, and it normalises what it is given (E.164, default
# region TH) — so a number is always passed on exactly as the shop holds it.
# Half-normalising on this side is how one person ends up with two memberships.


def lookup_member(phone: str) -> dict[str, Any]:
    """Everything the CRM knows about the holder of ``phone``.

    The body is passed back as it arrived.  ``member`` is ``None`` for a number
    with no membership, which is a *successful* answer to "is this person one
    of ours" — only an unparseable or half-typed number is an error.

    ``member["available"]`` are vouchers the customer is holding and
    ``member["to_confirm"]`` ones they have already redeemed on their own
    phone.  The difference decides what a cashier may do with them; see
    :func:`bravepos.loyalty.member_for_customer`, which is where that rule
    lives.
    """
    phone = (phone or "").strip()
    if not phone:
        raise CrmError("A loyalty lookup needs a phone number.")
    # The number goes in the path, so anything with a slash or a space in it
    # would silently address a different route.
    return _request("GET", f"/members/{quote(phone, safe='')}/",
                    timeout=MEMBER_TIMEOUT)


def register_member(phone: str, name: str, *, email: str = "",
                    birthday: str = "", gender: str = "") -> dict[str, Any]:
    """Sign a customer up, and return the CRM's reply.

    Safe to repeat and safe to race: a number that already has a membership
    comes back ``created: False`` with that member, and **nothing on it is
    touched** — not even a blank field.  This creates memberships, it never
    edits them, so a shop that corrects a name in the POS does not correct it
    in the CRM (and cannot blank one over there by accident).

    Optional details are only sent when we actually hold them.  The CRM
    validates each one and refuses the whole registration over a malformed
    birthday, so an empty string must never be sent in place of "don't know".
    """
    name = (name or "").strip()
    if not name:
        # The CRM would refuse this anyway; catching it here keeps a nameless
        # walk-in from turning into a failed round trip mid-sale.
        raise CrmError("A CRM membership needs the customer's name.")

    body: dict[str, Any] = {"phone": (phone or "").strip(), "name": name}
    if email:
        body["email"] = email
    if birthday:
        body["birthday"] = birthday
    if gender:
        body["gender"] = gender
    return _request("POST", "/members/", timeout=MEMBER_TIMEOUT, json_body=body)


def record_order(*, phone: str, amount: Decimal | str, receipt_id: str,
                 branch_id: int | None = None, staff_name: str = "",
                 reward_ids: list[int] | None = None,
                 notify: bool = True) -> dict[str, Any]:
    """File a finished sale against a membership.  Returns the CRM's reply.

    ``receipt_id`` is the whole of the retry safety: re-posting one the CRM has
    already recorded returns that same order rather than paying the customer
    twice, so it is required here even though the CRM would accept a blank.

    ``reward_ids`` may only usefully contain vouchers in the member's
    ``to_confirm`` list.  Anything else — one the customer is still holding,
    one belonging to somebody else, one already spent — is dropped by the CRM
    *silently*, which is why the till is not allowed to offer them (see
    :func:`bravepos.loyalty.member_for_customer`).

    Raises :class:`MemberNotFound` when the phone has no membership.  Nothing
    is written in that case, and ``auto_create_customer`` is deliberately not
    used: the name on a POS customer row is whatever a cashier typed, and a
    membership carrying "walk-in" can never be tidied up once it has orders
    against it.  Register first, then file.
    """
    receipt_id = (receipt_id or "").strip()
    if not receipt_id:
        raise CrmError("A loyalty order needs a receipt number to be safe to retry.")

    body: dict[str, Any] = {
        "phone": (phone or "").strip(),
        # A string, not a float: the CRM parses these as decimals and floors
        # the points off them, and a total that arrives as 789.9999999 earns
        # the customer one point fewer than the receipt in their hand says.
        "amount": str(Decimal(str(amount))),
        "receipt_id": receipt_id,
        "staff_name": (staff_name or "")[:200],
        "reward_ids": [int(r) for r in (reward_ids or [])],
        "notify": bool(notify),
    }
    if branch_id is not None:
        body["branch_id"] = int(branch_id)
    # ``source`` is left at the CRM's default of "api" on purpose — it is the
    # value every machine-filed order has ever carried, and splitting it would
    # split the shop's own history in two.
    return _request("POST", "/orders/", timeout=ORDER_TIMEOUT, json_body=body)


def void_order(order_id: int, *, note: str = "") -> dict[str, Any]:
    """Reverse a filed sale: points, spend, visit count, tier and vouchers.

    Takes the CRM's own order id, which is why we store it — one receipt
    number can name several CRM orders over its life (keyed, voided, re-keyed)
    and only the caller knows which one it means.

    Idempotent: voiding an already-voided order changes nothing and answers
    ``already_voided: True``, so a retry after a timeout is safe.  There is no
    ownership check on the CRM side, so only ids we recorded ourselves are ever
    sent here.
    """
    return _request("POST", f"/orders/{int(order_id)}/void/",
                    timeout=ORDER_TIMEOUT, json_body={"note": (note or "")})
