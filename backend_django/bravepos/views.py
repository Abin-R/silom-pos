"""Brave POS HTTP views.

Each endpoint pairs 1:1 with a route from the legacy FastAPI backend
(``../backend/server.py``) so the frontend doesn't change.  Routes still
TODO return 501 so we always have a clear "not ported yet" signal during
the migration.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone as djtz
from rest_framework import status, viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    Branch, BranchSession, Category, Customer, Order, OrderItem, ParkedOrder,
    Product, Settings, Shift, ShiftMovement, Staff, StockMovement,
)
from .serializers import (
    BranchSerializer,
    CategorySerializer, CustomerSerializer, OrderSerializer,
    ParkedOrderSerializer, ProductSerializer, SettingsSerializer,
    ShiftSerializer, ShiftMovementSerializer, StockMovementSerializer,
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

    # Single-session-per-branch: drop any other session on this branch
    # (different staff) before creating the new one.
    BranchSession.objects.filter(branch=branch).delete()
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
    # Drop any existing session on the target branch (same behavior as a fresh
    # login), drop our current session, then create a new one bound to the
    # target branch.
    BranchSession.objects.filter(branch=branch).delete()
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

    Query: ?branch_id=<uuid>. Returns admins (global — can sign in anywhere)
    plus cashiers explicitly assigned to that branch. Unauthenticated, since
    the PIN-pad screen needs this before any session exists.
    """
    branch_id = request.query_params.get('branch_id')
    if not branch_id:
        return Response({'detail': 'branch_id is required.'}, status=400)
    try:
        branch = Branch.objects.get(id=branch_id, active=True)
    except (Branch.DoesNotExist, ValueError):
        return Response({'detail': 'Branch not found.'}, status=404)

    admins = Staff.objects.filter(role='admin', active=True).order_by('name')
    cashiers = Staff.objects.filter(role='cashier', active=True, branches=branch).order_by('name')
    users = [
        {'id': str(s.id), 'name': s.name, 'role': s.role}
        for s in list(admins) + list(cashiers)
    ]
    return Response({'branch': {'id': str(branch.id), 'name': branch.name}, 'users': users})


@api_view(['POST'])
def auth_pin_login(request):
    """Body: { branch_id, staff_id, pin }.

    PIN-pad equivalent of /auth/login. Same session semantics: one active
    session per branch (any prior session on the branch is dropped) and one
    active session per staff (reject if signed in elsewhere).
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

    existing = (
        BranchSession.objects
        .select_related('branch')
        .filter(staff=staff)
        .first()
    )
    if existing and str(existing.branch_id) != str(branch.id):
        return Response({
            'detail': (
                f'This account is already signed in at '
                f'{existing.branch.name}. Sign out there first.'
            ),
            'code': 'user_already_signed_in',
            'occupied_branch': existing.branch.name,
        }, status=409)

    BranchSession.objects.filter(branch=branch).delete()
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
BEAM_API_KEY_MASK_PREFIX = '••••'


def _get_or_create_settings() -> Settings:
    obj, _ = Settings.objects.get_or_create(id='shop')
    return obj


def _mask_api_key(data: dict) -> dict:
    """Mirror the FastAPI mask: replace the Beam key with ••••<last4>."""
    key = data.get('beam_api_key') or ''
    if key and not key.startswith(BEAM_API_KEY_MASK_PREFIX) and len(key) > 4:
        data['beam_api_key'] = BEAM_API_KEY_MASK_PREFIX + key[-4:]
    return data


@api_view(['GET', 'PUT'])
@require_session
def settings_view(request):
    obj = _get_or_create_settings()
    if request.method == 'GET':
        return Response(_mask_api_key(dict(SettingsSerializer(obj).data)))

    # PUT — update, but never overwrite the API key with a mask placeholder
    # echoed back from the UI (matches FastAPI semantics).
    payload = dict(request.data)
    if 'beam_api_key' in payload:
        val = payload['beam_api_key']
        if not val or (isinstance(val, str) and val.startswith(BEAM_API_KEY_MASK_PREFIX)):
            payload.pop('beam_api_key')

    ser = SettingsSerializer(obj, data=payload, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(_mask_api_key(dict(SettingsSerializer(obj).data)))


# ─── Order numbering ─────────────────────────────────────────────────────────
def _next_order_number() -> str:
    """``PS`` + 9-digit running number, matching the FastAPI format."""
    last = (
        Order.objects
        .order_by('-order_number')
        .values_list('order_number', flat=True)
        .first()
    )
    n = 1
    if last and last.startswith('PS') and last[2:].isdigit():
        n = int(last[2:]) + 1
    return f'PS{n:09d}'


# ─── Orders ──────────────────────────────────────────────────────────────────
@api_view(['GET', 'POST'])
@require_session
def orders_list_create(request):
    branch = request.session_obj.branch
    if request.method == 'GET':
        qs = Order.objects.filter(branch=branch)
        source = request.query_params.get('source')
        status_ = request.query_params.get('status')
        if source and source != 'all':
            qs = qs.filter(source=source)
        if status_:
            qs = qs.filter(status=status_)
        return Response(OrderSerializer(qs[:500], many=True).data)

    # POST
    payload = dict(request.data)
    items_data = payload.pop('items', []) or []

    # Snapshot category info from products for items missing it (parity with FastAPI).
    # All lookups are scoped to the caller's branch so an order can only reference
    # products that live at that branch.
    product_ids = [it.get('product_id') for it in items_data if it.get('product_id')]
    cats_by_pid: dict[str, tuple[str | None, str]] = {}
    if product_ids:
        for p in Product.objects.filter(id__in=product_ids, branch=branch).select_related('category'):
            cats_by_pid[str(p.id)] = (
                str(p.category_id) if p.category_id else None,
                p.category.name if p.category else 'Other',
            )

    # Look up products once so we can both snapshot categories AND decrement
    # stock.  We lock only the Product rows (`of=('self',)`) because joining
    # the nullable Category for the same lock breaks on Postgres ("FOR UPDATE
    # cannot be applied to the nullable side of an outer join").  Categories
    # are looked up separately right after.
    products_by_id: dict[str, Product] = {}
    if product_ids:
        with transaction.atomic():
            for p in (
                Product.objects
                .select_for_update(of=('self',))
                .filter(id__in=product_ids, branch=branch)
            ):
                products_by_id[str(p.id)] = p
        # Lookup category names in one cheap query (no lock needed).
        cat_ids = {p.category_id for p in products_by_id.values() if p.category_id}
        cat_name_by_id = (
            {str(c.id): c.name for c in Category.objects.filter(id__in=cat_ids)}
            if cat_ids else {}
        )
        for pid_str, p in products_by_id.items():
            cats_by_pid[pid_str] = (
                str(p.category_id) if p.category_id else None,
                cat_name_by_id.get(str(p.category_id) or '', 'Other'),
            )

    # Silom-style workflow: after payment, the order lands in the "New Order"
    # kanban column so kitchen staff can pick it up and progress it through
    # Preparing → Completed manually.  Callers can still send an explicit
    # status (e.g. "completed" for an over-the-counter walk-in) to skip the
    # queue.
    requested_status = payload.get('status')
    initial_status = requested_status if requested_status in dict(Order.STATUS_CHOICES) else 'new'

    with transaction.atomic():
        order = Order.objects.create(
            branch=branch,
            order_number=_next_order_number(),
            subtotal=Decimal(str(payload.get('subtotal', 0))),
            discount_type=payload.get('discount_type', 'none'),
            discount_value=Decimal(str(payload.get('discount_value', 0))),
            discount_amount=Decimal(str(payload.get('discount_amount', 0))),
            total=Decimal(str(payload.get('total', 0))),
            payment_method=payload.get('payment_method', '') or '',
            paid_amount=Decimal(str(payload.get('paid_amount', 0))),
            change=Decimal(str(payload.get('change', 0))),
            status=initial_status,
            source=payload.get('source', 'table'),
            customer_id=payload.get('customer_id'),
            customer_name=payload.get('customer_name', '') or '',
            beam_charge_id=payload.get('beam_charge_id', '') or '',
            delivery_provider=payload.get('delivery_provider', '') or '',
            delivery_status=payload.get('delivery_status', '') or '',
            created_time=datetime.now().strftime('%H:%M'),
            staff=payload.get('staff', '') or '',
        )
        for it in items_data:
            pid = str(it.get('product_id')) if it.get('product_id') else ''
            cat_id, cat_name = cats_by_pid.get(
                pid,
                (it.get('category_id'), it.get('category_name', '')),
            )
            qty = int(it.get('qty', 1))
            OrderItem.objects.create(
                order=order,
                product_id=it.get('product_id'),
                name=it.get('name', ''),
                price=Decimal(str(it.get('price', 0))),
                qty=qty,
                category_id=cat_id,
                category_name=cat_name or '',
            )

            # Decrement product stock + log a StockMovement for the audit
            # trail.  Stock can go negative — matches the existing demo data
            # (Sexy Back Cookie shipped with stock=-32) and avoids blocking
            # the order if the cashier wants to oversell.
            prod = products_by_id.get(pid)
            if prod:
                prod.stock = (prod.stock or 0) - qty
                prod.save(update_fields=['stock'])
                StockMovement.objects.create(
                    branch=branch,
                    product=prod,
                    product_name=prod.name,
                    type='out',
                    qty=qty,
                    note=f'Order {order.order_number}',
                    document_no=order.order_number,
                )
    return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@require_session
def order_update_status(request, order_id):
    order = get_object_or_404(Order, id=order_id, branch=request.session_obj.branch)
    new_status = (request.data or {}).get('status')
    if new_status not in dict(Order.STATUS_CHOICES):
        return Response({'detail': 'Invalid status'}, status=400)
    order.status = new_status
    order.save(update_fields=['status'])
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
        note=request.data.get('note', '') or '',
    )
    if type_ == 'paid_in':
        s.total_paid_in = (s.total_paid_in or 0) + amount
    else:
        s.total_paid_out = (s.total_paid_out or 0) + amount
    s.save(update_fields=['total_paid_in', 'total_paid_out'])
    return Response(ShiftMovementSerializer(mv).data, status=201)


@api_view(['PUT'])
@require_session
def shift_close(request):
    branch = request.session_obj.branch
    s = Shift.objects.filter(branch=branch, status='open').first()
    if not s:
        return Response({'detail': 'No open shift'}, status=400)
    from django.db.models import Sum
    cash_total = (
        Order.objects
        .filter(branch=branch, created_at__gte=s.opened_at,
                payment_method__in=['Easy Pay', 'Cash'])
        .aggregate(t=Sum('total'))['t'] or Decimal('0')
    )
    expected = (s.start_cash or 0) + cash_total + (s.total_paid_in or 0) - (s.total_paid_out or 0)
    s.status = 'closed'
    s.closed_at = djtz.now()
    s.closed_by = request.data.get('closed_by', 'Admin') or 'Admin'
    s.actual_in_drawer = Decimal(str(request.data.get('actual_in_drawer', 0) or 0))
    s.total_sales_cash = cash_total
    s.expected_in_drawer = expected
    s.save()
    return Response(ShiftSerializer(s).data)


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
    if period == 'today':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'week':
        start = now - timedelta(days=7)
    elif period == 'year':
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=30)

    orders = list(
        Order.objects
        .filter(branch=branch, created_at__gte=start)
        .exclude(status='cancel')
        .prefetch_related('items')
    )

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


# ─── Beam Payment ────────────────────────────────────────────────────────────
SATANG_PER_THB = 100
BEAM_POST_TIMEOUT_S = 15.0
BEAM_GET_TIMEOUT_S = 10.0


def _beam_credentials() -> tuple[str, dict]:
    """Returns (base_url, headers) or raises a tuple-error suitable for Response."""
    s = _get_or_create_settings()
    if not s.beam_merchant_id or not s.beam_api_key:
        raise ValueError(
            'Beam credentials not configured. Go to Settings → Payment to add '
            'your Merchant ID and API Key.'
        )
    base = (
        django_settings.BRAVEPOS['BEAM_PLAYGROUND_URL']
        if s.beam_sandbox
        else django_settings.BRAVEPOS['BEAM_PRODUCTION_URL']
    )
    token = base64.b64encode(f"{s.beam_merchant_id}:{s.beam_api_key}".encode()).decode()
    return base, {'Authorization': f'Basic {token}'}


def _extract_qr_data(data: dict) -> tuple[str | None, str | None]:
    qr_image, qr_string = None, None
    if data.get('actionRequired') == 'ENCODED_IMAGE':
        e = data.get('encodedImage', {})
        qr_image = e.get('imageBase64Encoded') or e.get('image')
        qr_string = e.get('rawData') or e.get('qrString')
    elif data.get('qrCode'):
        qr_image = data['qrCode']
    return qr_image, qr_string


@api_view(['POST'])
@require_session
def beam_charge_create(request):
    try:
        base, headers = _beam_credentials()
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)

    amount = float(request.data.get('amount', 0) or 0)
    reference_id = request.data.get('reference_id') or request.data.get('reference', '')
    description = request.data.get('description') or f"Order {reference_id}"

    payload = {
        'amount': int(round(amount * SATANG_PER_THB)),
        'currency': 'THB',
        'referenceId': reference_id,
        'description': description,
        'paymentMethod': {'paymentMethodType': 'QR_PROMPT_PAY', 'qrPromptPay': {}},
    }
    try:
        with httpx.Client(timeout=BEAM_POST_TIMEOUT_S) as client:
            resp = client.post(f"{base}/api/v1/charges", json=payload, headers=headers)
    except httpx.TimeoutException:
        return Response({'detail': 'Beam API timed out. Please try again.'}, status=502)
    except httpx.RequestError as e:
        return Response({'detail': f'Cannot reach Beam API: {e}'}, status=502)

    if resp.status_code == 401:
        return Response({'detail': 'Beam API key is invalid or expired.'}, status=401)
    if resp.status_code not in (200, 201):
        return Response(
            {'detail': f'Beam API error {resp.status_code}: {resp.text[:300]}'},
            status=502,
        )

    data = resp.json()
    charge_id = data.get('chargeId') or data.get('id') or data.get('charge_id') or ''
    if not charge_id:
        return Response(
            {'detail': 'Beam response did not include a charge id; cannot poll status.'},
            status=502,
        )
    qr_image, qr_string = _extract_qr_data(data)
    return Response({
        'charge_id': charge_id,
        'status': data.get('status', 'PENDING'),
        'qr_image': qr_image,
        'qr_string': qr_string,
        'amount': amount,
        'currency': 'THB',
    })


@api_view(['GET'])
@require_session
def beam_charge_status(request, charge_id):
    try:
        base, headers = _beam_credentials()
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)
    try:
        with httpx.Client(timeout=BEAM_GET_TIMEOUT_S) as client:
            resp = client.get(f"{base}/api/v1/charges/{charge_id}", headers=headers)
    except httpx.TimeoutException:
        return Response({'detail': 'Beam API timed out.'}, status=502)
    except httpx.RequestError as e:
        return Response({'detail': f'Cannot reach Beam API: {e}'}, status=502)
    if resp.status_code != 200:
        return Response({'detail': f'Beam API error {resp.status_code}'}, status=502)
    data = resp.json()
    return Response({
        'charge_id': data.get('chargeId') or data.get('id') or charge_id,
        'status': data.get('status', 'PENDING'),
        'amount': (data.get('amount') or 0) / SATANG_PER_THB,
        'currency': data.get('currency', 'THB'),
    })


# ─── Seed (demo data, idempotent) ────────────────────────────────────────────
@api_view(['POST'])
def seed_data(_request):
    """Wipe + reseed the bravepos_* tables only.  Never touches other apps."""
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
