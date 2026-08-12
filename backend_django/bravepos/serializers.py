"""DRF serializers — shape the JSON over the wire so it matches what the
existing frontend already sends and expects."""
from rest_framework import serializers

from . import images
from .models import (
    Branch, Category, Product, StockMovement, Customer,
    Settings, Order, OrderItem, ParkedOrder, Shift, ShiftMovement,
    DrawerCategory, StockDocument, StockDocumentItem, StockOutReason,
)


class BranchSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=False, required=False)
    # Surface the first cashier's email so the edit screen can display + update
    # it without an extra round trip.  One-cashier-per-branch is the assumption
    # baked into the create flow; if admin manually assigns multiple, we just
    # show the first.
    cashier_email = serializers.SerializerMethodField()
    # Declared, and with no validators, purely to keep DRF's own uniqueness
    # machinery off this field: ``branch_pos_id_unique_when_set`` would
    # otherwise generate a UniqueValidator whose message ("branch with this pos
    # id already exists") names no branch, which is the one fact the person
    # fixing the clash needs.  ``validate_pos_id`` below does the check instead.
    pos_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, validators=[])

    class Meta:
        model = Branch
        # ``self_order_enabled`` lets the POS app switch the whole self-ordering
        # feature off per branch (no print poller, no QR screen), so a branch
        # that doesn't use it runs exactly as it did before.  Not a secret — the
        # payment credentials on Branch are deliberately NOT exposed here.
        fields = ['id', 'name', 'code', 'address', 'phone', 'tax_id', 'pos_id',
                  'peak_account_code', 'active', 'created_at', 'cashier_email',
                  'self_order_enabled']
        read_only_fields = ['created_at', 'cashier_email', 'self_order_enabled']
        # The other half of that: from the same constraint DRF also builds a
        # serializer-level UniqueTogetherValidator, and that one forces every
        # field it covers to be *required* on create — a new branch normally has
        # no machine number yet, so branch creation would start 400-ing.
        validators = []

    def get_cashier_email(self, obj):
        s = obj.staff.filter(role='cashier').order_by('created_at').first()
        return s.email if s else ''

    def validate_pos_id(self, value):
        """One Revenue Department machine number, one branch.

        The database enforces this too (``branch_pos_id_unique_when_set``), but
        an IntegrityError reaches the caller as a 500 — this turns it into a 400
        that names the branch already holding the number.  Blank is always
        allowed: it means "not registered yet", which any number of branches
        may be.
        """
        value = (value or '').strip()
        if not value:
            return value
        clash = Branch.objects.filter(pos_id=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        other = clash.first()
        if other:
            raise serializers.ValidationError(
                f'POS ID "{value}" already belongs to {other.name}.'
            )
        return value


class CategorySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=False, required=False)

    class Meta:
        model = Category
        fields = ['id', 'name', 'name_th', 'color', 'order', 'source', 'active']


class ProductSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=False, required=False)
    category_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'name_th', 'category_id',
            'price', 'cost', 'stock', 'sku', 'barcode',
            'image_url', 'image_base64',
            'is_favorite', 'tax_type', 'product_type',
            'active', 'sort_order',
        ]

    # Both image fields are unbounded TextFields holding base64 ``data:`` URIs.
    # The app resizes before sending, but that is the *client's* promise —
    # ``image_url`` is a free-text field a paste can bypass entirely, and an
    # older build may not resize at all.  Capping here means the cap holds for
    # every writer of this API, now and later.  Oversized is normalised rather
    # than rejected: a 400 on save would lose the whole product edit over an
    # image, which is not a trade the cashier asked for.  See bravepos.images.
    def validate_image_url(self, value):
        return images.normalize(value)

    def validate_image_base64(self, value):
        return images.normalize(value)


class CustomerSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=False, required=False)
    # The Add-Customer form leaves the date of birth empty most of the time and
    # sends "" for it.  DRF's DateField rejects "" outright, so accept it and
    # store NULL instead of failing the whole save over an optional field.
    birth_date = serializers.DateField(required=False, allow_null=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'phone', 'last_visit', 'color',
            'last_name', 'gender', 'birth_date', 'group',
            'tax_id', 'tax_branch', 'address', 'email',
        ]

    def validate_birth_date(self, value):
        return value or None

    def to_internal_value(self, data):
        if isinstance(data, dict) and data.get('birth_date') == '':
            data = {**data, 'birth_date': None}
        return super().to_internal_value(data)


class SettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Settings
        fields = '__all__'


class OrderItemSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(required=False, allow_null=True)
    category_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = OrderItem
        fields = ['product_id', 'name', 'price', 'qty', 'discount', 'category_id', 'category_name']


class OrderSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    items = OrderItemSerializer(many=True)
    customer_id = serializers.UUIDField(required=False, allow_null=True)
    # Branch is the *Order's* branch, not the global Settings.branch.
    # Needed so the reprint flow can render the correct branch label
    # on the receipt (otherwise every receipt prints "Main").  Both
    # fields are method-based to handle Order.branch being null
    # without raising AttributeError.
    branch_id = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()

    def get_branch_id(self, obj):
        return str(obj.branch_id) if obj.branch_id else None

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else ""

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'items',
            'branch_id', 'branch_name',
            'subtotal', 'discount_type', 'discount_value', 'discount_amount', 'total',
            'vat_amount', 'processing_fee', 'processing_fee_vat',
            'payment_method', 'paid_amount', 'change',
            'status', 'source',
            # The number the customer is called by. Server-assigned per branch
            # per day; the receipt used to derive it from the last two digits of
            # the global order_number, which collided across branches.
            'queue_number',
            'customer_id', 'customer_name',
            'beam_charge_id', 'beam_link_id', 'omise_link_id', 'omise_charge_id',
            'delivery_provider', 'delivery_status',
            'created_at', 'created_time', 'staff',
            'voided_by', 'voided_at',
            # Buyer details captured when a full tax invoice is issued for this
            # bill.  Exposed read-only so Transactions can tell which bills
            # already have one and prefill the form on a re-issue; writes go
            # through the dedicated /orders/<id>/tax-invoice endpoint.
            'pos_tax_invoice',
        ]
        read_only_fields = [
            'order_number', 'queue_number', 'created_at', 'voided_by', 'voided_at',
            'pos_tax_invoice',
        ]


class ParkedOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParkedOrder
        fields = ['id', 'name', 'items', 'customer_id', 'customer_name', 'created_at']
        read_only_fields = ['created_at']


class StockMovementSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField()

    class Meta:
        model = StockMovement
        fields = [
            'id', 'product_id', 'product_name', 'type', 'qty',
            'note', 'document_no', 'created_at',
        ]
        read_only_fields = ['created_at', 'product_name', 'document_no']


class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = '__all__'


class ShiftMovementSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftMovement
        fields = '__all__'


class DrawerCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = DrawerCategory
        fields = ['id', 'type', 'name', 'name_th', 'sort_order', 'active']


class StockOutReasonSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockOutReason
        fields = ['id', 'name', 'name_th', 'sort_order', 'active']


class StockDocumentItemSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = StockDocumentItem
        fields = [
            'id', 'product_id', 'barcode', 'product_name',
            'qty', 'price', 'discount', 'total',
            'before_qty', 'reconcile_qty',
        ]
        read_only_fields = ['id']


class StockDocumentSerializer(serializers.ModelSerializer):
    items = StockDocumentItemSerializer(many=True)

    class Meta:
        model = StockDocument
        fields = [
            'id', 'type', 'document_no', 'document_name', 'adjust_type',
            'ref_no', 'vendor', 'receiver', 'reason',
            'note', 'tax_included', 'avg_cost',
            'subtotal', 'discount', 'tax', 'total',
            'created_by', 'created_at', 'items',
        ]
        read_only_fields = ['id', 'document_no', 'created_by', 'created_at']
