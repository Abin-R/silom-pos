"""DRF serializers — shape the JSON over the wire so it matches what the
existing frontend already sends and expects."""
from rest_framework import serializers

from .models import (
    Branch, Category, Product, StockMovement, Customer,
    Settings, Order, OrderItem, ParkedOrder, Shift, ShiftMovement,
    DrawerCategory, StockDocument, StockDocumentItem,
)


class BranchSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=False, required=False)
    # Surface the first cashier's email so the edit screen can display + update
    # it without an extra round trip.  One-cashier-per-branch is the assumption
    # baked into the create flow; if admin manually assigns multiple, we just
    # show the first.
    cashier_email = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = ['id', 'name', 'code', 'address', 'phone', 'tax_id', 'pos_id',
                  'active', 'created_at', 'cashier_email']
        read_only_fields = ['created_at', 'cashier_email']

    def get_cashier_email(self, obj):
        s = obj.staff.filter(role='cashier').order_by('created_at').first()
        return s.email if s else ''


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


class CustomerSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=False, required=False)

    class Meta:
        model = Customer
        fields = ['id', 'name', 'phone', 'last_visit', 'color']


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
            'payment_method', 'paid_amount', 'change',
            'status', 'source',
            'customer_id', 'customer_name',
            'beam_charge_id', 'delivery_provider', 'delivery_status',
            'created_at', 'created_time', 'staff',
            'voided_by', 'voided_at',
        ]
        read_only_fields = ['order_number', 'created_at', 'voided_by', 'voided_at']


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
            'ref_no', 'vendor', 'receiver',
            'note', 'tax_included', 'avg_cost',
            'subtotal', 'discount', 'tax', 'total',
            'created_by', 'created_at', 'items',
        ]
        read_only_fields = ['id', 'document_no', 'created_by', 'created_at']
