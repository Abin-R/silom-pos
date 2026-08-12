"""Brave POS HTTP views.

Each endpoint pairs 1:1 with a route from the legacy FastAPI backend
(``../backend/server.py``) so the frontend doesn't change.  Routes still
TODO return 501 so we always have a clear "not ported yet" signal during
the migration.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone as djtz
from rest_framework import status, viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import audit, gateways
from .orders import create_order_from_items
from .peak import flag_branch_day_for_reissue
from .gateways import GatewayConfigError, GatewayError, get_shop_settings
from .models import (
    Branch, BranchSession, Category, Customer, DrawerCategory, Order, OrderItem,
    ParkedOrder, Product, SelfOrder, Shift, ShiftMovement, Staff,
    StockMovement, StockDocument, StockDocumentItem, StockOutReason,
)
from .serializers import (
    BranchSerializer,
    CategorySerializer, CustomerSerializer, DrawerCategorySerializer,
    OrderSerializer, ParkedOrderSerializer, ProductSerializer, SettingsSerializer,
    ShiftSerializer, ShiftMovementSerializer, StockMovementSerializer,
    StockDocumentSerializer, StockOutReasonSerializer,
)

logger = logging.getLogger("bravepos")


# ─── Session / branch context ────────────────────────────────────────────────
def get_session(request):
    """Look up the BranchSession for the caller's bearer token.

    Returns the session (with branch + staff prefetched) or ``None`` if the
    request has no token, an invalid token, or a token that has been replaced
    by a newer login on the same branch.  Touches ``last_seen_at`` on hit.
    """
    auth = request.headers.get('Authorization', '')
    if not auth.lower().startswith('bearer '):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        sess = BranchSession.objects.select_related('staff', 'branch').get(token=token)
    except BranchSession.DoesNotExist:
        return None
    sess.save(update_fields=['last_seen_at'])
    # Single choke point for POS API auth — tell the audit log who is acting
    # so writes made further down the view stack are attributed to this staff
    # member rather than landing as anonymous.
    audit.set_actor(sess.staff)
    return sess


def require_session(view):
    """Decorator: 401s if the caller has no valid BranchSession.

    On success, exposes the session as ``request.session_obj`` so the view can
    pull ``request.session_obj.branch`` / ``.staff`` without re-querying.
    """
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        sess = get_session(request)
        if sess is None:
            return Response(
                {'detail': 'Session expired or replaced by another login.'},
                status=401,
            )
        request.session_obj = sess
        return view(request, *args, **kwargs)
    return wrapped


def require_admin(view):
    """Decorator: 401 if no session, 403 if session isn't an admin."""
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        sess = get_session(request)
        if sess is None:
            return Response({'detail': 'Session expired.'}, status=401)
        if sess.staff.role != 'admin':
            return Response({'detail': 'Admin role required.'}, status=403)
        request.session_obj = sess
        return view(request, *args, **kwargs)
    return wrapped


class _Unauthenticated(Exception):
    """Raised by BranchScopedMixin.initial when no valid session exists.
    Mapped to a 401 by the ViewSet's ``handle_exception`` override below."""


class BranchScopedMixin:
    """ViewSet mixin: scopes the queryset to the caller's branch and stamps
    ``branch`` on every create/update.  Returns 401 if there is no valid session.
    """
    branch_field = 'branch'

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        sess = get_session(request)
        if sess is None:
            raise _Unauthenticated()
        request.session_obj = sess

    def handle_exception(self, exc):
        if isinstance(exc, _Unauthenticated):
            return Response(
                {'detail': 'Session expired or replaced by another login.'},
                status=401,
            )
        return super().handle_exception(exc)

    def get_queryset(self):
        qs = super().get_queryset()
        sess = getattr(self.request, 'session_obj', None)
        if sess is None:
            return qs.none()
        return qs.filter(**{self.branch_field: sess.branch})

    def perform_create(self, serializer):
        sess = self.request.session_obj
        serializer.save(**{self.branch_field: sess.branch})


# ─── Healthcheck ─────────────────────────────────────────────────────────────
@api_view(['GET'])
def api_root(_request):
    return Response({'service': 'bravepos', 'status': 'ok'})


# ─── Auth (email + password, branch-scoped session) ──────────────────────────
@api_view(['POST'])
def auth_login(request):
    """Body: { email, password, branch_id }.

    Branch-scoped login.  If the requested branch already has an active
    session, that one is replaced (the previous user's token stops working).
    Returns: { token, staff: { id, name, role, email } }
    """
    email = (request.data or {}).get('email', '').strip().lower()
    password = (request.data or {}).get('password', '')
    branch_id = (request.data or {}).get('branch_id')

    if not email or not password:
        return Response({'detail': 'Email and password are required.'}, status=400)
    if not branch_id:
        return Response({'detail': 'Branch is required.'}, status=400)

    try:
        staff = Staff.objects.get(email=email, active=True)
    except Staff.DoesNotExist:
        return Response({'detail': 'Invalid credentials.'}, status=401)

    if not staff.check_password(password):
        return Response({'detail': 'Invalid credentials.'}, status=401)

    try:
        branch = Branch.objects.get(id=branch_id, active=True)
    except Branch.DoesNotExist:
        return Response({'detail': 'Branch not found.'}, status=404)

    # Cashiers must be explicitly assigned to the branch; admins can log into
    # any branch (so they don't get locked out of branch management).
    if staff.role != 'admin' and not staff.branches.filter(id=branch.id).exists():
        return Response({'detail': 'This account is not allowed at this branch.'}, status=403)

    # Block concurrent logins with the same account.  If this staff is already
    # signed in anywhere (any branch, any browser), reject — the human can
    # still switch branches via /auth/switch-branch, but two browsers can't
    # both hold the same identity at once.
    existing_user_session = (
        BranchSession.objects
        .select_related('branch')
        .filter(staff=staff)
        .first()
    )
    if existing_user_session:
        return Response({
            'detail': (
                f'This account is already signed in at '
                f'{existing_user_session.branch.name}. Sign out there first.'
            ),
            'code': 'user_already_signed_in',
            'occupied_branch': existing_user_session.branch.name,
        }, status=409)

    # Multiple staff may be signed in to one branch at once (e.g. admin phone
    # + cashier phone). The per-staff guard above already prevents the same
    # account from holding two sessions, so just create this one.
    sess = BranchSession.objects.create(
        branch=branch,
        staff=staff,
        token=BranchSession.new_token(),
    )

    return Response({
        'token': sess.token,
        'staff': {
            'id': str(staff.id),
            'email': staff.email,
            'name': staff.name,
            'role': staff.role,
        },
        'branch': {
            'id': str(branch.id),
            'name': branch.name,
        },
        # legacy shape so older frontend code keeps working
        'role': staff.role,
        'name': staff.name,
    })


@api_view(['POST'])
def auth_switch_branch(request):
    """Body: { branch_id }.  Admin-only: swap the caller's session to a
    different branch without re-entering email/password.

    Returns the same shape as ``auth_login`` so the frontend can persist it
    identically (token + staff + branch).
    """
    sess = get_session(request)
    if sess is None:
        return Response({'detail': 'Session expired.'}, status=401)
    if sess.staff.role != 'admin':
        return Response({'detail': 'Admin role required to switch branches.'}, status=403)

    branch_id = (request.data or {}).get('branch_id')
    if not branch_id:
        return Response({'detail': 'Branch is required.'}, status=400)
    try:
        branch = Branch.objects.get(id=branch_id, active=True)
    except Branch.DoesNotExist:
        return Response({'detail': 'Branch not found.'}, status=404)

    if str(branch.id) == str(sess.branch.id):
        # No-op: already on this branch.  Return current session so the client
        # doesn't have to special-case it.
        return Response({
            'token': sess.token,
            'staff': {'id': str(sess.staff.id), 'email': sess.staff.email,
                      'name': sess.staff.name, 'role': sess.staff.role},
            'branch': {'id': str(branch.id), 'name': branch.name},
        })

    staff = sess.staff
    # Drop only our own current session (this admin can't hold two), then
    # create a new one bound to the target branch. Other staff already signed
    # in to that branch are left alone.
    sess.delete()
    new_sess = BranchSession.objects.create(
        branch=branch, staff=staff, token=BranchSession.new_token(),
    )
    return Response({
        'token': new_sess.token,
        'staff': {'id': str(staff.id), 'email': staff.email,
                  'name': staff.name, 'role': staff.role},
        'branch': {'id': str(branch.id), 'name': branch.name},
    })


@api_view(['POST'])
def auth_logout(request):
    """Deletes the caller's session.  Idempotent.

    Token is read from the ``Authorization: Bearer <token>`` header (preferred)
    or, for legacy callers, from the JSON body's ``token`` field.
    """
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not token:
        token = (request.data or {}).get('token', '') or ''
    if token:
        BranchSession.objects.filter(token=token).delete()
    return Response({'ok': True})


@api_view(['GET'])
def auth_me(request):
    """Returns the current authenticated staff (validates the bearer token)."""
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.lower().startswith('bearer ') else ''
    if not token:
        return Response({'detail': 'Not authenticated.'}, status=401)
    try:
        sess = BranchSession.objects.select_related('staff', 'branch').get(token=token)
    except BranchSession.DoesNotExist:
        return Response({'detail': 'Session expired or replaced by another login.'}, status=401)
    sess.save(update_fields=['last_seen_at'])
    return Response({
        'staff': {
            'id': str(sess.staff.id),
            'email': sess.staff.email,
            'name': sess.staff.name,
            'role': sess.staff.role,
        },
        'branch': {
            'id': str(sess.branch.id),
            'name': sess.branch.name,
        },
    })


# Legacy PIN endpoint — kept so existing installed APKs keep working until
# the new login UI is deployed.  Will be removed once everyone migrated.
@api_view(['POST'])
def verify_pin(request):
    pin = (request.data or {}).get('pin', '')
    if pin == '1234':
        return Response({'role': 'admin', 'name': 'Admin'})
    if pin == '0000':
        return Response({'role': 'cashier', 'name': 'Cashier'})
    return Response({'detail': 'Invalid PIN'}, status=status.HTTP_401_UNAUTHORIZED)


# ─── PIN-pad login (new flow) ────────────────────────────────────────────────
@api_view(['GET'])
def auth_branch_users(request):
    """List the staff who can sign in at a given branch.

    Query: ?branch_id=<uuid>. Returns the admins and cashiers assigned to
    that branch (every branch gets its own Admin + Cashier on creation —
    see bravepos.staff_provisioning). Unauthenticated, since the PIN-pad
    screen needs this before any session exists.
    """
    branch_id = request.query_params.get('branch_id')
    if not branch_id:
        return Response({'detail': 'branch_id is required.'}, status=400)
    try:
        branch = Branch.objects.get(id=branch_id, active=True)
    except (Branch.DoesNotExist, ValueError):
        return Response({'detail': 'Branch not found.'}, status=404)

    admins = Staff.objects.filter(role='admin', active=True, branches=branch).order_by('name')
    cashiers = Staff.objects.filter(role='cashier', active=True, branches=branch).order_by('name')
    users = [
        {'id': str(s.id), 'name': s.name, 'role': s.role}
        for s in list(admins) + list(cashiers)
    ]
    return Response({'branch': {'id': str(branch.id), 'name': branch.name}, 'users': users})


@api_view(['POST'])
def auth_pin_login(request):
    """Body: { branch_id, staff_id, pin }.

    PIN-pad equivalent of /auth/login. Same session semantics: multiple staff
    may be signed in to one branch at once, but a single staff account is
    limited to one active session (reject if already signed in elsewhere).
    """
    branch_id = (request.data or {}).get('branch_id')
    staff_id = (request.data or {}).get('staff_id')
    pin = (request.data or {}).get('pin', '')

    if not branch_id or not staff_id or not pin:
        return Response({'detail': 'branch_id, staff_id, and pin are required.'}, status=400)

    try:
        staff = Staff.objects.get(id=staff_id, active=True)
    except (Staff.DoesNotExist, ValueError):
        return Response({'detail': 'Invalid credentials.'}, status=401)

    if not staff.check_pin(pin):
        return Response({'detail': 'Invalid PIN.'}, status=401)

    try:
        branch = Branch.objects.get(id=branch_id, active=True)
    except (Branch.DoesNotExist, ValueError):
        return Response({'detail': 'Branch not found.'}, status=404)

    if staff.role != 'admin' and not staff.branches.filter(id=branch.id).exists():
        return Response({'detail': 'This account is not allowed at this branch.'}, status=403)

    # One active session per staff account — including the same branch.
    # Two devices can share a branch, but not the same identity.
    existing = (
        BranchSession.objects
        .select_related('branch')
        .filter(staff=staff)
        .first()
    )
    if existing:
        return Response({
            'detail': (
                f'This account is already signed in at '
                f'{existing.branch.name}. Sign out there first.'
            ),
            'code': 'user_already_signed_in',
            'occupied_branch': existing.branch.name,
        }, status=409)

    sess = BranchSession.objects.create(
        branch=branch, staff=staff, token=BranchSession.new_token(),
    )
    return Response({
        'token': sess.token,
        'staff': {
            'id': str(staff.id),
            'email': staff.email,
            'name': staff.name,
            'role': staff.role,
        },
        'branch': {'id': str(branch.id), 'name': branch.name},
        'role': staff.role,
        'name': staff.name,
    })


# ─── ViewSets — basic CRUD ───────────────────────────────────────────────────
class CategoryViewSet(BranchScopedMixin, viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class ProductViewSet(BranchScopedMixin, viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        category_id = self.request.query_params.get('category_id')
        active = self.request.query_params.get('active')
        if category_id:
            qs = qs.filter(category_id=category_id)
        if active in ('true', 'false'):
            qs = qs.filter(active=(active == 'true'))
        return qs


class CustomerViewSet(BranchScopedMixin, viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer


class DrawerCategoryViewSet(BranchScopedMixin, viewsets.ModelViewSet):
    """Paid In / Paid Out reason codes, branch-scoped + admin-editable.

    Filter to one side with ``?type=paid_in`` / ``?type=paid_out``; the Drawer
    picker passes ``?active=true`` to hide deactivated rows from cashiers.
    """
    queryset = DrawerCategory.objects.all()
    serializer_class = DrawerCategorySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        type_ = self.request.query_params.get('type')
        active = self.request.query_params.get('active')
        if type_ in ('paid_in', 'paid_out'):
            qs = qs.filter(type=type_)
        if active in ('true', 'false'):
            qs = qs.filter(active=(active == 'true'))
        return qs


class StockOutReasonViewSet(BranchScopedMixin, viewsets.ModelViewSet):
    """Stock-out reason codes, branch-scoped + admin-editable.

    The Stock-Out document form passes ``?active=true`` so a deactivated
    reason disappears from the picker without breaking the documents that
    already recorded it — those snapshot the name onto ``StockDocument.reason``.
    """
    queryset = StockOutReason.objects.all()
    serializer_class = StockOutReasonSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        active = self.request.query_params.get('active')
        if active in ('true', 'false'):
            qs = qs.filter(active=(active == 'true'))
        return qs


class BranchViewSet(viewsets.ModelViewSet):
    """Branches (physical shop locations).

    Reads are unauthenticated so the login screen can populate the branch
    dropdown.  Mutations require an admin session.
    """
    queryset = Branch.objects.all()
    serializer_class = BranchSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get('active') == 'true':
            qs = qs.filter(active=True)
        return qs

    def _require_admin(self):
        sess = get_session(self.request)
        if sess is None:
            from rest_framework.exceptions import NotAuthenticated
            raise NotAuthenticated('Session required.')
        if sess.staff.role != 'admin':
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Admin role required to edit branches.')

    def create(self, request, *args, **kwargs):
        """Admin-only branch create.

        Accepts two optional fields, ``cashier_email`` + ``cashier_password``.
        When both are present, a Staff(role=cashier) is created in the same
        transaction and attached to the new branch.  Either both must be
        provided or both omitted.
        """
        self._require_admin()
        data = request.data or {}
        cashier_email = (data.get('cashier_email') or '').strip().lower()
        cashier_password = data.get('cashier_password') or ''

        if bool(cashier_email) != bool(cashier_password):
            return Response(
                {'detail': 'Cashier email and password must both be provided, or both omitted.'},
                status=400,
            )

        if cashier_email and Staff.objects.filter(email=cashier_email).exists():
            return Response(
                {'detail': f'A staff account with email "{cashier_email}" already exists.'},
                status=400,
            )

        # Strip the cashier fields from the payload before DRF serializes the
        # Branch — the BranchSerializer doesn't know about them and DRF will
        # 400 on unknown fields in strict mode.
        clean = {k: v for k, v in data.items() if k not in ('cashier_email', 'cashier_password')}
        serializer = self.get_serializer(data=clean)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            self.perform_create(serializer)
            branch = serializer.instance
            if cashier_email:
                staff = Staff(
                    email=cashier_email,
                    name=f'{branch.name} Cashier',
                    role='cashier',
                )
                staff.set_password(cashier_password)
                staff.save()
                staff.branches.add(branch)

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        self._require_admin()
        return self._update_with_cashier(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self._require_admin()
        return self._update_with_cashier(request, *args, partial=True, **kwargs)

    def _update_with_cashier(self, request, *args, **kwargs):
        """Branch update + optional cashier email/password change.

        - ``cashier_email`` (string): rename the branch's primary cashier.
        - ``cashier_password`` (string): reset that cashier's password.
        - If no cashier exists yet, both fields together create one.
        - Duplicate-email guard mirrors the create path.
        """
        partial = kwargs.pop('partial', False)
        data = dict(request.data or {})
        new_email = (data.pop('cashier_email', None) or '').strip().lower() or None
        new_password = data.pop('cashier_password', None) or None

        instance = self.get_object()

        # Validate the branch fields first (matches default DRF flow).
        serializer = self.get_serializer(instance, data=data, partial=partial)
        serializer.is_valid(raise_exception=True)

        cashier = instance.staff.filter(role='cashier').order_by('created_at').first()

        # Email-change collision check
        if new_email and (not cashier or cashier.email != new_email):
            if Staff.objects.filter(email=new_email).exclude(id=getattr(cashier, 'id', None)).exists():
                return Response(
                    {'detail': f'A staff account with email "{new_email}" already exists.'},
                    status=400,
                )

        with transaction.atomic():
            self.perform_update(serializer)

            if cashier:
                changed = False
                if new_email and new_email != cashier.email:
                    cashier.email = new_email
                    changed = True
                if new_password:
                    cashier.set_password(new_password)
                    changed = True
                if changed:
                    cashier.save()
            elif new_email or new_password:
                # No existing cashier — need both to create one.
                if not (new_email and new_password):
                    return Response(
                        {'detail': 'To add a cashier, provide both email and password.'},
                        status=400,
                    )
                staff = Staff(email=new_email, name=f'{instance.name} Cashier', role='cashier')
                staff.set_password(new_password)
                staff.save()
                staff.branches.add(instance)

        if getattr(instance, '_prefetched_objects_cache', None):
            instance._prefetched_objects_cache = {}

        return Response(self.get_serializer(instance).data)

    def destroy(self, request, *args, **kwargs):
        self._require_admin()
        return super().destroy(request, *args, **kwargs)


# ─── Settings — singleton, GET + PUT only ────────────────────────────────────
# The Settings singleton accessor now lives in gateways.py (the payment layer
# needs it for credentials + fee rates, and importing it back from views would
# be circular).  Kept under the old name so the ~20 call sites below don't churn.
_get_or_create_settings = get_shop_settings


def _strip_payment_fields(data: dict) -> dict:
    """Remove payment config from anything crossing the POS API.

    Payment credentials are backoffice-only.  The app used to read and write
    them on this endpoint (Settings → Payment), but that screen is gone: a POS
    session is a tablet on a shop counter, and it has no business either
    learning the merchant account or being able to change where money lands.

    Stripping rather than masking, on both directions, so an older app build
    still in the field can neither read a key nor write one back — its stale
    copy of the payment fields is simply ignored.
    """
    for field in gateways.PAYMENT_FIELDS:
        data.pop(field, None)
    return data


@api_view(['GET', 'PUT'])
@require_session
def settings_view(request):
    obj = _get_or_create_settings()
    if request.method == 'GET':
        return Response(_strip_payment_fields(dict(SettingsSerializer(obj).data)))

    ser = SettingsSerializer(obj, data=_strip_payment_fields(dict(request.data)), partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(_strip_payment_fields(dict(SettingsSerializer(obj).data)))


# ─── Orders ──────────────────────────────────────────────────────────────────
ORDERS_MAX_LIMIT = 500


def _parse_bound(raw):
    """Parse an ISO-8601 ``from``/``to`` query bound into an aware datetime.

    The client sends the *device's* local day boundary as a full ISO string
    (with offset), because "today" on the till has to mean today on the wall
    clock, not in UTC.  A naive value is read in the project timezone.
    Returns None for anything unparseable so a typo widens the window instead
    of 500-ing the till.
    """
    if not raw:
        return None
    raw = raw.strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        # A "+07:00" offset that reached us unencoded arrives as a space.
        # Restore it rather than silently widening the window to every order
        # the branch has.  Re-raises if that wasn't the problem.
        parsed = datetime.fromisoformat(raw.replace(' ', '+'))
    if djtz.is_naive(parsed):
        parsed = djtz.make_aware(parsed)
    return parsed


@api_view(['GET', 'POST'])
@require_session
def orders_list_create(request):
    branch = request.session_obj.branch
    if request.method == 'GET':
        # prefetch the lines: the serializer nests them, so without this a
        # 500-order page issued 501 queries and the Transactions screen sat on
        # a spinner for seconds.
        # Explicit ordering: Order has no Meta.ordering, so an unordered
        # slice is undefined in Postgres — pages could repeat or skip rows,
        # and even the un-paged `[:limit]` was not guaranteed to be the newest.
        # id breaks ties so two bills in the same second can't swap places.
        qs = (
            Order.objects.filter(branch=branch)
            .select_related('branch')
            .prefetch_related('items')
            .order_by('-created_at', '-id')
        )
        source = request.query_params.get('source')
        status_ = request.query_params.get('status')
        if source and source != 'all':
            qs = qs.filter(source=source)
        if status_:
            qs = qs.filter(status=status_)

        # Date window — Transactions asks only for the bucket it is showing
        # (today / yesterday / last 7 days) rather than pulling every order the
        # branch has ever rung up.
        try:
            created_from = _parse_bound(request.query_params.get('from'))
            created_to = _parse_bound(request.query_params.get('to'))
        except ValueError:
            created_from = created_to = None
        if created_from:
            qs = qs.filter(created_at__gte=created_from)
        if created_to:
            qs = qs.filter(created_at__lt=created_to)

        # Order-number search.  This has to happen server-side: the client
        # only holds one page, so filtering there would quietly search 50 rows
        # and report "no matching orders" for a bill sitting on page 2.
        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(order_number__icontains=q)

        try:
            limit = int(request.query_params.get('limit', ORDERS_MAX_LIMIT))
        except (TypeError, ValueError):
            limit = ORDERS_MAX_LIMIT
        limit = max(1, min(limit, ORDERS_MAX_LIMIT))

        # Paging.  Without an offset the client could only ever see the newest
        # `limit` rows and had no way to reach the rest — "All" silently
        # truncated at ORDERS_MAX_LIMIT with nothing on screen to say so.
        try:
            offset = int(request.query_params.get('offset', 0))
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)

        # The total is sent as a header rather than wrapping the body, so
        # existing callers that expect a bare list keep working.
        total = qs.count()
        page = qs[offset:offset + limit]
        response = Response(OrderSerializer(page, many=True).data)
        response['X-Total-Count'] = str(total)
        return response

    # POST
    payload = dict(request.data)
    items_data = payload.pop('items', []) or []

    # A rung-up sale is finished the moment it is paid, so it lands as
    # "completed".  It used to land as "new" for the Order Hub kanban to walk
    # through Preparing → Completed, but that screen is switched off and no one
    # was using it — an order left in "new" would now have nothing to advance
    # it.  An explicit status is still honoured for any caller that wants one.
    requested_status = payload.get('status')
    initial_status = requested_status if requested_status in dict(Order.STATUS_CHOICES) else 'completed'

    order = create_order_from_items(
        branch=branch,
        items=items_data,
        payment_method=payload.get('payment_method', '') or '',
        # The client sends ``total`` as the *goods* total (subtotal − discount);
        # VAT and the card fee are recomputed server-side so they can't be
        # tampered with and stay consistent with the payment link already paid.
        goods_total=Decimal(str(payload.get('total', 0))),
        charges=None,
        subtotal=Decimal(str(payload.get('subtotal', 0))),
        discount_type=payload.get('discount_type', 'none'),
        discount_value=Decimal(str(payload.get('discount_value', 0))),
        discount_amount=Decimal(str(payload.get('discount_amount', 0))),
        paid_amount=Decimal(str(payload.get('paid_amount', 0))),
        status=initial_status,
        source=payload.get('source', 'table'),
        # Stamp the cashier who rang up the sale.  Prefer the client-sent name
        # but fall back to the authenticated session's staff so the field is
        # never empty (the void/reprint copy reads it back from the DB, not the
        # live POS session).
        staff=(
            payload.get('staff')
            or (request.session_obj.staff.name if request.session_obj.staff else '')
            or ''
        ),
        customer_id=payload.get('customer_id'),
        customer_name=payload.get('customer_name', '') or '',
        gateway_ids={
            'beam_charge_id': payload.get('beam_charge_id'),
            'beam_link_id': payload.get('beam_link_id'),
            'omise_link_id': payload.get('omise_link_id'),
            'omise_charge_id': payload.get('omise_charge_id'),
        },
        delivery_provider=payload.get('delivery_provider', '') or '',
        delivery_status=payload.get('delivery_status', '') or '',
    )
    # A cash sale just changed what is in the drawer.  Update the shift's cached
    # totals now so any reader — the Cash Drawer screen, the backoffice
    # unreconciled-cash tile — sees the sale without waiting for the round to close.
    refresh_shift_cash(order.shift)
    return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


# Buyer particulars a Thai full tax invoice (ใบกำกับภาษีเต็มรูป) cannot legally
# omit.  Everything else on the form — branch designation, phone, email — is
# optional, matching the reference POS.
TAX_INVOICE_REQUIRED = (
    ('name', 'Taxpayer or company name is required'),
    ('tax_id', 'Tax ID is required'),
    ('address', 'Address is required'),
)
TAX_INVOICE_FIELDS = (
    'name', 'tax_id', 'tax_branch', 'address', 'phone', 'email', 'customer_id',
)


@api_view(['POST'])
@require_session
def order_tax_invoice(request, order_id):
    """Record the buyer details a full tax invoice was issued to.

    Writes ``Order.pos_tax_invoice``, which a reprint replays so the cashier
    never retypes the particulars.  See that field's comment for why this does
    NOT reuse ``tax_invoice_data`` — that one belongs to the customer-facing
    Peak flow and has an incompatible schema.

    Printing happens on the device; this endpoint only persists.  It does NOT
    push to Peak — the customer-facing flow still owns that.
    """
    order = get_object_or_404(Order, id=order_id, branch=request.session_obj.branch)

    payload = request.data or {}
    data = {f: str(payload.get(f) or '').strip() for f in TAX_INVOICE_FIELDS}
    for field, message in TAX_INVOICE_REQUIRED:
        if not data[field]:
            return Response({'detail': message}, status=400)

    # Thai tax IDs are 13 digits (both juristic-person and citizen numbers).
    digits = ''.join(c for c in data['tax_id'] if c.isdigit())
    if len(digits) != 13:
        return Response({'detail': 'Tax ID must be 13 digits'}, status=400)
    data['tax_id'] = digits

    # Stamp who issued it and when, so a reissued invoice is attributable —
    # the printed slip itself carries no such trace.
    data['issued_by'] = request.session_obj.staff.name
    data['issued_at'] = djtz.now().isoformat()

    update_fields = ['pos_tax_invoice']

    # Issuing a full tax invoice names the buyer, so attach them to the bill if
    # it was rung up anonymously.  An existing customer link is left alone —
    # rewriting it would silently move the sale to a different customer's
    # history.
    customer_id = data.pop('customer_id', '')
    if customer_id and not order.customer_id:
        customer = Customer.objects.filter(
            id=customer_id, branch=request.session_obj.branch,
        ).first()
        if customer:
            order.customer = customer
            order.customer_name = customer.name
            update_fields += ['customer', 'customer_name']

    order.pos_tax_invoice = data
    order.save(update_fields=update_fields)
    return Response(OrderSerializer(order).data)


@api_view(['PUT'])
@require_session
def order_update_status(request, order_id):
    order = get_object_or_404(Order, id=order_id, branch=request.session_obj.branch)
    new_status = (request.data or {}).get('status')
    if new_status not in dict(Order.STATUS_CHOICES):
        return Response({'detail': 'Invalid status'}, status=400)
    # Only cancelled-ness decides whether this bill's money is part of the day.
    # new/preparing/completed are kitchen flow and change nothing downstream.
    void_state_changed = (order.status == 'cancel') != (new_status == 'cancel')
    order.status = new_status
    update_fields = ['status']
    # Stamp the void audit the first time a bill is cancelled so the
    # receipt detail can show "Voided by: <name>".  Leave any earlier
    # stamp untouched on a re-cancel; clear it if a bill is un-cancelled.
    if new_status == 'cancel':
        if not order.voided_at:
            order.voided_by = request.session_obj.staff.name
            order.voided_at = djtz.now()
            update_fields += ['voided_by', 'voided_at']
    elif order.voided_at:
        order.voided_by = ''
        order.voided_at = None
        update_fields += ['voided_by', 'voided_at']
    order.save(update_fields=update_fields)
    # Cancelling (or un-cancelling) a cash bill moves the drawer, so the shift's
    # cash totals have to be recomputed against the new status.
    refresh_shift_cash(order.shift)
    # Voiding (or un-voiding) changes what the branch-day sold, so a consolidated
    # receipt already filed for that day now states the wrong total.  Flag it for
    # the nightly void-and-reissue; a day not yet billed has nothing to flag.
    # Gated on an actual change of state: every flag costs a live Peak document
    # its number when the sweep replaces it, so a re-cancel must not raise one.
    if void_state_changed:
        flag_branch_day_for_reissue(order)
    return Response(OrderSerializer(order).data)


# ─── Parked orders ───────────────────────────────────────────────────────────
@api_view(['GET', 'POST'])
@require_session
def parked_orders_list_create(request):
    branch = request.session_obj.branch
    if request.method == 'GET':
        return Response(ParkedOrderSerializer(
            ParkedOrder.objects.filter(branch=branch), many=True,
        ).data)
    ser = ParkedOrderSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ser.save(branch=branch)
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@require_session
def parked_orders_delete(request, pid):
    deleted, _ = ParkedOrder.objects.filter(
        id=pid, branch=request.session_obj.branch,
    ).delete()
    if not deleted:
        return Response({'detail': 'Not found'}, status=404)
    return Response(status=204)


# ─── Stock movements ─────────────────────────────────────────────────────────
@api_view(['GET', 'POST'])
@require_session
def stock_movements(request):
    branch = request.session_obj.branch
    if request.method == 'GET':
        qs = StockMovement.objects.filter(branch=branch)
        pid = request.query_params.get('product_id')
        if pid:
            qs = qs.filter(product_id=pid)
        return Response(StockMovementSerializer(qs[:500], many=True).data)

    # POST — update product stock and snapshot the movement.
    payload = dict(request.data)
    product_id = payload.get('product_id')
    type_ = payload.get('type')
    qty = int(payload.get('qty', 0))
    note = payload.get('note', '') or ''
    if type_ not in ('in', 'out', 'adjust', 'check'):
        return Response({'detail': 'Invalid type'}, status=400)
    try:
        product = Product.objects.get(id=product_id, branch=branch)
    except Product.DoesNotExist:
        return Response({'detail': 'Product not found'}, status=404)

    if type_ == 'in':
        product.stock = (product.stock or 0) + qty
    elif type_ == 'out':
        product.stock = (product.stock or 0) - qty
    elif type_ == 'adjust':
        product.stock = qty
    # type_ == 'check' is a non-mutating audit entry
    product.save(update_fields=['stock'])

    doc_no = f"SM{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S')}"
    mv = StockMovement.objects.create(
        branch=branch,
        product=product,
        product_name=product.name,
        type=type_,
        qty=qty,
        note=note,
        document_no=doc_no,
    )
    return Response(StockMovementSerializer(mv).data, status=201)


# ─── Shifts ──────────────────────────────────────────────────────────────────
@api_view(['GET'])
@require_session
def shift_current(request):
    branch = request.session_obj.branch
    s = Shift.objects.filter(branch=branch, status='open').order_by('-opened_at').first()
    if not s:
        # DRF's JSONRenderer turns `Response(None)` into an empty body, which
        # makes the frontend's `r.json()` blow up.  Use Django's JsonResponse
        # so the wire shape is the JSON literal `null`.
        from django.http import JsonResponse
        return JsonResponse(None, safe=False)
    # Recompute before serialising.  The stored totals are a cache, and this is
    # the read the Cash Drawer screen polls, so it must never hand back a figure
    # that predates the last sale.
    refresh_shift_cash(s)
    return Response(ShiftSerializer(s).data)


@api_view(['POST'])
@require_session
def shift_open(request):
    branch = request.session_obj.branch
    if Shift.objects.filter(branch=branch, status='open').exists():
        return Response({'detail': 'Shift already open'}, status=400)
    count = Shift.objects.filter(branch=branch).count()
    s = Shift.objects.create(
        branch=branch,
        round_number=count + 1,
        start_cash=Decimal(str(request.data.get('start_cash', 0) or 0)),
        opened_by=request.data.get('opened_by', 'Admin') or 'Admin',
    )
    return Response(ShiftSerializer(s).data, status=201)


@api_view(['POST'])
@require_session
def shift_movement(request):
    branch = request.session_obj.branch
    s = Shift.objects.filter(branch=branch, status='open').first()
    if not s:
        return Response({'detail': 'No open shift'}, status=400)
    type_ = request.data.get('type')
    amount = Decimal(str(request.data.get('amount', 0) or 0))
    if type_ not in ('paid_in', 'paid_out'):
        return Response({'detail': 'Invalid type'}, status=400)
    mv = ShiftMovement.objects.create(
        shift=s, type=type_, amount=amount,
        category=request.data.get('category', '') or '',
        note=request.data.get('note', '') or '',
    )
    if type_ == 'paid_in':
        s.total_paid_in = (s.total_paid_in or 0) + amount
    else:
        s.total_paid_out = (s.total_paid_out or 0) + amount
    s.save(update_fields=['total_paid_in', 'total_paid_out'])
    # Paid in/out moves the drawer, so the expected figure has to follow it.
    refresh_shift_cash(s)
    return Response(ShiftMovementSerializer(mv).data, status=201)


@api_view(['PUT'])
@require_session
def shift_close(request):
    branch = request.session_obj.branch
    s = Shift.objects.filter(branch=branch, status='open').first()
    if not s:
        return Response({'detail': 'No open shift'}, status=400)
    s.status = 'closed'
    s.closed_at = djtz.now()
    s.closed_by = request.data.get('closed_by', 'Admin') or 'Admin'
    s.actual_in_drawer = Decimal(str(request.data.get('actual_in_drawer', 0) or 0))
    s.save()
    # Stamp the final cash figures *after* closed_at exists, so the window
    # fallback in ``shift_orders`` is bounded by the real close time.
    refresh_shift_cash(s)
    return Response({**ShiftSerializer(s).data, 'summary': _shift_summary(s)})


# Cash-equivalent payment methods — money that actually lands in the drawer.
CASH_METHODS = ['Cash', 'Easy Pay']


def shift_cash_sales(shift):
    """Cash that this round's sales put in the drawer.

    Excludes cancelled bills — a voided cash sale is money handed back, so
    counting it would overstate the drawer.  This is the same order set the
    printed summary's ``cash_sales`` line uses; they must agree or the slip
    doesn't add up.
    """
    from django.db.models import Sum
    return (
        shift_orders(shift)
        .exclude(status='cancel')
        .filter(payment_method__in=CASH_METHODS)
        .aggregate(t=Sum('total'))['t'] or Decimal('0')
    )


def refresh_shift_cash(shift):
    """Recompute and persist ``total_sales_cash`` / ``expected_in_drawer``.

    Both columns are a *cache* of an aggregate over orders and movements.  They
    used to be written only by ``shift_close``, so for the whole life of an open
    round they read back as their 0 defaults: the Cash Drawer screen showed
    "Total Sales (cash) 0.00" no matter how many bills were rung up, and the
    backoffice's unreconciled-cash figure was always ฿0.  Call this from every
    path that moves drawer cash so the stored numbers stay true between opens
    and closes.
    """
    if shift is None:
        return None
    cash_total = shift_cash_sales(shift)
    shift.total_sales_cash = cash_total
    shift.expected_in_drawer = (
        (shift.start_cash or 0) + cash_total
        + (shift.total_paid_in or 0) - (shift.total_paid_out or 0)
    )
    shift.save(update_fields=['total_sales_cash', 'expected_in_drawer'])
    return shift


def shift_orders(shift):
    """Every Order belonging to ``shift``.

    Prefers the ``shift`` FK, which is stamped at creation.  The old
    time-window-only query silently dropped any order that landed *outside*
    opened_at..closed_at — which a self-order can, because it is confirmed when
    the gateway says so, not when the cashier is looking: pay at 22:01, close
    the round at 22:02, confirm at 22:03, and the sale appeared in no round at all.

    Orders created before the FK existed have ``shift IS NULL``, so they still
    fall back to the window and historical summaries don't change.
    """
    window = Q(shift__isnull=True, created_at__gte=shift.opened_at)
    if shift.closed_at:
        window &= Q(created_at__lte=shift.closed_at)
    return Order.objects.filter(
        Q(branch=shift.branch) & (Q(shift=shift) | window)
    )


def _shift_summary(shift):
    """Aggregate everything the close-shift slip (ใบสรุปปิดรอบการขาย) prints.

    Returns plain JSON-able types (strings/floats) so it can be embedded in a
    Response and rendered straight onto the printed image.
    """
    from django.db.models import Count, Sum

    orders = shift_orders(shift)

    sold = orders.exclude(status='cancel')
    cancelled = orders.filter(status='cancel')

    def _money(v):
        return float(v or 0)

    nums = list(sold.order_by('created_at').values_list('order_number', flat=True))
    sales_total = sold.aggregate(t=Sum('total'))['t'] or Decimal('0')
    cash_sales = (
        sold.filter(payment_method__in=CASH_METHODS).aggregate(t=Sum('total'))['t']
        or Decimal('0')
    )

    payments = [
        {
            'method': p['payment_method'] or 'Cash',
            'amount': _money(p['amt']),
            'count': p['n'],
        }
        for p in sold.values('payment_method').annotate(amt=Sum('total'), n=Count('id')).order_by('-amt')
    ]

    def _movements(kind):
        return [
            {'category': m.category, 'note': m.note, 'amount': _money(m.amount)}
            for m in shift.movements.filter(type=kind).order_by('created_at')
        ]

    actual = shift.actual_in_drawer if shift.actual_in_drawer is not None else Decimal('0')

    return {
        'round_number': shift.round_number,
        'opened_at': shift.opened_at.isoformat() if shift.opened_at else None,
        'opened_by': shift.opened_by,
        'closed_at': shift.closed_at.isoformat() if shift.closed_at else None,
        'closed_by': shift.closed_by,
        'invoice_first': nums[0] if nums else '',
        'invoice_last': nums[-1] if nums else '',
        'bill_count': len(nums),
        # Cash drawer
        'start_cash': _money(shift.start_cash),
        'cash_sales': _money(cash_sales),
        'paid_in': _money(shift.total_paid_in),
        'paid_out': _money(shift.total_paid_out),
        'actual_in_drawer': _money(actual),
        'expected_in_drawer': _money(shift.expected_in_drawer),
        'difference': _money(actual - (shift.expected_in_drawer or 0)),
        'paid_in_items': _movements('paid_in'),
        'paid_out_items': _movements('paid_out'),
        # Payment + sales summary
        'payments': payments,
        'sales_total': _money(sales_total),
        'discount_total': _money(sold.aggregate(t=Sum('discount_amount'))['t'] or Decimal('0')),
        'cancelled_total': _money(cancelled.aggregate(t=Sum('total'))['t'] or Decimal('0')),
        'cancelled_count': cancelled.count(),
    }


@api_view(['GET'])
@require_session
def shift_summary(request, shift_id):
    """Re-fetch a (usually closed) shift's printable summary for reprint."""
    s = get_object_or_404(Shift, id=shift_id, branch=request.session_obj.branch)
    return Response(_shift_summary(s))


@api_view(['GET'])
@require_session
def shifts_list(request):
    return Response(ShiftSerializer(
        Shift.objects.filter(branch=request.session_obj.branch)[:100],
        many=True,
    ).data)


# ─── Customer stats ──────────────────────────────────────────────────────────
@api_view(['GET'])
@require_session
def customer_stats(request, customer_id):
    branch = request.session_obj.branch
    try:
        Customer.objects.get(id=customer_id, branch=branch)
    except Customer.DoesNotExist:
        return Response({'detail': 'Customer not found'}, status=404)

    orders = list(
        Order.objects
        .filter(branch=branch, customer_id=customer_id)
        .prefetch_related('items')
    )
    completed = [o for o in orders if o.status == 'completed']
    outstanding = [o for o in orders if o.status not in ('completed', 'cancel')]

    success_total = sum((o.total or 0) for o in completed)
    bill_count = len(completed)
    avg_bill = (success_total / bill_count) if bill_count else 0
    outstanding_total = sum((o.total or 0) for o in outstanding)

    prod_totals: dict = {}
    cat_totals: dict = {}
    for o in completed:
        for item in o.items.all():
            key = str(item.product_id) if item.product_id else item.name
            entry = prod_totals.setdefault(
                key,
                {'product_id': str(item.product_id) if item.product_id else None,
                 'name': item.name, 'total': 0, 'qty': 0},
            )
            line_total = float(item.price) * item.qty
            entry['total'] += line_total
            entry['qty'] += item.qty
            cname = item.category_name or 'Other'
            cat_totals[cname] = cat_totals.get(cname, 0) + line_total

    top_products = sorted(prod_totals.values(), key=lambda x: -x['total'])[:5]
    top_categories = [
        {'name': k, 'total': v}
        for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])[:5]
    ]
    return Response({
        'customer_id': str(customer_id),
        'success_total': float(success_total),
        'bill_count': bill_count,
        'avg_bill': float(avg_bill),
        'outstanding_total': float(outstanding_total),
        'outstanding_count': len(outstanding),
        'top_products': top_products,
        'top_categories': top_categories,
    })


# ─── Dashboard ───────────────────────────────────────────────────────────────
@api_view(['GET'])
@require_session
def dashboard(request):
    branch = request.session_obj.branch
    period = request.query_params.get('period', 'month')
    now = djtz.now()
    start, end = _period_range(
        period, now,
        request.query_params.get('start', ''),
        request.query_params.get('end', ''),
    )

    oq = Order.objects.filter(branch=branch, created_at__gte=start)
    if end:
        oq = oq.filter(created_at__lt=end)
    orders = list(oq.exclude(status='cancel').prefetch_related('items'))

    total_sales = sum(float(o.total or 0) for o in orders)
    tx_count = len(orders)
    avg_bill = (total_sales / tx_count) if tx_count else 0

    product_ids = {item.product_id for o in orders for item in o.items.all() if item.product_id}
    products = {
        str(p.id): p for p in
        Product.objects.filter(id__in=product_ids, branch=branch).only('id', 'cost', 'category_id')
    }

    cost_total = 0.0
    prod_totals: dict = {}
    cat_totals: dict = {}
    buckets: dict = {}
    for o in orders:
        bucket_key = o.created_at.date().isoformat()
        buckets[bucket_key] = buckets.get(bucket_key, 0) + float(o.total or 0)
        for item in o.items.all():
            line_total = float(item.price) * item.qty
            p = products.get(str(item.product_id)) if item.product_id else None
            if p:
                cost_total += float(p.cost or 0) * item.qty
            entry = prod_totals.setdefault(
                str(item.product_id) if item.product_id else item.name,
                {'product_id': str(item.product_id) if item.product_id else None,
                 'name': item.name, 'total': 0, 'qty': 0},
            )
            entry['total'] += line_total
            entry['qty'] += item.qty
            cname = item.category_name or 'Other'
            cat_totals[cname] = cat_totals.get(cname, 0) + line_total

    profit = total_sales - cost_total
    timeline = [{'label': k, 'value': v} for k, v in sorted(buckets.items())[-7:]]
    top_products = sorted(prod_totals.values(), key=lambda x: -x['total'])[:5]
    top_categories = [
        {'name': k, 'total': v}
        for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])[:5]
    ]
    return Response({
        'period': period,
        'total_sales': total_sales,
        'cost': cost_total,
        'profit': profit,
        'gp_percent': (profit / total_sales * 100) if total_sales else 0,
        'tx_count': tx_count,
        'avg_bill': avg_bill,
        'timeline': timeline,
        'top_products': top_products,
        'top_categories': top_categories,
    })


def _period_range(period, now, start_q='', end_q=''):
    """Return ``(start_dt, end_dt_or_None)`` for a dashboard period.

    ``period='custom'`` uses the ``YYYY-MM-DD`` strings ``start_q`` / ``end_q``
    (end is inclusive of the whole day).  Any other period is an open-ended
    range starting at the preset boundary."""
    if period == 'custom' and start_q:
        try:
            s = djtz.make_aware(datetime.strptime(start_q, '%Y-%m-%d'))
        except (ValueError, TypeError):
            return now - timedelta(days=30), None
        end_dt = None
        if end_q:
            try:
                end_dt = djtz.make_aware(datetime.strptime(end_q, '%Y-%m-%d')) + timedelta(days=1)
            except (ValueError, TypeError):
                end_dt = None
        return s, end_dt
    if period == 'today':
        return now.replace(hour=0, minute=0, second=0, microsecond=0), None
    if period == 'week':
        return now - timedelta(days=7), None
    if period == 'year':
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), None
    return now - timedelta(days=30), None


# ─── Sales channel report ────────────────────────────────────────────────────
CHANNEL_LABELS = {
    'table': 'Store',
    'delivery': 'Delivery',
    'kiosk': 'KIOSK',
    'other': 'Other',
}


@api_view(['GET'])
@require_session
def dashboard_channels(request):
    """Sales grouped by order source — one row per channel with the
    before-GP / GP / after-GP breakdown.  GP (a per-channel gross-profit
    deduction) is not configured per channel yet, so every channel reports
    ``has_gp: false`` → "No GP" and before == after, matching SilomPOS."""
    branch = request.session_obj.branch
    period = request.query_params.get('period', 'today')
    now = djtz.now()
    start, end = _period_range(
        period, now,
        request.query_params.get('start', ''),
        request.query_params.get('end', ''),
    )

    oq = Order.objects.filter(branch=branch, created_at__gte=start)
    if end:
        oq = oq.filter(created_at__lt=end)
    orders = oq.exclude(status='cancel').only('source', 'delivery_provider', 'total')

    rows: dict = {}
    for o in orders:
        provider = (o.delivery_provider or '').strip()
        if o.source == 'delivery' and provider:
            key, label = f'delivery:{provider}', provider
        else:
            key = o.source or 'other'
            label = CHANNEL_LABELS.get(o.source, 'Other')
        entry = rows.setdefault(
            key, {'channel': label, 'source': o.source, 'count': 0, 'before_gp': 0.0},
        )
        entry['count'] += 1
        entry['before_gp'] += float(o.total or 0)

    channels = []
    for r in sorted(rows.values(), key=lambda x: -x['before_gp']):
        gp = 0.0  # no per-channel GP deduction configured
        channels.append({
            'channel': r['channel'],
            'source': r['source'],
            'count': r['count'],
            'before_gp': r['before_gp'],
            'gp': gp,
            'after_gp': r['before_gp'] - gp,
            'has_gp': False,
        })

    return Response({
        'period': period,
        'channels': channels,
        'total_before_gp': sum(c['before_gp'] for c in channels),
        'total_gp': sum(c['gp'] for c in channels),
        'total_after_gp': sum(c['after_gp'] for c in channels),
        'total_count': sum(c['count'] for c in channels),
    })


# ─── Stock documents (multi-line stock-in / -out / adjust / check) ───────────
def _random_doc_suffix(n: int = 8) -> str:
    import secrets
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789'
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def _next_stock_doc_no(branch, doc_type, adjust_type='') -> str:
    """Document number matching SilomPOS conventions:
      • in/out  → ``#YYMMDD-XXXXXXXX`` (date prefix + random)
      • adjust  → ``A+0000001`` / ``A-0000001`` (sign + running count)
      • check   → ``CK0000001``
    """
    if doc_type in ('in', 'out'):
        return f"#{datetime.now(timezone.utc).strftime('%y%m%d')}-{_random_doc_suffix()}"
    seq = StockDocument.objects.filter(branch=branch, type=doc_type).count() + 1
    if doc_type == 'adjust':
        prefix = adjust_type if adjust_type in ('A+', 'A-') else 'A+'
        return f"{prefix}{seq:07d}"
    return f"CK{seq:07d}"


@api_view(['GET', 'POST'])
@require_session
def stock_documents(request):
    branch = request.session_obj.branch

    if request.method == 'GET':
        qs = StockDocument.objects.filter(branch=branch).prefetch_related('items')
        doc_type = request.query_params.get('type')
        if doc_type in ('in', 'out', 'adjust', 'check'):
            qs = qs.filter(type=doc_type)
        start = request.query_params.get('start')
        end = request.query_params.get('end')
        if start:
            qs = qs.filter(created_at__date__gte=start)
        if end:
            qs = qs.filter(created_at__date__lte=end)
        return Response(StockDocumentSerializer(qs[:500], many=True).data)

    # POST — create the document, snapshot lines, and apply stock deltas.
    payload = dict(request.data)
    doc_type = payload.get('type')
    if doc_type not in ('in', 'out', 'adjust', 'check'):
        return Response({'detail': 'Invalid type'}, status=400)
    items = payload.get('items') or []
    if not items:
        return Response({'detail': 'At least one item is required'}, status=400)

    def _dec(v):
        return Decimal(str(v if v not in (None, '') else 0))

    is_reconcile = doc_type in ('adjust', 'check')
    # For adjust, the "Update" delta per line (qty) reconciles on-hand to the
    # counted value; the document's A+/A- type is the sign of the net delta.
    net_delta = sum(_dec(l.get('qty')) for l in items)
    if doc_type == 'adjust':
        adjust_type = 'A+' if net_delta >= 0 else 'A-'
    else:
        adjust_type = payload.get('adjust_type', '') or ''
    staff_name = getattr(request.session_obj.staff, 'name', '') or ''
    doc_no = _next_stock_doc_no(branch, doc_type, adjust_type)

    with transaction.atomic():
        doc = StockDocument.objects.create(
            branch=branch,
            type=doc_type,
            document_no=doc_no,
            document_name=payload.get('document_name', '') or '',
            adjust_type=adjust_type if doc_type == 'adjust' else '',
            ref_no=payload.get('ref_no', '') or '',
            vendor=payload.get('vendor', '') or '',
            receiver=payload.get('receiver', '') or '',
            # Stock-out only.  Snapshotted as text, not an FK, so renaming or
            # deleting the reason later can't rewrite a saved document.
            reason=(payload.get('reason', '') or '') if doc_type == 'out' else '',
            note=payload.get('note', '') or '',
            tax_included=bool(payload.get('tax_included', False)),
            avg_cost=bool(payload.get('avg_cost', False)),
            subtotal=_dec(payload.get('subtotal')),
            discount=_dec(payload.get('discount')),
            tax=_dec(payload.get('tax')),
            total=_dec(payload.get('total')),
            created_by=staff_name,
        )
        for line in items:
            pid = line.get('product_id')
            product = None
            if pid:
                product = Product.objects.filter(id=pid, branch=branch).first()
            qty = _dec(line.get('qty'))           # in/out qty OR adjust update-delta
            reconcile = _dec(line.get('reconcile_qty'))
            before = _dec(line.get('before_qty'))
            StockDocumentItem.objects.create(
                document=doc,
                product=product,
                barcode=line.get('barcode', '') or (product.barcode if product else ''),
                product_name=line.get('product_name', '') or (product.name if product else ''),
                qty=qty,
                price=_dec(line.get('price')),
                discount=_dec(line.get('discount')),
                total=_dec(line.get('total')),
                before_qty=before,
                reconcile_qty=reconcile,
            )
            # Apply on-hand change per line.
            #   in   → +qty,  out → -qty
            #   adjust → set stock to the counted (reconcile) value
            #   check  → non-mutating audit (records the count, leaves stock)
            if product and doc_type != 'check':
                if doc_type == 'in':
                    product.stock = (product.stock or 0) + int(qty)
                elif doc_type == 'out':
                    product.stock = (product.stock or 0) - int(qty)
                else:  # adjust → reconcile to the counted value
                    product.stock = int(reconcile)
                product.save(update_fields=['stock'])
            if product and (qty or is_reconcile):
                StockMovement.objects.create(
                    branch=branch,
                    product=product,
                    product_name=product.name,
                    type=doc_type,
                    qty=int(qty),
                    # Reason first for a stock-out: it is now the structured
                    # field, and the movement ledger is what the Inventory
                    # screen shows when you tap a product's history.
                    note=doc.reason or doc.note or doc.document_name,
                    document_no=doc_no,
                )

    return Response(StockDocumentSerializer(doc).data, status=201)


@api_view(['GET'])
@require_session
def stock_document_detail(request, doc_id):
    branch = request.session_obj.branch
    doc = StockDocument.objects.filter(id=doc_id, branch=branch).prefetch_related('items').first()
    if not doc:
        return Response({'detail': 'Not found'}, status=404)
    return Response(StockDocumentSerializer(doc).data)


# ─── Payments (Beam + Omise) ─────────────────────────────────────────────────
# Thin HTTP wrappers.  All gateway logic — credentials, the HTTP calls, the
# VAT/fee math — lives in ``gateways.py`` so the public self-ordering views can
# reuse it without a session.  These endpoints exist for the POS (a cashier at
# the till) and stay @require_session.
#
# The two status vocabularies below are NOT accidental and must not be
# "harmonised": ``beam_charge_status`` returns Beam's raw string (SUCCEEDED /
# COMPLETED) while ``beam_link_status`` returns a normalised one (successful /
# failed / pending).  pos.tsx hard-codes both; changing either breaks checkout.
def _gateway_response(fn, *args, **kwargs):
    """Run a gateway call, mapping its exceptions onto DRF responses."""
    try:
        return fn(*args, **kwargs), None
    except GatewayConfigError as e:
        return None, Response({'detail': str(e)}, status=400)
    except GatewayError as e:
        return None, Response({'detail': e.detail}, status=e.status)


@api_view(['POST'])
@require_session
def beam_charge_create(request):
    reference_id = request.data.get('reference_id') or request.data.get('reference', '')
    res, err = _gateway_response(
        gateways.beam_create_promptpay_charge,
        amount=Decimal(str(request.data.get('amount', 0) or 0)),
        reference_id=reference_id,
        description=request.data.get('description') or '',
        # Route into this branch's own Beam account if it has one; falls back to
        # the shop account otherwise, so single-account shops are unaffected.
        branch=request.session_obj.branch,
    )
    if err:
        return err
    return Response({
        'charge_id': res['charge_id'],
        'status': res['status'],
        'qr_image': res['qr_image'],
        'qr_string': res['qr_string'],
        'amount': res['amount'],
        'currency': res['currency'],
    })


@api_view(['GET'])
@require_session
def beam_charge_status(request, charge_id):
    res, err = _gateway_response(gateways.beam_get_charge, charge_id, branch=request.session_obj.branch)
    if err:
        return err
    return Response({
        'charge_id': res['charge_id'],
        'status': res['status'],
        'amount': res['amount'],
        'currency': res['currency'],
    })


@api_view(['POST'])
@require_session
def beam_link_create(request):
    """Create a Beam card payment Link and return its hosted-checkout URL.

    ``amount`` is the *goods* total; the backend adds the card processing fee +
    fee VAT so the customer is charged the grand total.  The POS renders
    ``payment_uri`` as a QR the customer scans to pay.
    """
    reference_id = request.data.get('reference_id') or request.data.get('reference', '')
    res, err = _gateway_response(
        gateways.beam_create_card_link,
        goods_total=Decimal(str(request.data.get('amount', 0) or 0)),
        reference_id=reference_id,
        description=request.data.get('description') or '',
        # Cashier flow: the customer pays on a device at the counter and the
        # cashier is watching the POS, so bouncing the browser at the POS host
        # is fine.  Self-ordering overrides this with the customer's own
        # status page.
        redirect_url=None,
        branch=request.session_obj.branch,
    )
    if err:
        return err
    return Response({
        'link_id': res['link_id'],
        'payment_uri': res['payment_uri'],
        'goods_total': res['goods_total'],
        'vat_amount': res['vat_amount'],
        'processing_fee': res['processing_fee'],
        'processing_fee_vat': res['processing_fee_vat'],
        'amount_total': res['amount_total'],
        'currency': res['currency'],
    })


@api_view(['GET'])
@require_session
def beam_link_status(request, link_id):
    res, err = _gateway_response(gateways.beam_get_link, link_id, branch=request.session_obj.branch)
    if err:
        return err
    return Response({
        'link_id': res['link_id'],
        'status': res['status'],
        'charge_id': res['charge_id'],
    })


@api_view(['POST'])
@require_session
def omise_link_create(request):
    """Create an Omise payment Link for a card charge and return its hosted URL."""
    res, err = _gateway_response(
        gateways.omise_create_link,
        goods_total=Decimal(str(request.data.get('amount', 0) or 0)),
        title=request.data.get('title') or '',
        description=request.data.get('description') or '',
        branch=request.session_obj.branch,
    )
    if err:
        return err
    return Response({
        'link_id': res['link_id'],
        'payment_uri': res['payment_uri'],
        'goods_total': res['goods_total'],
        'vat_amount': res['vat_amount'],
        'processing_fee': res['processing_fee'],
        'processing_fee_vat': res['processing_fee_vat'],
        'amount_total': res['amount_total'],
        'currency': res['currency'],
    })


@api_view(['GET'])
@require_session
def omise_link_status(request, link_id):
    res, err = _gateway_response(gateways.omise_get_link, link_id, branch=request.session_obj.branch)
    if err:
        return err
    return Response({
        'link_id': res['link_id'],
        'status': res['status'],
        'charge_id': res['charge_id'],
    })


# ─── Seed (demo data) ────────────────────────────────────────────────────────
@api_view(['POST'])
def seed_data(_request):
    """Wipe + reseed the bravepos_* tables (demo/dev only).

    DANGER: this DELETES every product, order, shift, staff and branch row.
    It is public (no auth), so it is gated behind an explicit env flag —
    otherwise anything that can reach this server (e.g. a LAN-exposed dev box
    pointed at the production DB) could erase the whole shop.  Set
    ``BRAVEPOS_ALLOW_SEED=1`` in the environment to enable it deliberately.
    """
    import os
    if os.environ.get('BRAVEPOS_ALLOW_SEED') != '1':
        return Response(
            {'detail': 'Seeding is disabled. Set BRAVEPOS_ALLOW_SEED=1 to enable.'},
            status=403,
        )
    # Order matters because of FKs — children first.
    OrderItem.objects.all().delete()
    Order.objects.all().delete()
    ParkedOrder.objects.all().delete()
    StockMovement.objects.all().delete()
    ShiftMovement.objects.all().delete()
    Shift.objects.all().delete()
    Product.objects.all().delete()
    Customer.objects.all().delete()
    Category.objects.all().delete()
    BranchSession.objects.all().delete()
    Staff.objects.all().delete()
    Branch.objects.all().delete()

    # Seed default branch
    emq = Branch.objects.create(
        name="EmQuartier",
        code="EMQ",
        address="EmQuartier, Sukhumvit 39, Bangkok",
        phone="0644184887",
        tax_id="0105563083534",
        pos_id="E020140003A0087",
        active=True,
    )

    # Seed demo staff — admin can log into any branch, cashier only to EmQuartier.
    # PINs match the legacy /auth/verify-pin defaults so tutorials still work.
    admin = Staff(email="admin@rollingpinn.com", name="Admin", role="admin")
    admin.set_password("admin1234")
    admin.set_pin("1234")
    admin.save()

    cashier = Staff(email="cashier@rollingpinn.com", name="Cashier", role="cashier")
    cashier.set_password("cashier1234")
    cashier.set_pin("0000")
    cashier.save()
    cashier.branches.add(emq)

    cats_data = [
        ("Favorite", "รายการโปรด", "#00B14F", 0, ""),
        ("Valentine's Collection", "วาเลนไทน์", "#EC4899", 1, "Grabfood"),
        ("Hot Promotion!", "โปรโมชั่น", "#F59E0B", 2, "Grabfood"),
        ("Christmas Collection", "คริสต์มาส", "#EF4444", 3, "Grabfood"),
        ("Cake Slices", "เค้กชิ้น", "#94A3B8", 4, "Grabfood"),
        ("Choco Gems", "ช็อกโกเจม", "#00B14F", 5, "Grabfood"),
        ("Small Cookies", "คุกกี้เล็ก", "#00B14F", 6, ""),
        ("Cookie Cake", "คุกกี้เค้ก", "#00B14F", 7, "Grabfood"),
        ("Brownie Bites", "บราวนี่", "#7C2D12", 10, ""),
        ("Dubai Chocolate", "ดูไบช็อกโกแลต", "#92400E", 12, "Grabfood"),
    ]
    cats = {
        name: Category.objects.create(
            branch=emq,
            name=name, name_th=name_th, color=color, order=order, source=source,
        )
        for (name, name_th, color, order, source) in cats_data
    }

    IMG_GEMS = "https://images.pexels.com/photos/9419469/pexels-photo-9419469.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"
    IMG_COOKIE = "https://images.pexels.com/photos/36500580/pexels-photo-36500580.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"
    IMG_BROWNIE = "https://images.pexels.com/photos/45202/brownie-dessert-cake-sweet-45202.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940"
    IMG_CAKE = "https://images.unsplash.com/photo-1694588915262-30d22a36b379?crop=entropy&cs=srgb&fm=jpg&w=600"

    products = [
        ("Chocogems pop Baby edition", "ช็อกโกเจมป๊อปเบบี้", 350, 180, "Choco Gems", IMG_GEMS, True, 24),
        ("Choco Gems Pop", "ช็อกโกเจมป๊อป", 299, 150, "Choco Gems", IMG_GEMS, True, 32),
        ("Mayongchid Choco Gems Pop", "มะยงชิดช็อกโกเจม", 350, 180, "Choco Gems", IMG_GEMS, True, 18),
        ("Mama OG Dark Chocolate Walnut Cookie", "คุกกี้ดาร์ก", 95, 40, "Small Cookies", IMG_COOKIE, False, 0),
        ("The Marching Ladies Cookie", "มาร์ชิ่งเลดี้ส์", 95, 40, "Small Cookies", IMG_COOKIE, False, -59),
        ("Sexy Back Cookie", "เซ็กซี่แบ็กคุกกี้", 95, 40, "Small Cookies", IMG_COOKIE, True, -32),
        ("Pink Birthday Cookie Cake (1lb)", "พิงค์ เบิร์ธเดย์", 590, 280, "Cookie Cake", IMG_CAKE, True, 6),
        ("Mini Strawberry Shortcake", "มินิสตรอเบอร์รี่", 690, 320, "Cookie Cake", IMG_CAKE, False, 3),
        ("Red Velvet Cookie Cake Slice", "เรดเวลเว็ทชิ้น", 160, 70, "Cake Slices", IMG_CAKE, False, 12),
        ("Classic Brownie Bite", "บราวนี่คลาสสิก", 45, 18, "Brownie Bites", IMG_BROWNIE, False, 50),
        ("Salted Caramel Brownie", "ซอลเทดคาราเมล", 55, 22, "Brownie Bites", IMG_BROWNIE, False, 36),
        ("Box of 9pcs Bae Brownie", "แบบราวนี่ 9 ชิ้น", 380, 180, "Hot Promotion!", IMG_BROWNIE, False, 10),
        ("Strawberry Love Cake", "เค้กความรัก", 590, 280, "Valentine's Collection", IMG_CAKE, False, 6),
        ("Dubai Chewy Cookies", "ดูไบชิววี่", 299, 149, "Dubai Chocolate", IMG_COOKIE, False, 22),
        ("Crystal Velvet Tanghulu Cookie", "คริสตัลเวลเว็ท", 160, 70, "Christmas Collection", IMG_COOKIE, False, 18),
    ]
    for (name, name_th, price, cost, cat_name, img, fav, stock) in products:
        Product.objects.create(
            branch=emq,
            name=name, name_th=name_th,
            price=Decimal(price), cost=Decimal(cost),
            category=cats.get(cat_name),
            image_url=img, is_favorite=fav, stock=stock,
        )

    return Response({
        'ok': True,
        'categories': Category.objects.count(),
        'products': Product.objects.count(),
    })


# ─── Printer (stubs — receipt rendering is ported in a follow-up step) ───────
@api_view(['GET'])
def printer_detect(_request):
    """Detect attached usblp printers on the host."""
    import glob, os
    paths = sorted(glob.glob('/dev/usb/lp*'))
    return Response({
        'candidates': [{'path': p, 'writable': os.access(p, os.W_OK)} for p in paths],
    })


@api_view(['GET'])
def printer_status(_request):
    """Mirror of the FastAPI endpoint."""
    s = _get_or_create_settings()
    base = {
        'enabled': s.printer_enabled,
        'transport': s.printer_transport,
        'address': s.printer_address,
        'paper_width': s.printer_paper_width,
    }
    if s.printer_transport == 'disabled' or not s.printer_enabled:
        return Response({**base, 'connected': False, 'status': 'disabled'})

    if s.printer_transport == 'file':
        import os
        path = s.printer_address or '/dev/usb/lp0'
        if not os.path.exists(path):
            return Response({**base, 'connected': False, 'status': 'offline',
                             'error': f'Device {path} not found'})
        if not os.access(path, os.W_OK):
            return Response({**base, 'connected': False, 'status': 'offline',
                             'error': f'Device {path} not writable (permission?)'})
        return Response({**base, 'connected': True, 'status': 'connected'})

    if s.printer_transport == 'network':
        if not s.printer_address:
            return Response({**base, 'connected': False, 'status': 'offline',
                             'error': 'No address configured'})
        host, _, port_s = s.printer_address.partition(':')
        port = int(port_s) if port_s else 9100
        import socket
        try:
            with socket.create_connection((host, port), timeout=2.0):
                pass
            return Response({**base, 'connected': True, 'status': 'connected'})
        except Exception as e:
            return Response({**base, 'connected': False, 'status': 'offline',
                             'error': f'{type(e).__name__}: {e}'})

    return Response({**base, 'connected': False, 'status': 'offline',
                     'error': f'Unknown transport: {s.printer_transport}'})


@api_view(['POST'])
def print_test(_request):
    """Renders a sample receipt and dispatches.  Uses the FastAPI printer.py
    module unchanged — it's transport-agnostic Python."""
    s = _get_or_create_settings()
    if s.printer_transport == 'disabled':
        return Response(
            {'detail': "Set printer_transport to 'file' or 'network' in Settings first."},
            status=400,
        )
    try:
        from backend import printer as printer_mod  # noqa: WPS433  legacy module
    except Exception:
        # Fall back to the in-repo printer.py shipped with FastAPI.
        import importlib.util
        from pathlib import Path
        p = Path(django_settings.BASE_DIR).parent / 'backend' / 'printer.py'
        spec = importlib.util.spec_from_file_location('printer_mod', p)
        printer_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(printer_mod)
    fake_order = {
        'order_number': 'PS999000001',
        'items': [{'name': 'Test Item — ทดสอบ', 'qty': 1, 'price': 100.0}],
        'subtotal': 100.0, 'total': 100.0,
        'payment_method': 'Test', 'paid_amount': 100.0, 'change': 0,
        'created_at_local': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'staff': 'TEST',
    }
    settings_dict = SettingsSerializer(s).data
    settings_dict['printer_enabled'] = True   # force-enable for the test
    try:
        printer_mod.print_receipt(fake_order, dict(settings_dict))
    except printer_mod.PrinterError as e:
        return Response({'detail': str(e)}, status=502)
    return Response({'ok': True})


@api_view(['POST'])
@require_session
def print_receipt(request, order_id):
    """Manually re-print a previously saved order."""
    try:
        order = (
            Order.objects
            .prefetch_related('items')
            .get(id=order_id, branch=request.session_obj.branch)
        )
    except Order.DoesNotExist:
        return Response({'detail': 'Order not found'}, status=404)
    s = _get_or_create_settings()
    if not s.printer_enabled:
        return Response(
            {'detail': 'Printer is disabled. Enable it in Settings.'},
            status=400,
        )
    import importlib.util
    from pathlib import Path
    p = Path(django_settings.BASE_DIR).parent / 'backend' / 'printer.py'
    spec = importlib.util.spec_from_file_location('printer_mod', p)
    printer_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(printer_mod)
    order_dict = OrderSerializer(order).data
    order_dict['created_at_local'] = order.created_at.astimezone().strftime('%d/%m/%Y %H:%M')
    try:
        printer_mod.print_receipt(dict(order_dict), dict(SettingsSerializer(s).data))
    except printer_mod.PrinterError as e:
        return Response({'detail': str(e)}, status=502)
    return Response({'ok': True})


# ─── Self-orders (POS side) ──────────────────────────────────────────────────
# The receipt for a self-order prints on the POS tablet, because printing is
# client-side (the Epson native module lives in the Expo app; the server-side
# print path here is a vestigial stub).  So the tablet has to *ask* for work.
#
# A branch can have several tablets, and printerQueue's in-flight Set only
# dedupes within one JS runtime — so two tablets polling this endpoint would
# both be handed the same order and both print it.  The claim is therefore
# server-side: a tablet wins a row with a conditional UPDATE and only prints
# what it won.

# How long a tablet's claim is honoured before another may steal it.  Covers a
# tablet that fetched a job then died (crash, battery, Wi-Fi) — without a lease
# that receipt would never print.
PRINT_CLAIM_LEASE_S = 90


@api_view(['GET'])
@require_session
def self_orders_pending(request):
    """Paid self-orders this tablet should print.

    Claims each row it returns.  Claiming *is* the acknowledgement — we
    deliberately do not wait for the tablet to confirm the paper came out.
    Acking after the fact would mean a dropped connection re-serves the row and
    prints a **second receipt with the same queue number**, which is a dispute
    with a customer standing at the counter.  A missing receipt is recoverable
    (Reprint already exists in admin); a duplicate is not.
    """
    branch = request.session_obj.branch
    now = djtz.now()
    lease_cutoff = now - timedelta(seconds=PRINT_CLAIM_LEASE_S)
    claimant = str(request.session_obj.id)

    claimable = (
        SelfOrder.objects
        .filter(branch=branch, status='paid', printed_at__isnull=True)
        .filter(Q(print_claimed_at__isnull=True) | Q(print_claimed_at__lt=lease_cutoff))
        .values_list('id', flat=True)
    )

    claimed_ids = []
    for so_id in list(claimable):
        # Conditional UPDATE: only one caller can flip a given row, so only one
        # tablet is ever told to print it.  rowcount is the arbiter.
        won = (
            SelfOrder.objects
            .filter(pk=so_id, status='paid', printed_at__isnull=True)
            .filter(Q(print_claimed_at__isnull=True) | Q(print_claimed_at__lt=lease_cutoff))
            .update(print_claimed_at=now, print_claimed_by=claimant, printed_at=now)
        )
        if won:
            claimed_ids.append(so_id)

    orders = (
        Order.objects
        .filter(self_order__id__in=claimed_ids)
        .prefetch_related('items')
    )
    return Response(OrderSerializer(orders, many=True).data)
