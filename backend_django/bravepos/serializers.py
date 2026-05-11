"""DRF serializers — shape the JSON over the wire so it matches what the
existing frontend already sends and expects."""
from rest_framework import serializers

from .models import (
    Category, Product, StockMovement, Customer,
    Settings, Order, OrderItem, ParkedOrder, Shift, ShiftMovement,
)


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
        fields = ['product_id', 'name', 'price', 'qty', 'category_id', 'category_name']


class OrderSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    items = OrderItemSerializer(many=True)
    customer_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'items',
            'subtotal', 'discount_type', 'discount_value', 'discount_amount', 'total',
            'payment_method', 'paid_amount', 'change',
            'status', 'source',
            'customer_id', 'customer_name',
            'beam_charge_id', 'delivery_provider', 'delivery_status',
            'created_at', 'created_time', 'staff',
        ]
        read_only_fields = ['order_number', 'created_at']


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
