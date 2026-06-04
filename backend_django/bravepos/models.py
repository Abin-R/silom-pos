"""Brave POS models — mirror the MongoDB collections used by the legacy FastAPI
backend, but normalised into SQL tables.  Every model lives under the
``bravepos`` app label, so the underlying table is named ``bravepos_<model>``
and cannot collide with the other Django apps that share the production DB.

UUID primary keys preserve the existing API shape — the frontend already sends
and expects string UUIDs everywhere, so no client-side changes are needed when
we swap the backend.
"""
from __future__ import annotations

import secrets
import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone


# ─── Staff (email/password auth) ────────────────────────────────────────────
class Staff(models.Model):
    """A POS user who logs in with email + password.

    NOT using Django's built-in ``auth_user`` — we share that table with
    other apps in the same Postgres, and our role/branch model is custom.
    All Brave POS auth lives in this ``bravepos_staff`` table.
    """
    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("cashier", "Cashier"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    password_hash = models.CharField(max_length=256)
    # PIN is the only credential the in-app login uses. Email/password remain
    # in the schema so a Django-admin/script-driven flow can still seed staff,
    # but the cashier-facing PIN pad never touches them.
    pin_hash = models.CharField(max_length=256, blank=True, default="")
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default="cashier")
    branches = models.ManyToManyField(
        "Branch", related_name="staff", blank=True,
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    # ── Password helpers ──
    def set_password(self, plain: str) -> None:
        self.password_hash = make_password(plain)

    def check_password(self, plain: str) -> bool:
        return check_password(plain, self.password_hash)

    # ── PIN helpers (4-digit numeric, hashed at rest) ──
    def set_pin(self, plain: str) -> None:
        self.pin_hash = make_password(plain)

    def check_pin(self, plain: str) -> bool:
        if not self.pin_hash:
            return False
        return check_password(plain, self.pin_hash)


class BranchSession(models.Model):
    """The single active login for a branch.

    Enforced as 1-row-per-branch by the unique constraint on ``branch``.
    Logging in to a branch that's already taken DELETEs the old row then
    INSERTs a new one — the kicked-out user's token stops authenticating.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.OneToOneField(
        "Branch", on_delete=models.CASCADE, related_name="current_session",
    )
    staff = models.ForeignKey(
        Staff, on_delete=models.CASCADE, related_name="sessions",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-opened_at"]

    @staticmethod
    def new_token() -> str:
        # 64 hex chars (256 bits) — opaque, secret, generated server-side.
        return secrets.token_hex(32)


# ─── Branches ───────────────────────────────────────────────────────────────
class Branch(models.Model):
    """A physical shop location.  Each tablet/POS terminal is assigned to one
    branch at login time.  Sales, shifts, etc. will eventually carry a branch
    FK; we keep the model nullable in those relations during the rollout so
    existing data isn't broken."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=16, blank=True, default="")  # optional short code
    address = models.TextField(blank=True, default="")
    # SilomPOS-style split: shown as two free-form lines on the Shop page
    # and printed on the per-branch receipt. The legacy `address` blob is
    # kept in sync on save for older callers that still read it.
    address_line_1 = models.CharField(max_length=200, blank=True, default="")
    address_line_2 = models.CharField(max_length=200, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    tax_id = models.CharField(max_length=64, blank=True, default="")
    pos_id = models.CharField(max_length=64, blank=True, default="")
    logo_url = models.TextField(blank=True, default="")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ─── Shop directory ──────────────────────────────────────────────────────────
class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="categories",
        null=True, blank=True,  # nullable for migration backfill; enforced at app level
    )
    name = models.CharField(max_length=120)
    name_th = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=16, default="#00B14F")
    order = models.IntegerField(default=0)
    source = models.CharField(max_length=64, blank=True, default="")  # e.g. "Grabfood"
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]
        indexes = [models.Index(fields=["branch"])]

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="products",
        null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    name_th = models.CharField(max_length=200, blank=True, default="")
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="products",
    )
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    stock = models.IntegerField(default=0)
    sku = models.CharField(max_length=64, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    image_url = models.TextField(blank=True, default="")
    image_base64 = models.TextField(blank=True, default="")
    is_favorite = models.BooleanField(default=False)
    tax_type = models.CharField(max_length=4, default="V")  # V=VAT, N=None
    product_type = models.CharField(max_length=4, default="P")  # P=Product, S=Service, BOM=Bundle
    active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["branch"]),
        ]

    def __str__(self) -> str:
        return self.name


class StockMovement(models.Model):
    TYPES = [
        ("in", "Stock in"),
        ("out", "Stock out"),
        ("adjust", "Adjust"),
        ("check", "Check"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="stock_movements",
        null=True, blank=True,
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="movements",
    )
    product_name = models.CharField(max_length=200, blank=True, default="")  # snapshot
    type = models.CharField(max_length=16, choices=TYPES)
    qty = models.IntegerField()
    note = models.TextField(blank=True, default="")
    document_no = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


# ─── Customers ───────────────────────────────────────────────────────────────
class Customer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="customers",
        null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, blank=True, default="")
    last_visit = models.CharField(max_length=32, blank=True, default="")
    color = models.CharField(max_length=16, default="#94A3B8")

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["branch"])]


# ─── Settings (single-row config) ────────────────────────────────────────────
class Settings(models.Model):
    """One row per deployment.  ``id`` is hardcoded to ``"shop"`` so there is
    always exactly one Settings instance per database."""
    id = models.CharField(primary_key=True, max_length=32, default="shop")
    shop_name = models.CharField(max_length=200, default="Brave POS")
    business_type = models.CharField(max_length=64, default="Restaurant")
    tax_id = models.CharField(max_length=64, blank=True, default="")
    pos_id = models.CharField(max_length=64, default="001")
    branch = models.CharField(max_length=120, default="Main")
    pos_number = models.CharField(max_length=16, default="001")
    open_time = models.CharField(max_length=8, default="09:00")
    close_time = models.CharField(max_length=8, default="22:00")
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=7)
    tax_mode = models.CharField(max_length=16, default="exclusive")  # exclusive|inclusive
    service_charge_enabled = models.BooleanField(default=False)
    service_charge_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    logo_url = models.TextField(blank=True, default="")
    logo_path = models.TextField(blank=True, default="")
    address = models.TextField(blank=True, default="")
    # SilomPOS-style split: the dashboard shows two free-form lines. We keep
    # the legacy single `address` blob for back-compat (older callers still
    # read it), but new edits write both lines and receipts render them.
    address_line_1 = models.CharField(max_length=200, blank=True, default="")
    address_line_2 = models.CharField(max_length=200, blank=True, default="")
    # Legal entity printed above the address on tax-invoice receipts.
    company_name = models.CharField(max_length=200, blank=True, default="")
    currency = models.CharField(max_length=8, default="THB")
    phone = models.CharField(max_length=32, blank=True, default="")

    # Beam gateway
    beam_merchant_id = models.CharField(max_length=128, blank=True, default="")
    beam_api_key = models.CharField(max_length=256, blank=True, default="")
    beam_sandbox = models.BooleanField(default=True)

    # Printer
    printer_enabled = models.BooleanField(default=False)
    printer_transport = models.CharField(max_length=16, default="disabled")
    printer_address = models.CharField(max_length=200, blank=True, default="")
    printer_paper_width = models.IntegerField(default=80)

    class Meta:
        verbose_name = "Settings"
        verbose_name_plural = "Settings"

    def __str__(self) -> str:
        return f"Settings({self.shop_name})"


# ─── Orders ──────────────────────────────────────────────────────────────────
class Order(models.Model):
    STATUS_CHOICES = [
        ("new", "New"),
        ("preparing", "Preparing"),
        ("completed", "Completed"),
        ("cancel", "Cancelled"),
    ]
    SOURCE_CHOICES = [
        ("table", "Table"),
        ("delivery", "Delivery"),
        ("kiosk", "KIOSK"),
        ("other", "Other"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="orders",
        null=True, blank=True,
    )
    order_number = models.CharField(max_length=32, unique=True, db_index=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_type = models.CharField(max_length=16, default="none")  # none|amount|percent
    discount_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=64, blank=True, default="")
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    change = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="completed")
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default="table")
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="orders",
    )
    customer_name = models.CharField(max_length=200, blank=True, default="")
    beam_charge_id = models.CharField(max_length=128, blank=True, default="")
    delivery_provider = models.CharField(max_length=64, blank=True, default="")
    delivery_status = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    created_time = models.CharField(max_length=8, blank=True, default="")  # "HH:MM"
    staff = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["source"]),
            models.Index(fields=["status"]),
            models.Index(fields=["branch"]),
        ]


class OrderItem(models.Model):
    """One line of an Order.  De-normalised name / price / category snapshots
    so that future product edits never rewrite history."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="order_items",
    )
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    qty = models.IntegerField(default=1)
    category_id = models.UUIDField(null=True, blank=True)
    category_name = models.CharField(max_length=120, blank=True, default="")


# ─── Parked orders ───────────────────────────────────────────────────────────
class ParkedOrder(models.Model):
    """A held cart that hasn't been paid yet.  Items are stored as JSON because
    they're rarely queried and the shape is identical to the in-memory cart."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="parked_orders",
        null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    items = models.JSONField(default=list)
    customer_id = models.UUIDField(null=True, blank=True)
    customer_name = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["branch"])]


# ─── Shifts ──────────────────────────────────────────────────────────────────
class Shift(models.Model):
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="shifts",
        null=True, blank=True,
    )
    round_number = models.IntegerField(default=1)
    start_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    opened_at = models.DateTimeField(auto_now_add=True)
    opened_by = models.CharField(max_length=120, default="Admin")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.CharField(max_length=120, blank=True, default="")
    total_sales_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_paid_in = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_paid_out = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    expected_in_drawer = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    actual_in_drawer = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="open")

    class Meta:
        ordering = ["-opened_at"]
        indexes = [models.Index(fields=["branch"])]


class ShiftMovement(models.Model):
    TYPE_CHOICES = [("paid_in", "Paid In"), ("paid_out", "Paid Out")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="movements")
    type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
