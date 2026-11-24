"""HTTP endpoints for the Grab stock sync.

Called by the Grab stock poller, not by a person: no session, an ``X-API-Key``
header instead.  The rules are in ``bravepos.grab_sync``; these views only
check the key, parse the body, find the branch from the Grab merchant ID and
hand over.
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


def _start(request, parse):
    """Key, body, branch — the start of every POST endpoint.  ``(parsed, branch,
    None)`` or ``(None, None, Response)``."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return None, None, denied
    try:
        parsed = parse(request.data)
    except grab_sync.BadRequest as e:
        return None, None, Response({'detail': str(e)}, status=400)
    branch, missing = _branch_or_404(parsed['merchant_id'])
    if missing:
        return None, None, missing
    grab_sync.mark_audit_source(request)
    return parsed, branch, None


@api_view(['POST'])
def grab_deduct_orders(request):
    """Grab orders seen on the live pages: deduct what has not been deducted
    for each yet.  Safe to send the same order every cycle."""
    call, branch, early = _start(request, grab_sync.parse_deduct)
    if early:
        return early
    result = grab_sync.deduct_orders(branch, call['orders'], dry_run=call['dry_run'])
    # Orders that could not be read are reported, not deducted.
    result['invalid'] = call['invalid']
    return Response(result)


@api_view(['POST'])
def grab_heartbeat(request):
    """The poller finished a cycle (read by the watchdog only)."""
    beat, branch, early = _start(request, grab_sync.parse_heartbeat)
    if early:
        return early
    return Response(grab_sync.heartbeat(branch, beat, dry_run=beat['dry_run']))


@api_view(['GET'])
def grab_outlets(request):
    """The branches linked to a Grab outlet: the outlets the poller runs."""
    denied = grab_sync.check_api_key(request)
    if denied:
        return denied
    return Response(grab_sync.linked_outlets())


@api_view(['GET'])
def grab_stock(request):
    """The branch's products with what Grab should show, and when each target
    last moved, for the poller."""
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
