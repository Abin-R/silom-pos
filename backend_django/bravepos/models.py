"""Brave POS models — mirror the MongoDB collections used by the legacy FastAPI
backend, but normalised into SQL tables.  Every model lives under the
``bravepos`` app label, so the underlying table is named ``bravepos_<model>``
and cannot collide with the other Django apps that share the production DB.

UUID primary keys preserve the existing API shape — the frontend already sends
and expects string UUIDs everywhere, so no client-side changes are needed when
we swap the backend.
"""
from __future__ import annotations

import uuid
from django.db import models


# ─── Shop directory ──────────────────────────────────────────────────────────
class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    name_th = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=16, default="#00B14F")
    order = models.IntegerField(default=0)
    source = models.CharField(max_length=64, blank=True, default="")  # e.g. "Grabfood"
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
        indexes = [models.Index(fields=["category"])]

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
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, blank=True, default="")
    last_visit = models.CharField(max_length=32, blank=True, default="")
    color = models.CharField(max_length=16, default="#94A3B8")

    class Meta:
        ordering = ["name"]


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
    name = models.CharField(max_length=200)
    items = models.JSONField(default=list)
    customer_id = models.UUIDField(null=True, blank=True)
    customer_name = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


# ─── Shifts ──────────────────────────────────────────────────────────────────
class Shift(models.Model):
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
