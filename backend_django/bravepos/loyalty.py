"""Loyalty at the till — the POS half of the CRM's customer programme.

:mod:`bravepos.crm` is the wire.  This module is the policy: which branches
take part, which of a member's vouchers a cashier is allowed to touch, and —
above all — what happens when the CRM is not there.

**A loyalty call may never cost a sale.**  Every entry point here returns
rather than raises, and the two that run inside a checkout write their failure
to the log and nothing else.  Points are a nice-to-have on top of a bill; a
bill that would not save because a marketing system was down is a queue of
customers at a counter and money taken with no order recorded.  If that rule
ever has to be weakened, weaken it for the *lookup* (which happens while the
cashier is still building the basket) and never for the sale.

Three things decide whether a branch takes part, and all three have to hold:

1. ``CRM_API_KEY`` is set — otherwise there is no CRM to talk to at all.
2. ``Branch.crm_loyalty_enabled`` — the per-branch rollout switch and kill
   switch, off by default so this ships dark.
3. ``Branch.crm_branch_id`` — which CRM shop to file the sale against.  An
   order posted without one is attributed to nowhere, and the shop cannot tell
   afterwards where its loyalty trade came from.

The customer is identified by phone number and nothing else.  A POS customer
row with no phone simply has no loyalty: it is not an error and the till shows
no reward panel for them.

Where the calls sit in a sale
-----------------------------
``member_for_customer`` runs when the cashier picks a customer — early, while
the basket is still being built, so its latency is hidden.  ``record_sale``
runs inside the checkout POST, *after* the order has been committed, so a slow
CRM adds to the cashier's wait but can never roll a sale back.  That is a
deliberate trade against filing it in a background thread: a thread killed by a
worker restart loses the points silently and nothing on either side would ever
say so.  ``Branch.crm_loyalty_enabled`` is the answer if the wait ever bites.
"""
from __future__ import annotations

import logging
from typing import Any

from . import crm

logger = logging.getLogger("bravepos")

# POS gender values → the five the CRM accepts.  "unspecified" is deliberately
# absent: the CRM has ``prefer_not_to_say``, but that is the customer declining
# to answer, and ours means nobody was asked.  Unmapped values are dropped, not
# guessed — the CRM refuses the whole registration over a bad one.
_GENDER = {"male": "male", "female": "female"}


def enabled_for(branch) -> bool:
    """Should this branch's till be doing loyalty at all?

    Cheap and total: no exceptions, no network, safe to call on every request.
    """
    return bool(
        branch is not None
        and getattr(branch, "crm_loyalty_enabled", False)
        and getattr(branch, "crm_branch_id", None)
        and crm.is_configured()
    )


# ─── Looking a customer up ──────────────────────────────────────────────────

def _reward(row: dict[str, Any], *, redeemable: bool) -> dict[str, Any]:
    """One voucher, flattened for the till.

    ``redeemable`` is the whole point of this shape.  The CRM returns vouchers
    in two lists and only one of them is actionable: a voucher the customer is
    still *holding* can only be redeemed by them, on their own phone, and
    sending its id with an order is ignored **silently**.  A cashier looking at
    an unexplained toggle that does nothing would file it as a bug in the till
    every time, so the two kinds arrive here already told apart and the app
    renders the held ones as information rather than as a control.
    """
    return {
        "id": row.get("id"),
        "title": (row.get("about_line_1") or "Reward").strip(),
        "detail": (row.get("about_line_2") or "").strip(),
        "redeemable": redeemable,
        # The five minutes after the customer redeems.  Reported by the CRM,
        # not enforced by it — the confirm queue stays open indefinitely, so
        # this is a badge and never a reason to hide a voucher.
        "in_redemption_window": bool(row.get("in_redemption_window")),
        "expires_at": row.get("expires_at"),
    }


def _rewards(member: dict[str, Any]) -> list[dict[str, Any]]:
    """Every voucher on a member, the ones a cashier can act on first."""
    to_confirm = member.get("to_confirm")
    available = member.get("available")
    return (
        [_reward(r, redeemable=True) for r in to_confirm or [] if isinstance(r, dict)]
        + [_reward(r, redeemable=False) for r in available or [] if isinstance(r, dict)]
    )


def _summary(member: dict[str, Any]) -> dict[str, Any]:
    """The counter-screen facts about a member, without the voucher lists."""
    tier = member.get("tier") or {}
    return {
        "id": member.get("id"),
        "name": member.get("name") or "",
        # Decimal strings, passed through untouched — the app renders them and
        # never does arithmetic on them, so there is nothing to be gained by
        # turning them into floats on the way past.
        "points_balance": str(member.get("points_balance") or "0"),
        "total_spent": str(member.get("total_spent") or "0"),
        "order_count": member.get("order_count") or 0,
        "tier": (tier.get("title") or "") if isinstance(tier, dict) else "",
    }


def member_for_customer(branch, customer) -> dict[str, Any]:
    """The loyalty panel for one chosen customer, signing them up if needed.

    Returns a dict the till renders directly.  ``enabled`` False means "show
    nothing" and is the answer for every ordinary reason a customer has no
    loyalty — the branch is not in the rollout, the customer has no phone on
    file — so the app has one thing to check and no error states to design for.

    A number the CRM has never seen is registered on the spot, which is what
    makes the till's own customer list the front door to the programme.  That
    is safe to repeat: an existing membership comes back untouched.  It is
    *not* safe to do with a made-up name, which is why a nameless customer row
    is left alone rather than signed up as one.

    Raises :class:`crm.CrmError` — unlike the sale-time calls below, this one
    reports upward.  It runs before any money has moved, the caller is a view
    that can turn it into a 502, and a cashier who is told the lookup failed
    can retry it; one who is told nothing would read a member's blank reward
    list as "this customer has no rewards" and send them away.
    """
    if not enabled_for(branch):
        return {"enabled": False, "reason": "branch"}

    phone = (getattr(customer, "phone", "") or "").strip()
    if not phone:
        # Not a failure.  Phone number *is* the identity in the CRM, so a
        # customer row without one cannot be looked up or created, and the
        # cashier's fix is to edit the customer — which the panel says.
        return {"enabled": False, "reason": "no_phone"}

    body = crm.lookup_member(phone)
    member = body.get("member")
    created = False

    if not isinstance(member, dict):
        name = (getattr(customer, "name", "") or "").strip()
        if not name:
            return {"enabled": False, "reason": "no_name"}
        reply = crm.register_member(
            phone, name,
            email=(getattr(customer, "email", "") or "").strip(),
            birthday=(customer.birth_date.isoformat()
                      if getattr(customer, "birth_date", None) else ""),
            gender=_GENDER.get(getattr(customer, "gender", "") or "", ""),
        )
        member = reply.get("member")
        created = bool(reply.get("created"))
        if not isinstance(member, dict):
            # Registered, but the CRM did not hand the membership back.  The
            # customer is a member now and will look up fine next time; there
            # is just nothing to draw this time round.
            return {"enabled": True, "created": created, "member": None, "rewards": []}

    return {
        "enabled": True,
        "created": created,
        # Echo the CRM's normalised form rather than what the shop holds, so
        # the till shows the number the loyalty programme actually knows.
        "phone": body.get("phone_number") or phone,
        "member": _summary(member),
        "rewards": _rewards(member),
    }


# ─── Filing and reversing a sale ────────────────────────────────────────────

def _phone_for(order) -> str:
    """The phone number a finished order should be filed under, if any.

    Read off the *customer row*, not the order: an order carries only a name.
    A bill rung up with no customer, or against a customer since deleted, has
    no loyalty and that is the common case.
    """
    from .models import Customer  # local: models imports nothing from here

    if not order.customer_id:
        return ""
    return (
        Customer.objects.filter(pk=order.customer_id)
        .values_list("phone", flat=True)
        .first() or ""
    ).strip()


def record_sale(order, reward_ids: list[int] | None = None) -> None:
    """File a completed sale with the CRM so it earns points.  Never raises.

    Call **after** the order is committed.  On success the CRM's order id is
    stored on the row, which is the only thing that makes the sale reversible
    later; on any failure the row keeps ``crm_order_id`` null and the sale
    stands, unfiled, exactly as it would have before this feature existed.

    The amount filed is ``order.total`` — the grand total on the receipt,
    including VAT and any card processing fee.  It is what the customer paid
    and what they can read back off the slip in their hand, which is the number
    they will expect their points to have been calculated from.
    """
    if order.crm_order_id or not enabled_for(order.branch):
        return
    phone = _phone_for(order)
    if not phone:
        return

    def _file() -> dict[str, Any]:
        return crm.record_order(
            phone=phone,
            amount=order.total,
            # Our order number is the receipt id, and the CRM's idempotency key
            # with it: a retry of the same bill returns the order it already
            # made instead of paying the customer twice.
            receipt_id=order.order_number,
            branch_id=order.branch.crm_branch_id,
            staff_name=order.staff or "",
            reward_ids=reward_ids or [],
        )

    try:
        try:
            reply = _file()
        except crm.MemberNotFound:
            # The customer was never looked up at the till (loyalty was
            # switched on mid-basket, or the lookup failed), so sign them up
            # from the row we hold and file once more.  A membership needs a
            # name; without one there is nobody to file this against.
            name = (order.customer_name or "").strip()
            if not name:
                return
            crm.register_member(phone, name)
            reply = _file()
    except crm.CrmError as exc:
        # The sale is already saved and the customer has already paid.  Losing
        # the points is a bad afternoon; losing the bill is a bad day.
        logger.warning("CRM loyalty: order %s not filed — %s", order.order_number, exc)
        return

    try:
        order.crm_order_id = int(reply.get("order_id"))
    except (TypeError, ValueError):
        # Filed, but we were not told what it is called, so it can never be
        # voided from here.  Worth a louder line than a network blip: the two
        # sides are now out of step in a way only a person can put right.
        logger.error(
            "CRM loyalty: order %s was filed but the CRM returned no order_id; "
            "it cannot be voided from the POS.", order.order_number,
        )
        return
    order.save(update_fields=["crm_order_id"])


def reverse_sale(order, note: str = "") -> None:
    """Void this sale's CRM order: points, spend, visits, tier and vouchers.

    Never raises, for the same reason as :func:`record_sale` — a cashier
    voiding a mis-keyed bill must get the bill voided whatever the CRM is
    doing.  The CRM's void is idempotent, so a retry is harmless; what is *not*
    recoverable is a POS that refused to void.

    ``crm_order_id`` is cleared on success so the row states plainly that it is
    no longer filed anywhere, and so an un-void can file it afresh.
    """
    if not order.crm_order_id:
        return
    try:
        crm.void_order(order.crm_order_id, note=note or "Voided at the till.")
    except crm.CrmError as exc:
        logger.warning(
            "CRM loyalty: order %s (CRM #%s) was voided at the till but not in "
            "the CRM — %s", order.order_number, order.crm_order_id, exc,
        )
        return
    order.crm_order_id = None
    order.save(update_fields=["crm_order_id"])


def refile_sale(order) -> None:
    """File a sale again after it was un-voided.  Never raises.

    The CRM has no un-void; its own prescribed correction is to void and post
    the same receipt again, which is exactly what an un-cancel here amounts to.

    The vouchers the original order confirmed are **not** re-attached.  Voiding
    released them back into the customer's confirm queue, and nothing in the
    POS remembers which ones they were — so they wait there to be put on a
    later bill, which is the right place for them and the only honest one.
    """
    record_sale(order)
