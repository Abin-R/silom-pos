"""HTTP endpoints for the Grab stock sync.

Called by the Grab stock poller, not by a person: no session, an ``X-API-Key``
header instead.  The rules are in ``bravepos.grab_sync``; these views only
check the key, parse the body, find the branch from the Grab merchant ID and
hand over.  (The till's own ``POST /api/grab/till-check`` is in ``views`` — it
needs the till's session.)
"""
from __future__ import annotations

from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import grab_sync


def _branch_or_404(merchant_id):
    """``(branch, None)``, or ``(None, Response)`` for the 404."""
    branch = grab_sync.find_branch(merchant_id)
    if branch is None:
        return None, Response(
            {'detail': 'No active branch has that Grab merchant ID.'}, status=404)
    return branch, None


def _order_call(request, parser=grab_sync.parse_order):
    """Key, body, branch — the start of every order endpoint.  ``(order, branch,
    None)`` or ``(None, None, Response)``."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return None, None, denied
    try:
        order = parser(request.data)
    except grab_sync.BadRequest as e:
        return None, None, Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(order['merchant_id'])
    if missing:
        return None, None, missing
    grab_sync.mark_audit_source(request)
    return order, branch, None


@api_view(['POST'])
def grab_hold_orders(request):
    """Hold the units of every Grab order in the batch (create or update)."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return denied
    try:
        merchant_id, orders, dry_run = grab_sync.parse_hold_orders(request.data)
    except grab_sync.BadRequest as e:
        return Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(merchant_id)
    if missing:
        return missing
    grab_sync.mark_audit_source(request)
    return Response(grab_sync.hold_orders(branch, orders, dry_run=dry_run))


@api_view(['POST'])
def grab_convert_order(request):
    """A Grab order was packed: its hold becomes a deduction, once."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return denied
    try:
        order = grab_sync.parse_order(request.data)
        raw = request.data.get('items')
        items = grab_sync.parse_items(raw) if raw is not None else None
    except grab_sync.BadRequest as e:
        return Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(order['merchant_id'])
    if missing:
        return missing
    grab_sync.mark_audit_source(request)
    return Response(grab_sync.convert_order(
        branch, order['order_id'], order['display_id'], items,
        ready_at=order['ready_at'], go_live_at=order['go_live_at'],
        grab_state=order['grab_state'], dry_run=order['dry_run'],
    ))


@api_view(['POST'])
def grab_release_order(request):
    """A Grab order was cancelled before it was packed: drop its hold."""
    order, branch, early = _order_call(request)
    if early:
        return early
    return Response(grab_sync.release_order(
        branch, order['order_id'], order['reason'], dry_run=order['dry_run']))


@api_view(['POST'])
def grab_restore_order(request):
    """A converted Grab order was cancelled: put its stock back, once."""
    order, branch, early = _order_call(request)
    if early:
        return early
    return Response(grab_sync.restore_order(
        branch, order['order_id'], order['display_id'], order['reason'],
        grab_state=order['grab_state'], dry_run=order['dry_run'],
    ))


@api_view(['POST'])
def grab_correct_order(request):
    """A converted Grab order was edited: write only the difference."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return denied
    try:
        order = grab_sync.parse_order(request.data)
        items = grab_sync.parse_items(request.data.get('items'))
    except grab_sync.BadRequest as e:
        return Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(order['merchant_id'])
    if missing:
        return missing
    grab_sync.mark_audit_source(request)
    return Response(grab_sync.correct_order(
        branch, order['order_id'], order['display_id'], items,
        grab_state=order['grab_state'] or 'COMPLETED', dry_run=order['dry_run'],
    ))


@api_view(['POST'])
def grab_review_order(request):
    """The poller cannot tell what happened to an order: open a review."""
    order, branch, early = _order_call(request, grab_sync.parse_review)
    if early:
        return early
    return Response(grab_sync.review_order(branch, order, dry_run=order['dry_run']))


@api_view(['POST'])
def grab_heartbeat(request):
    """The poller finished a cycle.  Answers whether the branch is in outage."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return denied
    try:
        beat = grab_sync.parse_heartbeat(request.data)
    except grab_sync.BadRequest as e:
        return Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(beat['merchant_id'])
    if missing:
        return missing
    grab_sync.mark_audit_source(request)
    return Response(grab_sync.heartbeat(branch, beat, dry_run=beat['dry_run']))


@api_view(['GET'])
def grab_stock(request):
    """The branch's products with what Grab may show, the open Grab orders and
    when each target last moved, for the poller."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return denied
    try:
        merchant_id = grab_sync.parse_merchant_id(request.query_params.get('merchant_id'))
    except grab_sync.BadRequest as e:
        return Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(merchant_id)
    if missing:
        return missing
    return Response(grab_sync.stock_for_branch(branch))
