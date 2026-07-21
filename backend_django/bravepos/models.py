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

    # ── Django auth duck-typing ──
    # The backoffice's `django.contrib.auth` machinery expects `request.user`
    # to look like Django's User. We stay off the shared `auth_user` table
    # (see [[reference-bravepos-deploy]] — Postgres is shared with `home`
    # and `instamator_app`, which rewrite that table). Instead the custom
    # `backoffice.auth_backend.StaffBackend` returns Staff instances, and
    # the properties below let @login_required / templates treat them as
    # regular users.
    is_authenticated = True
    is_anonymous = False

    @property
    def is_active(self):
        return self.active

    @property
    def is_staff(self):
        return True

    @property
    def is_superuser(self):
        return self.role == "admin"

    def get_username(self):
        return self.email

    def get_session_auth_hash(self):
        from django.utils.crypto import salted_hmac
        return salted_hmac(
            "backoffice.auth.Staff.get_session_auth_hash",
            self.password_hash or "",
        ).hexdigest()

    # ── PIN helpers (4-digit numeric, hashed at rest) ──
    def set_pin(self, plain: str) -> None:
        self.pin_hash = make_password(plain)

    def check_pin(self, plain: str) -> bool:
        if not self.pin_hash:
            return False
        return check_password(plain, self.pin_hash)


class BranchSession(models.Model):
    """An active login at a branch.

    Multiple staff (e.g. an admin phone + a cashier phone) can be signed in
    to the same branch at once — one row per device/login.  A given *staff*
    account is still limited to one active session at a time (enforced in the
    login views), so the same identity can't be held on two devices.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="sessions",
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
    open_time = models.CharField(max_length=8, default="09:00")
    close_time = models.CharField(max_length=8, default="22:00")
    # Peak payment account code used when issuing the full tax invoice for
    # this branch's orders.  The default "BSV003" is Peak's generic payment
    # method code; a branch configured with a real chart-of-accounts code
    # (e.g. "113105") is sent as an ``accountCode`` instead.  See
    # ``peak.create_peak_receipt_for_order``.
    peak_account_code = models.CharField(max_length=32, blank=True, default="BSV003")

    # ── Per-branch payment credentials ─────────────────────────────────
    # Each branch can run its own Beam/Omise account.  This is an *override*,
    # not a replacement: while ``payment_own`` is False (the default for every
    # existing branch) all payment flows keep reading the shop-wide ``Settings``
    # singleton exactly as before, so turning this on for one branch changes
    # nothing for the others — and flipping it back off is an instant rollback.
    # Resolution lives in ``gateways.resolve_payment_config``.
    #
    # Secrets never leave the backoffice: BranchSerializer (the public /branches
    # feed the login screen reads) does NOT expose these fields.
    # Per-branch on/off switch for customer self-ordering — the feature flag for
    # a staged rollout AND the instant kill switch.  Default False, so deploying
    # the code exposes NO branch (biohouse included) until it is turned on here.
    # ``/api/branches`` is public and leaks branch ids, so this flag — not URL
    # obscurity — is what keeps self-ordering closed on a branch.
    self_order_enabled = models.BooleanField(default=False)

    payment_own = models.BooleanField(default=False)
    beam_merchant_id = models.CharField(max_length=128, blank=True, default="")
    beam_api_key = models.CharField(max_length=256, blank=True, default="")
    beam_sandbox = models.BooleanField(default=True)
    beam_card_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=3.65)
    omise_public_key = models.CharField(max_length=128, blank=True, default="")
    omise_secret_key = models.CharField(max_length=128, blank=True, default="")
    omise_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=3.65)

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


class Unit(models.Model):
    """A unit of measure (e.g. BOX, Piece, KG, ชิ้น) a product is counted in.
    Mirrors the SilomPOS Unit list — name + active flag, with a last-updated
    timestamp shown in the management table."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="units",
        null=True, blank=True,  # nullable: shared units aren't branch-scoped
    )
    name = models.CharField(max_length=64)
    order = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

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
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True,
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
    # Processing fee passed on to the customer for Beam card payments, as a
    # percentage of the goods total (kept separate from Omise's rate so each
    # gateway can carry its own surcharge).
    beam_card_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=3.65)

    # Omise gateway (credit-card payment links).  Omise has no separate base
    # URL for test vs live — the key prefix (pkey_test_/skey_test_ vs
    # pkey_/skey_) determines the environment, so there is no sandbox flag.
    omise_public_key = models.CharField(max_length=128, blank=True, default="")
    omise_secret_key = models.CharField(max_length=128, blank=True, default="")
    # Processing fee passed on to the customer for card payments, as a
    # percentage of the goods total (Omise Thailand's standard rate is 3.65%).
    omise_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=3.65)

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
    # Beam credit-card payment link (mirrors the Omise link fields below).
    beam_link_id = models.CharField(max_length=128, blank=True, default="")

    # ── Tax + card processing fee ──────────────────────────────────────
    # ``vat_amount`` is the 7% VAT *already contained* in the goods total
    # (prices are VAT-inclusive): vat = goods × 7/107.  For Omise card
    # payments the customer also covers the processing fee plus 7% VAT on
    # that fee; ``total`` is the grand total actually charged
    # (goods + processing_fee + processing_fee_vat).
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    processing_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    processing_fee_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    omise_link_id = models.CharField(max_length=128, blank=True, default="")
    omise_charge_id = models.CharField(max_length=128, blank=True, default="")
    delivery_provider = models.CharField(max_length=64, blank=True, default="")
    delivery_status = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    created_time = models.CharField(max_length=8, blank=True, default="")  # "HH:MM"
    staff = models.CharField(max_length=120, blank=True, default="")

    # ── Shift attribution ──────────────────────────────────────────────
    # Stamped at creation so an order always belongs to exactly one round.
    # ``_shift_summary`` used to window purely on ``created_at`` between the
    # shift's opened_at/closed_at, which silently *drops* an order that lands
    # after the cashier closed the round — a self-order paid at 22:01 and
    # confirmed at 22:03, after a 22:02 close, appeared in neither round's
    # totals.  The summary now prefers this FK and only falls back to the
    # time window for orders created before the field existed (hence null=True).
    shift = models.ForeignKey(
        "Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="orders",
    )

    # Per-branch, per-day number the customer is called by.  Previously derived
    # on the client as the last two digits of the *global* order_number
    # sequence (ReceiptImage.tsx), which collided across branches and repeated
    # every 100 orders.  Null for orders created before this field existed —
    # the receipt falls back to the old derivation for those.
    queue_number = models.IntegerField(null=True, blank=True)

    # ── Void / cancel audit ────────────────────────────────────────────
    # Set when a cashier voids a completed bill from the Transactions
    # screen.  ``voided_by`` snapshots the staff name (history-safe like
    # ``staff`` above) and ``voided_at`` the moment of the void so the
    # receipt detail can show "Voided by: <name>".
    voided_by = models.CharField(max_length=120, blank=True, default="")
    voided_at = models.DateTimeField(null=True, blank=True)

    # ── Peak full-tax-invoice integration ──────────────────────────────
    # Populated when a customer scans the receipt QR and submits the
    # full-tax-invoice form.  ``tax_invoice_data`` captures the form
    # fields verbatim (name, taxId, address, etc.).  ``peak_queue_id`` is
    # returned by Peak's /receipts/queue endpoint so we can poll for the
    # final receipt.  ``peak_response`` is the final queue payload,
    # including the documentLink we redirect the customer to.
    tax_invoice_data = models.JSONField(null=True, blank=True)
    peak_queue_id = models.CharField(max_length=128, blank=True, default="")
    peak_response = models.JSONField(null=True, blank=True)

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
    # Per-line discount (flat THB). There is no order-level discount in the POS —
    # the order's discount_amount is just the sum of these line discounts.
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
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


# ─── Customer self-ordering ──────────────────────────────────────────────────
class SelfOrder(models.Model):
    """A cart a customer built on their own phone, before it becomes a sale.

    Deliberately NOT an ``Order``.  An Order row is a real sale — every
    dashboard, report, kanban and shift query in the codebase assumes so.  An
    unpaid or abandoned self-order must not pollute those, so the draft lives
    here and is *promoted* into an Order only once the gateway confirms
    payment.  Same reasoning (and same JSON-items shape) as ``ParkedOrder``.

    ``items`` are priced SERVER-SIDE from the Product table at /start/ and
    frozen here.  The customer's browser is untrusted, so its prices are never
    read; and freezing means a menu price change mid-checkout can't alter what
    they were quoted.  The money fields are frozen for the same reason: the fee
    and VAT percentages live in the editable ``Settings`` singleton, and a
    self-order's build→pay window is minutes (vs. seconds at the till), so
    recomputing at promotion time could make Order.total disagree with the
    satang the gateway actually captured.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),    # created, not yet paid
        ("paid", "Paid"),          # gateway confirmed → promoted to an Order
        ("failed", "Failed"),      # gateway said expired/voided/refunded
        ("expired", "Expired"),    # never paid; aged out by the sweeper
    ]
    METHOD_QR = "Beam QR"
    METHOD_CARD = "Beam Card"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The bearer secret for the whole flow — whoever holds it owns the cart.
    # Deliberately NOT the order_number: that is a guessable running sequence
    # (PS000000123) and is already public on the receipt page.
    token = models.CharField(max_length=64, unique=True, db_index=True)

    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="self_orders",
    )
    items = models.JSONField(default=list)

    # Frozen money.  goods_total is VAT-inclusive; vat_amount is the 7/107
    # already contained in it.  processing_fee (+ its VAT) is added only for
    # card, so `total` differs by method — which is why it is only final once
    # the customer has picked one at /pay/.
    goods_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    processing_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    processing_fee_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    payment_method = models.CharField(max_length=64, blank=True, default="")
    beam_charge_id = models.CharField(max_length=128, blank=True, default="")
    beam_link_id = models.CharField(max_length=128, blank=True, default="")

    # Beam hands back the QR image / checkout URL exactly once, when the charge
    # or link is *created* — the status endpoint doesn't repeat them.  Cached
    # here so a customer who reloads the payment page (or comes back to it) is
    # shown the same QR instead of us minting a second charge for the same cart.
    qr_image_cache = models.TextField(blank=True, default="")
    payment_uri_cache = models.TextField(blank=True, default="")

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")

    # The sale this draft became.  OneToOne is a DB-level backstop against
    # double promotion: two pollers (the customer's phone and the POS) can race
    # on the same token, and the unique constraint makes a second Order for the
    # same draft impossible even if the row lock were somehow bypassed.
    order = models.OneToOneField(
        "Order", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="self_order",
    )

    # ── Print claim ────────────────────────────────────────────────────
    # The receipt prints on the POS tablet, not the server, and a branch can
    # have several tablets.  printerQueue's in-flight Set only dedupes within
    # one JS runtime, so the claim has to be server-side: a tablet wins the row
    # with a conditional UPDATE and only prints what it won.  ``print_claimed_at``
    # doubles as a lease — a stale claim (dead tablet) becomes stealable.
    print_claimed_at = models.DateTimeField(null=True, blank=True)
    print_claimed_by = models.CharField(max_length=64, blank=True, default="")
    printed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["status", "created_at"]),  # the sweeper's scan
        ]

    def __str__(self):
        return f"SelfOrder {self.token[:8]}… ({self.status})"

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)


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
    # Snapshot of the chosen DrawerCategory name at the time of the movement,
    # so renaming/deleting a category later never rewrites historical records.
    category = models.CharField(max_length=120, blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class DrawerCategory(models.Model):
    """Reason codes for cash Paid In / Paid Out movements.

    Branch-scoped and editable by admins in Settings → Drawer.  Defaults
    (Thai reason codes) are seeded per branch on creation, but each row can be
    renamed, reordered, deactivated, or deleted independently.
    """
    TYPE_CHOICES = [("paid_in", "Paid In"), ("paid_out", "Paid Out")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="drawer_categories",
        null=True, blank=True,
    )
    type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    name = models.CharField(max_length=120)
    name_th = models.CharField(max_length=120, blank=True, default="")
    sort_order = models.IntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [models.Index(fields=["branch", "type"])]

    def __str__(self) -> str:
        return f"{self.type}:{self.name}"


# ─── Peak (full tax-invoice) integration ────────────────────────────────────
# The customer-receipt landing page lets shoppers request a "full tax
# invoice" — the form data goes to Peak's API and produces a downloadable
# tax-invoice document, mirroring what Shopster's Django app already does
# for SilomPOS / Grab / Buzzb. These three models cover what we need to
# replicate that flow ourselves.

class PeakProductMap(models.Model):
    """Maps one of our Products to its Peak product code, so receipt
    submissions don't have to create a fresh Peak product for every item
    every time.  Created lazily — on the first receipt that includes a
    product, we POST /products to Peak, get a code back, and cache the
    pair here.  Subsequent receipts reuse it."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.OneToOneField(
        Product, on_delete=models.CASCADE, related_name="peak_map",
    )
    peak_code = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PeakRequest(models.Model):
    """Audit log of every outbound Peak API call.  Mirrors the structure
    of Shopster's PeakRequest table — invaluable for debugging when a
    receipt fails to materialise (the response body is the only source
    of truth for Peak's error codes)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    url = models.TextField()
    method = models.CharField(max_length=8)
    headers = models.JSONField(null=True, blank=True)
    body = models.JSONField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]


class PeakClientToken(models.Model):
    """Singleton-ish row holding Peak's rotating client token.  On a
    fresh install seed it from the env var PEAK_CLIENT_TOKEN.  The token
    refresh flow (when Peak returns resCode '600') is intentionally
    out-of-band for now — set up an admin/cron task to update this row
    when the token expires."""
    id = models.CharField(primary_key=True, max_length=16, default="default")
    token = models.CharField(max_length=512)
    updated_at = models.DateTimeField(auto_now=True)


# ─── Stock Documents (multi-line stock-in / stock-out) ───────────────────────
class StockDocument(models.Model):
    """A multi-line stock-in or stock-out document — the SilomPOS-style
    inventory paperwork (vendor / receiver, reference no., a table of
    product lines).  Saving one applies the per-line stock deltas and
    snapshots a StockMovement per line so the movement ledger stays the
    single source of truth for on-hand quantities."""
    TYPES = [
        ("in", "Stock in"),
        ("out", "Stock out"),
        ("adjust", "Adjust stock"),
        ("check", "Check stock"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        "Branch", on_delete=models.CASCADE, related_name="stock_documents",
        null=True, blank=True,
    )
    type = models.CharField(max_length=8, choices=TYPES)
    document_no = models.CharField(max_length=64, db_index=True)
    document_name = models.CharField(max_length=200, blank=True, default="")  # adjust / check
    adjust_type = models.CharField(max_length=8, blank=True, default="")      # "A+" / "A-"
    ref_no = models.CharField(max_length=120, blank=True, default="")  # purchasing / ref doc no.
    vendor = models.CharField(max_length=200, blank=True, default="")    # stock-in
    receiver = models.CharField(max_length=200, blank=True, default="")  # stock-out
    note = models.TextField(blank=True, default="")
    tax_included = models.BooleanField(default=False)
    avg_cost = models.BooleanField(default=False)  # "AVG Cost Calculate" toggle (stock-in)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_by = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "type", "-created_at"]),
        ]


class StockDocumentItem(models.Model):
    """One product line of a StockDocument.  Name / barcode are snapshotted
    so later product edits never rewrite the document."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        StockDocument, on_delete=models.CASCADE, related_name="items",
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_document_items",
    )
    barcode = models.CharField(max_length=64, blank=True, default="")
    product_name = models.CharField(max_length=200, blank=True, default="")
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # in/out qty OR adjust update-delta
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Reconcile flow (adjust / check): snapshot of on-hand before the count and
    # the counted ("reconcile") target.  ``qty`` carries the resulting delta.
    before_qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reconcile_qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)

