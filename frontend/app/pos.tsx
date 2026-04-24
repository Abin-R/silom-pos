import { useEffect, useState, useMemo, useCallback } from "react";
import {
  View,
  Text,
  StyleSheet,
  TouchableOpacity,
  FlatList,
  Image,
  TextInput,
  Modal,
  ScrollView,
  ActivityIndicator,
  Platform,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter, useLocalSearchParams } from "expo-router";
import { Ionicons, MaterialCommunityIcons } from "@expo/vector-icons";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;

// ---- Types ----
type Category = { id: string; name: string; name_th?: string; color: string; order: number };
type Product = {
  id: string;
  name: string;
  name_th?: string;
  price: number;
  category_id: string;
  image_url: string;
  is_favorite: boolean;
};
type CartItem = { product_id: string; name: string; price: number; qty: number };
type Customer = { id: string; name: string; phone?: string; last_visit?: string; color: string };
type Order = {
  id: string;
  order_number: string;
  items: CartItem[];
  subtotal: number;
  total: number;
  status: string;
  source: string;
  delivery_provider?: string;
  delivery_status?: string;
  created_time: string;
};
type ParkedOrder = {
  id: string;
  label: string;
  items: CartItem[];
  subtotal: number;
  created_at: string;
};

const THB = (n: number) => `฿${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function POS() {
  const router = useRouter();
  const { staff } = useLocalSearchParams<{ staff?: string }>();
  const { width } = useWindowDimensions();
  const isWide = width >= 900;
  const isMid = width >= 600;
  const gridCols = isWide ? 4 : isMid ? 3 : 2;

  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("favorite");
  const [cart, setCart] = useState<CartItem[]>([]);
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [discountType, setDiscountType] = useState<"none" | "amount" | "percent">("none");
  const [discountValue, setDiscountValue] = useState<number>(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [parkedCount, setParkedCount] = useState(0);
  const [orderHubCount, setOrderHubCount] = useState(0);

  // modal states
  const [showPayment, setShowPayment] = useState(false);
  const [showDiscount, setShowDiscount] = useState(false);
  const [showCustomer, setShowCustomer] = useState(false);
  const [showOrderHub, setShowOrderHub] = useState(false);
  const [showParked, setShowParked] = useState(false);
  const [showCart, setShowCart] = useState(false); // mobile cart sheet
  const [showSuccess, setShowSuccess] = useState<null | {
    order_number: string;
    total: number;
    paid: number;
    change: number;
    method: string;
  }>(null);
  const [showDrawer, setShowDrawer] = useState(false);

  // Load initial data
  useEffect(() => {
    (async () => {
      try {
        const [catsRes, prodsRes] = await Promise.all([
          fetch(`${API}/categories`),
          fetch(`${API}/products`),
        ]);
        const cats: Category[] = await catsRes.json();
        const prods: Product[] = await prodsRes.json();
        setCategories(cats);
        setProducts(prods);
      } catch (e) {
        console.error("Load failed", e);
      } finally {
        setLoading(false);
      }
      refreshBadges();
    })();
  }, []);

  const refreshBadges = async () => {
    try {
      const [po, oh] = await Promise.all([
        fetch(`${API}/parked-orders`).then((r) => r.json()),
        fetch(`${API}/orders?source=delivery`).then((r) => r.json()),
      ]);
      setParkedCount(po.length);
      // count active (non-delivered) delivery orders
      const active = oh.filter((o: Order) => o.delivery_status === "DELIVERING").length;
      setOrderHubCount(active);
    } catch {}
  };

  // Derived
  const filteredProducts = useMemo(() => {
    let list = products;
    if (search.trim()) {
      const s = search.toLowerCase();
      list = list.filter((p) => p.name.toLowerCase().includes(s) || p.name_th?.includes(search));
    } else if (activeCat === "favorite") {
      list = list.filter((p) => p.is_favorite);
    } else {
      list = list.filter((p) => p.category_id === activeCat);
    }
    return list;
  }, [products, activeCat, search]);

  const subtotal = useMemo(
    () => cart.reduce((s, i) => s + i.price * i.qty, 0),
    [cart]
  );
  const discountAmount = useMemo(() => {
    if (discountType === "amount") return Math.min(discountValue, subtotal);
    if (discountType === "percent") return (subtotal * discountValue) / 100;
    return 0;
  }, [discountType, discountValue, subtotal]);
  const total = Math.max(0, subtotal - discountAmount);
  const cartCount = cart.reduce((s, i) => s + i.qty, 0);

  // Cart ops
  const addToCart = useCallback((p: Product) => {
    setCart((c) => {
      const ex = c.find((i) => i.product_id === p.id);
      if (ex) return c.map((i) => (i.product_id === p.id ? { ...i, qty: i.qty + 1 } : i));
      return [...c, { product_id: p.id, name: p.name, price: p.price, qty: 1 }];
    });
  }, []);

  const updateQty = (pid: string, delta: number) => {
    setCart((c) =>
      c
        .map((i) => (i.product_id === pid ? { ...i, qty: i.qty + delta } : i))
        .filter((i) => i.qty > 0)
    );
  };

  const removeItem = (pid: string) =>
    setCart((c) => c.filter((i) => i.product_id !== pid));

  const clearCart = () => {
    setCart([]);
    setCustomer(null);
    setDiscountType("none");
    setDiscountValue(0);
  };

  const handlePaySuccess = async (method: string, paid: number) => {
    try {
      const res = await fetch(`${API}/orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: cart,
          subtotal,
          discount_type: discountType,
          discount_value: discountValue,
          discount_amount: discountAmount,
          total,
          payment_method: method,
          paid_amount: paid,
          change: Math.max(0, paid - total),
          source: "table",
          customer_id: customer?.id,
          customer_name: customer?.name,
        }),
      });
      const order = await res.json();
      setShowPayment(false);
      setShowSuccess({
        order_number: order.order_number,
        total,
        paid,
        change: Math.max(0, paid - total),
        method,
      });
      refreshBadges();
    } catch (e) {
      console.error("checkout fail", e);
    }
  };

  const parkCurrentOrder = async () => {
    if (cart.length === 0) return;
    await fetch(`${API}/parked-orders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: `Order ${new Date().toLocaleTimeString().slice(0, 5)}`,
        items: cart,
        subtotal,
      }),
    });
    clearCart();
    setShowParked(false);
    refreshBadges();
  };

  if (loading) {
    return (
      <View style={[styles.root, { alignItems: "center", justifyContent: "center" }]}>
        <ActivityIndicator color="#00B14F" size="large" />
      </View>
    );
  }

  return (
    <SafeAreaView style={styles.root} edges={["top", "bottom"]}>
      {/* ============ TOP BAR ============ */}
      <View style={styles.topBar} testID="top-bar">
        <TouchableOpacity
          style={styles.menuBtn}
          onPress={() => setShowDrawer(true)}
          testID="menu-btn"
        >
          <Ionicons name="menu" size={24} color="#0F172A" />
        </TouchableOpacity>

        {isMid && (
          <View style={styles.searchWrap}>
            <Ionicons name="search" size={18} color="#94A3B8" />
            <TextInput
              placeholder="Search Products"
              placeholderTextColor="#94A3B8"
              style={styles.searchInput}
              value={search}
              onChangeText={setSearch}
              testID="product-search"
            />
          </View>
        )}
        {!isMid && <View style={{ flex: 1 }} />}

        <ToolbarIcon
          icon="globe-outline"
          label="Order Hub"
          badge={orderHubCount}
          onPress={() => setShowOrderHub(true)}
          testId="toolbar-order-hub"
          compact={!isWide}
        />
        {isMid && (
          <ToolbarIcon
            icon="pricetag-outline"
            label="Discount"
            onPress={() => setShowDiscount(true)}
            testId="toolbar-discount"
            disabled={cart.length === 0}
            compact={!isWide}
          />
        )}
        <ToolbarIcon
          icon="bookmark-outline"
          label="Save"
          badge={parkedCount}
          onPress={() => setShowParked(true)}
          testId="toolbar-parked"
          compact={!isWide}
        />
        <ToolbarIcon
          icon="person-outline"
          label="Customer"
          onPress={() => setShowCustomer(true)}
          testId="toolbar-customer"
          compact={!isWide}
        />
        {isWide && (
          <View style={styles.staffChip}>
            <Ionicons name="person-circle" size={22} color="#00B14F" />
            <Text style={styles.staffText}>{staff || "Admin"}</Text>
          </View>
        )}
        <TouchableOpacity
          style={styles.adminBtn}
          onPress={() => router.replace({ pathname: "/admin", params: { staff: staff || "Admin" } })}
          testID="goto-admin"
        >
          <Ionicons name="grid-outline" size={18} color="#00B14F" />
          {isWide && <Text style={styles.adminBtnText}>Admin</Text>}
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.logoutBtn}
          onPress={() => router.replace("/")}
          testID="logout-btn"
        >
          <Ionicons name="log-out-outline" size={20} color="#EF4444" />
        </TouchableOpacity>
      </View>

      {/* Mobile search bar (below top bar on narrow) */}
      {!isMid && (
        <View style={styles.mobileSearchWrap}>
          <Ionicons name="search" size={18} color="#94A3B8" />
          <TextInput
            placeholder="Search Products"
            placeholderTextColor="#94A3B8"
            style={styles.searchInput}
            value={search}
            onChangeText={setSearch}
            testID="product-search-mobile"
          />
        </View>
      )}

      {/* ============ MAIN LAYOUT ============ */}
      <View style={[styles.main, !isWide && styles.mainStacked]}>
        {/* Category rail — vertical on wide, horizontal scroll on narrow */}
        {isWide ? (
          <View style={styles.leftRail} testID="category-rail">
            <ScrollView showsVerticalScrollIndicator={false}>
              <CatPill
                label="Favorite"
                active={activeCat === "favorite"}
                onPress={() => {
                  setActiveCat("favorite");
                  setSearch("");
                }}
                testId="cat-favorite"
              />
              {categories
                .filter((c) => c.name !== "Favorite")
                .map((c) => (
                  <CatPill
                    key={c.id}
                    label={c.name}
                    sub={c.name_th}
                    active={activeCat === c.id}
                    onPress={() => {
                      setActiveCat(c.id);
                      setSearch("");
                    }}
                    testId={`cat-${c.id}`}
                  />
                ))}
            </ScrollView>
          </View>
        ) : (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            style={styles.catStrip}
            contentContainerStyle={{ paddingHorizontal: 12, gap: 8 }}
            testID="category-rail"
          >
            <CatChip
              label="Favorite"
              active={activeCat === "favorite"}
              onPress={() => {
                setActiveCat("favorite");
                setSearch("");
              }}
              testId="cat-favorite"
            />
            {categories
              .filter((c) => c.name !== "Favorite")
              .map((c) => (
                <CatChip
                  key={c.id}
                  label={c.name}
                  active={activeCat === c.id}
                  onPress={() => {
                    setActiveCat(c.id);
                    setSearch("");
                  }}
                  testId={`cat-${c.id}`}
                />
              ))}
          </ScrollView>
        )}

        {/* Center product grid */}
        <View style={styles.center}>
          <FlatList
            key={`grid-${gridCols}`}
            data={filteredProducts}
            keyExtractor={(i) => i.id}
            numColumns={gridCols}
            contentContainerStyle={{ padding: 12, paddingBottom: 120 }}
            columnWrapperStyle={{ gap: 10 }}
            ItemSeparatorComponent={() => <View style={{ height: 10 }} />}
            ListEmptyComponent={
              <View style={styles.empty}>
                <Ionicons name="search-outline" size={40} color="#CBD5E1" />
                <Text style={styles.emptyText}>No products found</Text>
              </View>
            }
            renderItem={({ item }) => (
              <TouchableOpacity
                style={[styles.productCard, { maxWidth: `${100 / gridCols - 2}%` }]}
                onPress={() => addToCart(item)}
                activeOpacity={0.85}
                testID={`product-${item.id}`}
              >
                <Image source={{ uri: item.image_url }} style={styles.productImg} />
                <View style={styles.productInfo}>
                  <Text style={styles.productName} numberOfLines={2}>
                    {item.name}
                  </Text>
                  <Text style={styles.productPrice}>{THB(item.price)}</Text>
                </View>
              </TouchableOpacity>
            )}
          />
          {isWide && (
            <View style={styles.scanBar}>
              <Ionicons name="barcode-outline" size={20} color="#475569" />
              <Text style={styles.scanText}>Scan Barcode</Text>
            </View>
          )}
        </View>

        {/* Right cart — sidebar on wide, floating button + modal on narrow */}
        {isWide ? (
          <CartSidebar
            cart={cart}
            customer={customer}
            subtotal={subtotal}
            discountType={discountType}
            discountValue={discountValue}
            discountAmount={discountAmount}
            total={total}
            cartCount={cartCount}
            onClear={clearCart}
            onRemoveCustomer={() => setCustomer(null)}
            onPay={() => setShowPayment(true)}
            onInc={(pid) => updateQty(pid, 1)}
            onDec={(pid) => updateQty(pid, -1)}
            onRemove={removeItem}
          />
        ) : (
          cart.length > 0 && (
            <TouchableOpacity
              style={styles.fabCart}
              onPress={() => setShowCart(true)}
              testID="fab-cart"
            >
              <View style={styles.fabLeft}>
                <MaterialCommunityIcons name="cart" size={22} color="#FFFFFF" />
                <View style={styles.fabBadge}>
                  <Text style={styles.fabBadgeText}>{cartCount}</Text>
                </View>
              </View>
              <View style={styles.fabMid}>
                <Text style={styles.fabTotalLabel}>Total</Text>
                <Text style={styles.fabTotal}>{THB(total)}</Text>
              </View>
              <View style={styles.fabRight}>
                <Text style={styles.fabView}>View</Text>
                <Ionicons name="chevron-up" size={18} color="#FFFFFF" />
              </View>
            </TouchableOpacity>
          )
        )}
      </View>

      {/* Mobile cart modal */}
      <Modal
        visible={showCart && !isWide}
        transparent
        animationType="slide"
        onRequestClose={() => setShowCart(false)}
      >
        <View style={styles.cartSheetOverlay}>
          <TouchableOpacity style={{ flex: 1 }} onPress={() => setShowCart(false)} />
          <View style={styles.cartSheet}>
            <View style={styles.cartSheetHandle} />
            <View style={styles.cartSheetHeader}>
              <Text style={styles.cartSheetTitle}>Current Order</Text>
              <TouchableOpacity onPress={() => setShowCart(false)}>
                <Ionicons name="close" size={24} color="#475569" />
              </TouchableOpacity>
            </View>
            <CartSidebar
              cart={cart}
              customer={customer}
              subtotal={subtotal}
              discountType={discountType}
              discountValue={discountValue}
              discountAmount={discountAmount}
              total={total}
              cartCount={cartCount}
              onClear={() => {
                clearCart();
                setShowCart(false);
              }}
              onRemoveCustomer={() => setCustomer(null)}
              onPay={() => {
                setShowCart(false);
                setShowPayment(true);
              }}
              onInc={(pid) => updateQty(pid, 1)}
              onDec={(pid) => updateQty(pid, -1)}
              onRemove={removeItem}
              embedded
            />
            <TouchableOpacity
              style={styles.mobileDiscBtn}
              onPress={() => {
                setShowCart(false);
                setShowDiscount(true);
              }}
              disabled={cart.length === 0}
              testID="mobile-discount"
            >
              <Ionicons name="pricetag-outline" size={16} color="#00B14F" />
              <Text style={styles.mobileDiscText}>Add Discount</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>

      {/* ============ MODALS ============ */}
      <PaymentModal
        visible={showPayment}
        total={total}
        onClose={() => setShowPayment(false)}
        onPay={handlePaySuccess}
      />
      <DiscountModal
        visible={showDiscount}
        onClose={() => setShowDiscount(false)}
        onApply={(t, v) => {
          setDiscountType(t);
          setDiscountValue(v);
          setShowDiscount(false);
        }}
      />
      <CustomerModal
        visible={showCustomer}
        onClose={() => setShowCustomer(false)}
        onSelect={(c) => {
          setCustomer(c);
          setShowCustomer(false);
        }}
      />
      <OrderHubModal
        visible={showOrderHub}
        onClose={() => {
          setShowOrderHub(false);
          refreshBadges();
        }}
      />
      <ParkedOrdersModal
        visible={showParked}
        onClose={() => setShowParked(false)}
        currentCart={cart}
        onPark={parkCurrentOrder}
        onRetrieve={(items) => {
          setCart(items);
          setShowParked(false);
        }}
      />
      <SuccessModal
        data={showSuccess}
        onClose={() => {
          setShowSuccess(null);
          clearCart();
        }}
      />
      <DrawerModal visible={showDrawer} onClose={() => setShowDrawer(false)} />
    </SafeAreaView>
  );
}

// ---------- Sub components ----------
function ToolbarIcon({
  icon,
  label,
  onPress,
  badge,
  testId,
  disabled,
  compact,
}: {
  icon: any;
  label: string;
  onPress: () => void;
  badge?: number;
  testId: string;
  disabled?: boolean;
  compact?: boolean;
}) {
  return (
    <TouchableOpacity
      style={[
        styles.tbItem,
        compact && styles.tbItemCompact,
        disabled && { opacity: 0.4 },
      ]}
      onPress={onPress}
      disabled={disabled}
      testID={testId}
    >
      <View>
        <Ionicons name={icon} size={compact ? 20 : 22} color="#0F172A" />
        {badge && badge > 0 ? (
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{badge}</Text>
          </View>
        ) : null}
      </View>
      {!compact && <Text style={styles.tbLabel}>{label}</Text>}
    </TouchableOpacity>
  );
}

function CatChip({
  label,
  active,
  onPress,
  testId,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
  testId: string;
}) {
  return (
    <TouchableOpacity
      style={[styles.catChip, active && styles.catChipActive]}
      onPress={onPress}
      testID={testId}
    >
      <Text style={[styles.catChipText, active && styles.catChipTextActive]}>{label}</Text>
    </TouchableOpacity>
  );
}

function CatPill({
  label,
  sub,
  active,
  onPress,
  testId,
}: {
  label: string;
  sub?: string;
  active: boolean;
  onPress: () => void;
  testId: string;
}) {
  return (
    <TouchableOpacity
      style={[styles.catPill, active && styles.catPillActive]}
      onPress={onPress}
      testID={testId}
    >
      <Text style={[styles.catText, active && styles.catTextActive]} numberOfLines={2}>
        {label}
      </Text>
      {sub && (
        <Text style={[styles.catSub, active && styles.catSubActive]} numberOfLines={1}>
          {sub}
        </Text>
      )}
    </TouchableOpacity>
  );
}

// ---------- Cart Sidebar (shared between desktop sidebar + mobile sheet) ----------
function CartSidebar({
  cart,
  customer,
  subtotal,
  discountType,
  discountValue,
  discountAmount,
  total,
  cartCount,
  onClear,
  onRemoveCustomer,
  onPay,
  onInc,
  onDec,
  onRemove,
  embedded,
}: {
  cart: CartItem[];
  customer: Customer | null;
  subtotal: number;
  discountType: "none" | "amount" | "percent";
  discountValue: number;
  discountAmount: number;
  total: number;
  cartCount: number;
  onClear: () => void;
  onRemoveCustomer: () => void;
  onPay: () => void;
  onInc: (pid: string) => void;
  onDec: (pid: string) => void;
  onRemove: (pid: string) => void;
  embedded?: boolean;
}) {
  return (
    <View style={[styles.rightCart, embedded && styles.rightCartEmbedded]} testID="cart-sidebar">
      <View style={styles.cartHeader}>
        <View style={styles.tablePill}>
          <Ionicons name="restaurant-outline" size={14} color="#0F172A" />
          <Text style={styles.tableText}>Tables</Text>
        </View>
        {customer && (
          <View style={styles.custChip}>
            <View style={[styles.custDot, { backgroundColor: customer.color }]}>
              <Text style={styles.custInitial}>
                {customer.name[0]?.toUpperCase()}
              </Text>
            </View>
            <Text style={styles.custName} numberOfLines={1}>
              {customer.name}
            </Text>
            <TouchableOpacity onPress={onRemoveCustomer} testID="remove-customer">
              <Ionicons name="close" size={16} color="#94A3B8" />
            </TouchableOpacity>
          </View>
        )}
      </View>

      <View style={styles.totalBox}>
        <Text style={styles.totalLabel}>Sub Total</Text>
        <Text style={styles.subTotalVal}>{THB(subtotal)}</Text>
        {discountType !== "none" && (
          <View style={styles.discRow}>
            <Text style={styles.discLabel}>
              Discount {discountType === "percent" ? `(${discountValue}%)` : ""}
            </Text>
            <Text style={styles.discVal}>-{THB(discountAmount)}</Text>
          </View>
        )}
        <View style={styles.totalRow}>
          <Text style={styles.thbText}>THB</Text>
          <Text style={styles.totalVal} testID="cart-total">
            {total.toFixed(2)}
          </Text>
        </View>
      </View>

      <TouchableOpacity
        style={[styles.payBtn, cart.length === 0 && styles.payBtnDisabled]}
        disabled={cart.length === 0}
        onPress={onPay}
        testID="pay-btn"
      >
        <Text style={styles.payBtnText}>Pay</Text>
      </TouchableOpacity>

      <View style={styles.cartListHeader}>
        <Text style={styles.cartListTitle}>Store</Text>
        <Text style={styles.cartListCount}>
          {cart.length} Item{cart.length !== 1 ? "s" : ""} / {cartCount} pcs.
        </Text>
      </View>

      <FlatList
        data={cart}
        keyExtractor={(i) => i.product_id}
        style={{ maxHeight: embedded ? 240 : undefined }}
        ListEmptyComponent={
          <View style={styles.emptyCart}>
            <MaterialCommunityIcons name="cart-outline" size={40} color="#CBD5E1" />
            <Text style={styles.emptyCartText}>Cart is empty</Text>
            <Text style={styles.emptyCartSub}>Tap a product to add</Text>
          </View>
        }
        renderItem={({ item }) => (
          <View style={styles.cartItem} testID={`cart-item-${item.product_id}`}>
            <View style={{ flex: 1 }}>
              <Text style={styles.cartItemName} numberOfLines={2}>
                {item.name}
              </Text>
              <Text style={styles.cartItemPrice}>
                {THB(item.price)} × {item.qty}
              </Text>
            </View>
            <View style={styles.qtyCtrl}>
              <TouchableOpacity
                style={styles.qtyBtn}
                onPress={() => onDec(item.product_id)}
                testID={`qty-dec-${item.product_id}`}
              >
                <Ionicons name="remove" size={16} color="#0F172A" />
              </TouchableOpacity>
              <Text style={styles.qtyText}>{item.qty}</Text>
              <TouchableOpacity
                style={styles.qtyBtn}
                onPress={() => onInc(item.product_id)}
                testID={`qty-inc-${item.product_id}`}
              >
                <Ionicons name="add" size={16} color="#0F172A" />
              </TouchableOpacity>
            </View>
            <TouchableOpacity
              onPress={() => onRemove(item.product_id)}
              style={styles.trashBtn}
              testID={`remove-${item.product_id}`}
            >
              <Ionicons name="trash-outline" size={18} color="#EF4444" />
            </TouchableOpacity>
          </View>
        )}
      />

      {cart.length > 0 && (
        <TouchableOpacity style={styles.clearBtn} onPress={onClear} testID="clear-cart">
          <Ionicons name="close-circle-outline" size={16} color="#EF4444" />
          <Text style={styles.clearBtnText}>Clear cart</Text>
        </TouchableOpacity>
      )}
    </View>
  );
}

// ---------- Payment Modal ----------
function PaymentModal({
  visible,
  total,
  onClose,
  onPay,
}: {
  visible: boolean;
  total: number;
  onClose: () => void;
  onPay: (method: string, paid: number) => void;
}) {
  const [amount, setAmount] = useState("");
  const [method, setMethod] = useState("Easy Pay");

  useEffect(() => {
    if (visible) {
      setAmount("");
      setMethod("Easy Pay");
    }
  }, [visible]);

  const methods = [
    { key: "Easy Pay", icon: "qr-code-outline" as const },
    { key: "Credit", icon: "card-outline" as const },
    { key: "PromptPay", icon: "phone-portrait-outline" as const },
    { key: "QR Kbank", icon: "scan-outline" as const },
    { key: "EDC", icon: "print-outline" as const },
    { key: "Custom", icon: "wallet-outline" as const },
  ];

  const customOptions = [
    "EDC Kbank", "EDC Bangkok", "Brave Brand Co.,Ltd",
    "Thai Dot Com Pay", "คนละครึ่ง", "EDC SCB", "QR",
  ];
  const [customPick, setCustomPick] = useState("");
  const [orderRef, setOrderRef] = useState("");
  useEffect(() => { if (visible) { setCustomPick(""); setOrderRef(""); } }, [visible]);

  const paid = amount ? parseFloat(amount) : total;
  const change = Math.max(0, paid - total);
  const canPay = paid >= total;

  const onKey = (k: string) => {
    if (k === "clear") setAmount("");
    else if (k === "back") setAmount((a) => a.slice(0, -1));
    else if (k === ".") {
      if (!amount.includes(".")) setAmount((a) => (a || "0") + ".");
    } else setAmount((a) => (a === "0" ? k : a + k));
  };

  const quicks = [1000, 500, 100, 50, 20];
  const keys = ["7", "8", "9", "4", "5", "6", "1", "2", "3", "0", ".", "back"];

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.paymentModal} testID="payment-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose} testID="close-payment">
              <Ionicons name="close" size={26} color="#475569" />
            </TouchableOpacity>
            <Text style={styles.modalTitle}>Payment</Text>
            <View style={{ width: 26 }} />
          </View>

          <View style={styles.paymentBody}>
            {/* Methods */}
            <View style={styles.methodsCol}>
              {methods.map((m) => (
                <TouchableOpacity
                  key={m.key}
                  style={[styles.methodBtn, method === m.key && styles.methodBtnActive]}
                  onPress={() => setMethod(m.key)}
                  testID={`pay-method-${m.key}`}
                >
                  <Ionicons
                    name={m.icon}
                    size={24}
                    color={method === m.key ? "#00B14F" : "#475569"}
                  />
                  <Text
                    style={[styles.methodText, method === m.key && styles.methodTextActive]}
                  >
                    {m.key}
                  </Text>
                </TouchableOpacity>
              ))}
            </View>

            {/* Dynamic center content per method */}
            {method === "QR Kbank" || method === "PromptPay" ? (
              <View style={styles.qrPane} testID="qr-pane">
                <View style={styles.qrHeader}>
                  <Text style={styles.qrHeaderText}>
                    {method === "QR Kbank" ? "Thai QR Payment" : "PromptPay"}
                  </Text>
                </View>
                <View style={styles.qrBrandRow}>
                  <Text style={styles.brandPill}>PromptPay</Text>
                  <Text style={styles.brandPill}>VISA</Text>
                  <Text style={styles.brandPill}>MC</Text>
                  <Text style={styles.brandPill}>UnionPay</Text>
                </View>
                <Text style={styles.qrHint}>
                  Support payment type Thai QR (PromptPay), Credit Card
                </Text>
                <Text style={styles.qrBy}>
                  By {method === "QR Kbank" ? "KBank" : "Bank"}
                </Text>
                <View style={styles.qrBox}>
                  <Ionicons name="qr-code" size={110} color="#0F172A" />
                </View>
                <Text style={styles.qrAmount}>{THB(total)}</Text>
              </View>
            ) : method === "Custom" ? (
              <View style={styles.customPane} testID="custom-pane">
                <TextInput
                  placeholder="Order Ref.  (Optional)"
                  style={styles.customRef}
                  value={orderRef}
                  onChangeText={setOrderRef}
                  placeholderTextColor="#94A3B8"
                  testID="custom-order-ref"
                />
                <View style={styles.customAmountRow}>
                  <Text style={styles.customAmountLabel}>Amount</Text>
                  <Text style={styles.customAmountVal}>{total.toFixed(2)}</Text>
                </View>
                <View style={styles.customGrid}>
                  {customOptions.map((opt) => (
                    <TouchableOpacity
                      key={opt}
                      style={[styles.customOption, customPick === opt && styles.customOptionActive]}
                      onPress={() => setCustomPick(opt)}
                      testID={`custom-${opt}`}
                    >
                      <View style={[styles.radio, customPick === opt && styles.radioActive]}>
                        {customPick === opt && <View style={styles.radioInner} />}
                      </View>
                      <Text style={styles.customOptionText}>{opt}</Text>
                    </TouchableOpacity>
                  ))}
                </View>
              </View>
            ) : (
              <View style={styles.padCol}>
                <View style={styles.amountDisplay}>
                  <Text style={styles.thbSmall}>THB</Text>
                  <Text style={styles.amountText} testID="amount-display">
                    {amount || "0"}
                  </Text>
                </View>
                <View style={styles.padGrid}>
                  {keys.map((k) => (
                    <TouchableOpacity
                      key={k}
                      style={styles.padBtn}
                      onPress={() => onKey(k)}
                      testID={`pad-${k}`}
                    >
                      {k === "back" ? (
                        <Ionicons name="backspace-outline" size={22} color="#EF4444" />
                      ) : (
                        <Text style={styles.padText}>{k}</Text>
                      )}
                    </TouchableOpacity>
                  ))}
                </View>
                <TouchableOpacity
                  style={styles.clearPadBtn}
                  onPress={() => onKey("clear")}
                  testID="pad-clear"
                >
                  <Text style={styles.clearPadText}>Clear</Text>
                </TouchableOpacity>
              </View>
            )}

            {/* Right column: totals */}
            <View style={styles.quickCol}>
              <TouchableOpacity
                style={styles.netBox}
                onPress={() => setAmount(String(total))}
                testID="net-total"
              >
                <Text style={styles.netLabel}>Net Total</Text>
                <Text style={styles.netHint}>Tap to equal Total</Text>
                <Text style={styles.netVal}>{THB(total)}</Text>
              </TouchableOpacity>
              {method !== "QR Kbank" && method !== "PromptPay" && method !== "Custom" &&
                quicks.map((q) => (
                  <TouchableOpacity
                    key={q}
                    style={styles.quickBtn}
                    onPress={() =>
                      setAmount((a) => {
                        const cur = parseFloat(a || "0");
                        return String(cur + q);
                      })
                    }
                    testID={`quick-${q}`}
                  >
                    <Text style={styles.quickText}>{q.toLocaleString()}</Text>
                  </TouchableOpacity>
                ))}
              {(method === "QR Kbank" || method === "PromptPay") && (
                <View style={styles.qrInstructBox}>
                  <Ionicons name="information-circle-outline" size={18} color="#475569" />
                  <Text style={styles.qrInstructText}>
                    Ask customer to scan the QR on screen to complete payment.
                  </Text>
                </View>
              )}
              {method === "Custom" && (
                <View style={styles.summaryBox}>
                  <Text style={styles.netLabel}>Summary</Text>
                  <Text style={styles.summaryVal}>{THB(customPick ? total : 0)}</Text>
                </View>
              )}
            </View>
          </View>

          <View style={styles.paymentFooter}>
            <View style={styles.changeBox}>
              <Text style={styles.changeLabel}>
                {method === "Custom" ? "Summary" : method === "QR Kbank" || method === "PromptPay" ? "Awaiting scan" : "Change"}
              </Text>
              <Text style={styles.changeVal}>
                {method === "Custom" || method === "QR Kbank" || method === "PromptPay" ? THB(total) : THB(change)}
              </Text>
            </View>
            <TouchableOpacity
              style={[
                styles.confirmBtn,
                !(method === "QR Kbank" || method === "PromptPay" || (method === "Custom" && customPick) || canPay) && styles.payBtnDisabled,
              ]}
              disabled={!(method === "QR Kbank" || method === "PromptPay" || (method === "Custom" && customPick) || canPay)}
              onPress={() => {
                const finalMethod = method === "Custom" && customPick ? `Custom · ${customPick}` : method;
                const finalPaid = (method === "QR Kbank" || method === "PromptPay" || method === "Custom") ? total : paid;
                onPay(finalMethod, finalPaid);
              }}
              testID="confirm-payment"
            >
              <Text style={styles.confirmText}>
                {method === "QR Kbank" || method === "PromptPay" ? "Mark Paid" : method === "Custom" ? "Payment Confirm" : "Confirm Payment"}
              </Text>
              <Ionicons name="arrow-forward" size={20} color="#fff" />
            </TouchableOpacity>
          </View>
        </View>
      </View>
    </Modal>
  );
}

// ---------- Discount Modal ----------
function DiscountModal({
  visible,
  onClose,
  onApply,
}: {
  visible: boolean;
  onClose: () => void;
  onApply: (t: "amount" | "percent" | "none", v: number) => void;
}) {
  const [mode, setMode] = useState<"amount" | "percent">("percent");
  const [val, setVal] = useState("");

  useEffect(() => {
    if (visible) {
      setMode("percent");
      setVal("");
    }
  }, [visible]);

  const onKey = (k: string) => {
    if (k === "back") setVal((v) => v.slice(0, -1));
    else if (k === ".") {
      if (!val.includes(".")) setVal((v) => (v || "0") + ".");
    } else setVal((v) => (v === "0" ? k : v + k));
  };

  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "back"];
  const quicks = [5, 10, 15, 20];

  const apply = () => {
    const n = parseFloat(val) || 0;
    if (n === 0) {
      onApply("none", 0);
    } else {
      onApply(mode, n);
    }
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.discountModal} testID="discount-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose} testID="close-discount">
              <Ionicons name="chevron-back" size={26} color="#EF4444" />
            </TouchableOpacity>
            <Text style={styles.modalTitle}>Discount</Text>
            <View style={{ width: 26 }} />
          </View>

          <View style={styles.toggleRow}>
            <TouchableOpacity
              style={[styles.toggleBtn, mode === "amount" && styles.toggleBtnActive]}
              onPress={() => setMode("amount")}
              testID="disc-amount"
            >
              <Text style={[styles.toggleText, mode === "amount" && styles.toggleTextActive]}>
                Amount
              </Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.toggleBtn, mode === "percent" && styles.toggleBtnActive]}
              onPress={() => setMode("percent")}
              testID="disc-percent"
            >
              <Text style={[styles.toggleText, mode === "percent" && styles.toggleTextActive]}>
                %
              </Text>
            </TouchableOpacity>
          </View>

          <View style={styles.discountInput}>
            <Text style={styles.discountInputText}>
              {val || "0"}
              {mode === "percent" && val ? "%" : ""}
            </Text>
          </View>

          <View style={styles.quickRow}>
            {quicks.map((q) => (
              <TouchableOpacity
                key={q}
                style={styles.quickPct}
                onPress={() => {
                  setMode("percent");
                  setVal(String(q));
                }}
                testID={`disc-quick-${q}`}
              >
                <Text style={styles.quickPctText}>{q}%</Text>
              </TouchableOpacity>
            ))}
          </View>

          <View style={styles.discPad}>
            {keys.map((k) => (
              <TouchableOpacity
                key={k}
                style={styles.discKey}
                onPress={() => onKey(k)}
                testID={`disc-pad-${k}`}
              >
                {k === "back" ? (
                  <Ionicons name="backspace-outline" size={22} color="#00B14F" />
                ) : (
                  <Text style={styles.discKeyText}>{k}</Text>
                )}
              </TouchableOpacity>
            ))}
          </View>

          <TouchableOpacity style={styles.doneBtn} onPress={apply} testID="disc-done">
            <Text style={styles.doneBtnText}>Done</Text>
          </TouchableOpacity>
        </View>
      </View>
    </Modal>
  );
}

// ---------- Customer Modal ----------
function CustomerModal({
  visible,
  onClose,
  onSelect,
}: {
  visible: boolean;
  onClose: () => void;
  onSelect: (c: Customer) => void;
}) {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [q, setQ] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");

  useEffect(() => {
    if (visible) {
      setShowAdd(false);
      setQ("");
      fetch(`${API}/customers`)
        .then((r) => r.json())
        .then(setCustomers);
    }
  }, [visible]);

  const filtered = useMemo(
    () =>
      customers.filter(
        (c) =>
          !q ||
          c.name.toLowerCase().includes(q.toLowerCase()) ||
          c.phone?.includes(q)
      ),
    [customers, q]
  );

  const addCustomer = async () => {
    if (!name.trim()) return;
    const res = await fetch(`${API}/customers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), phone: phone.trim() || null }),
    });
    const c = await res.json();
    setCustomers((list) => [c, ...list]);
    setName("");
    setPhone("");
    setShowAdd(false);
    onSelect(c);
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.customerModal} testID="customer-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose}>
              <Text style={styles.cancelText}>Cancel</Text>
            </TouchableOpacity>
            <Text style={styles.modalTitle}>
              {showAdd ? "New Customer" : "Search Customer"}
            </Text>
            <TouchableOpacity onPress={() => setShowAdd((s) => !s)} testID="toggle-add-customer">
              <Ionicons
                name={showAdd ? "close" : "create-outline"}
                size={22}
                color="#00B14F"
              />
            </TouchableOpacity>
          </View>

          {showAdd ? (
            <View style={{ padding: 20, gap: 14 }}>
              <TextInput
                placeholder="Name"
                placeholderTextColor="#94A3B8"
                style={styles.textInput}
                value={name}
                onChangeText={setName}
                testID="new-cust-name"
              />
              <TextInput
                placeholder="Phone (optional)"
                placeholderTextColor="#94A3B8"
                style={styles.textInput}
                value={phone}
                onChangeText={setPhone}
                keyboardType="phone-pad"
                testID="new-cust-phone"
              />
              <TouchableOpacity
                style={styles.saveCustBtn}
                onPress={addCustomer}
                testID="save-customer"
              >
                <Text style={styles.saveCustText}>Save & Select</Text>
              </TouchableOpacity>
            </View>
          ) : (
            <>
              <View style={styles.searchBox}>
                <Ionicons name="search" size={18} color="#94A3B8" />
                <TextInput
                  placeholder="Search"
                  placeholderTextColor="#94A3B8"
                  style={styles.searchInput2}
                  value={q}
                  onChangeText={setQ}
                  testID="cust-search"
                />
              </View>
              <FlatList
                data={filtered}
                keyExtractor={(i) => i.id}
                ItemSeparatorComponent={() => <View style={styles.sep} />}
                renderItem={({ item }) => (
                  <TouchableOpacity
                    style={styles.custRow}
                    onPress={() => onSelect(item)}
                    testID={`cust-${item.id}`}
                  >
                    <View style={[styles.custAvatar, { backgroundColor: item.color }]}>
                      <Text style={styles.custAvatarText}>
                        {item.name[0]?.toUpperCase() || "?"}
                      </Text>
                    </View>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.custRowName} numberOfLines={1}>
                        {item.name}
                      </Text>
                      {item.phone && <Text style={styles.custRowPhone}>{item.phone}</Text>}
                      {item.last_visit && (
                        <Text style={styles.custRowLast}>
                          Last visit {item.last_visit}
                        </Text>
                      )}
                    </View>
                  </TouchableOpacity>
                )}
                ListEmptyComponent={
                  <View style={styles.empty}>
                    <Text style={styles.emptyText}>No customers</Text>
                  </View>
                }
              />
            </>
          )}
        </View>
      </View>
    </Modal>
  );
}

// ---------- Order Hub ----------
function OrderHubModal({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [tab, setTab] = useState("all");
  const [deliveryOn, setDeliveryOn] = useState(true);

  const load = async (source: string) => {
    const url = source === "all" ? `${API}/orders` : `${API}/orders?source=${source}`;
    const res = await fetch(url);
    setOrders(await res.json());
  };

  useEffect(() => {
    if (visible) load(tab);
  }, [visible, tab]);

  const cols: Array<{ key: string; label: string; icon: any; color: string }> = [
    { key: "new", label: "New Order", icon: "list-outline", color: "#F59E0B" },
    { key: "preparing", label: "Preparing", icon: "restaurant-outline", color: "#3B82F6" },
    { key: "completed", label: "Completed", icon: "checkmark-circle-outline", color: "#00B14F" },
    { key: "cancel", label: "Cancel", icon: "close-circle-outline", color: "#EF4444" },
  ];

  const grouped = (col: string) => orders.filter((o) => o.status === col);

  const updateStatus = async (id: string, status: string) => {
    await fetch(`${API}/orders/${id}/status`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    load(tab);
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.orderHub} testID="order-hub-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose} testID="close-orderhub">
              <Ionicons name="close" size={26} color="#475569" />
            </TouchableOpacity>
            <Text style={styles.modalTitle}>All Orders</Text>
            <View style={styles.deliveryCtrl}>
              <TouchableOpacity
                style={styles.delToggle}
                onPress={() => setDeliveryOn((v) => !v)}
                testID="delivery-toggle"
              >
                <View
                  style={[
                    styles.delDot,
                    { backgroundColor: deliveryOn ? "#00B14F" : "#CBD5E1" },
                  ]}
                />
                <Text style={styles.delText}>Delivery ON/OFF</Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.delMenu}>
                <Text style={styles.delMenuText}>Delivery Menu</Text>
              </TouchableOpacity>
            </View>
          </View>

          <View style={styles.hubTabs}>
            {["all", "table", "delivery", "kiosk", "other"].map((t) => (
              <TouchableOpacity
                key={t}
                style={[styles.hubTab, tab === t && styles.hubTabActive]}
                onPress={() => setTab(t)}
                testID={`hub-tab-${t}`}
              >
                <Text
                  style={[styles.hubTabText, tab === t && styles.hubTabTextActive]}
                >
                  {t === "kiosk" ? "KIOSK" : t.charAt(0).toUpperCase() + t.slice(1)}
                </Text>
              </TouchableOpacity>
            ))}
          </View>

          <View style={styles.hubSearch}>
            <Ionicons name="search" size={16} color="#94A3B8" />
            <Text style={styles.hubSearchText}>Search by order number</Text>
          </View>

          <View style={styles.kanban}>
            {cols.map((c) => {
              const items = grouped(c.key);
              return (
                <View key={c.key} style={styles.kanCol} testID={`kan-col-${c.key}`}>
                  <View style={styles.kanHead}>
                    <Ionicons name={c.icon} size={18} color={c.color} />
                    <Text style={styles.kanTitle}>{c.label}</Text>
                    <View style={[styles.kanCount, { backgroundColor: c.color }]}>
                      <Text style={styles.kanCountText}>{items.length}</Text>
                    </View>
                  </View>
                  <ScrollView style={{ flex: 1 }}>
                    {items.map((o) => (
                      <TouchableOpacity
                        key={o.id}
                        style={styles.orderCard}
                        onPress={() => {
                          // simple cycle for demo
                          const next: Record<string, string> = {
                            new: "preparing",
                            preparing: "completed",
                            completed: "cancel",
                            cancel: "new",
                          };
                          updateStatus(o.id, next[o.status]);
                        }}
                        testID={`order-${o.order_number}`}
                      >
                        {o.delivery_status && (
                          <View
                            style={[
                              styles.delBadge,
                              {
                                backgroundColor:
                                  o.delivery_status === "DELIVERING"
                                    ? "#FEE2E2"
                                    : "#DCFCE7",
                              },
                            ]}
                          >
                            <Text
                              style={[
                                styles.delBadgeText,
                                {
                                  color:
                                    o.delivery_status === "DELIVERING"
                                      ? "#DC2626"
                                      : "#16A34A",
                                },
                              ]}
                            >
                              {o.delivery_status}
                            </Text>
                          </View>
                        )}
                        <View style={styles.orderRow}>
                          {o.delivery_provider && (
                            <View style={styles.grabPill}>
                              <Text style={styles.grabText}>{o.delivery_provider}</Text>
                            </View>
                          )}
                          <Text style={styles.orderNum}>{o.order_number}</Text>
                        </View>
                        <View style={styles.orderMeta}>
                          <View style={styles.metaItem}>
                            <Ionicons name="cube-outline" size={13} color="#94A3B8" />
                            <Text style={styles.metaText}>
                              {o.items.reduce((s, i) => s + i.qty, 0)}
                            </Text>
                          </View>
                          <View style={styles.metaItem}>
                            <Text style={styles.metaText}>฿{o.total.toFixed(2)}</Text>
                          </View>
                          <View style={styles.metaItem}>
                            <Ionicons name="time-outline" size={13} color="#94A3B8" />
                            <Text style={styles.metaText}>{o.created_time}</Text>
                          </View>
                        </View>
                      </TouchableOpacity>
                    ))}
                  </ScrollView>
                </View>
              );
            })}
          </View>
        </View>
      </View>
    </Modal>
  );
}

// ---------- Parked Orders ----------
function ParkedOrdersModal({
  visible,
  onClose,
  currentCart,
  onPark,
  onRetrieve,
}: {
  visible: boolean;
  onClose: () => void;
  currentCart: CartItem[];
  onPark: () => void;
  onRetrieve: (items: CartItem[]) => void;
}) {
  const [parked, setParked] = useState<ParkedOrder[]>([]);

  const load = async () => {
    const res = await fetch(`${API}/parked-orders`);
    setParked(await res.json());
  };
  useEffect(() => {
    if (visible) load();
  }, [visible]);

  const del = async (id: string) => {
    await fetch(`${API}/parked-orders/${id}`, { method: "DELETE" });
    load();
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.parkedModal} testID="parked-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose}>
              <Ionicons name="close" size={26} color="#475569" />
            </TouchableOpacity>
            <Text style={styles.modalTitle}>Save & Retrieve</Text>
            <View style={{ width: 26 }} />
          </View>

          {currentCart.length > 0 && (
            <TouchableOpacity style={styles.parkBtn} onPress={onPark} testID="park-current">
              <Ionicons name="bookmark" size={18} color="#00B14F" />
              <Text style={styles.parkBtnText}>Park current order ({currentCart.length} items)</Text>
            </TouchableOpacity>
          )}

          <FlatList
            data={parked}
            keyExtractor={(i) => i.id}
            contentContainerStyle={{ padding: 16, gap: 10 }}
            ListEmptyComponent={
              <View style={styles.empty}>
                <Ionicons name="bookmarks-outline" size={40} color="#CBD5E1" />
                <Text style={styles.emptyText}>No parked orders</Text>
              </View>
            }
            renderItem={({ item }) => (
              <View style={styles.parkRow} testID={`parked-${item.id}`}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.parkLabel}>{item.label}</Text>
                  <Text style={styles.parkSub}>
                    {item.items.length} item(s) · {THB(item.subtotal)}
                  </Text>
                </View>
                <TouchableOpacity
                  style={styles.retrieveBtn}
                  onPress={() => {
                    onRetrieve(item.items);
                    del(item.id);
                  }}
                  testID={`retrieve-${item.id}`}
                >
                  <Text style={styles.retrieveBtnText}>Retrieve</Text>
                </TouchableOpacity>
                <TouchableOpacity onPress={() => del(item.id)} testID={`park-del-${item.id}`}>
                  <Ionicons name="trash-outline" size={20} color="#EF4444" />
                </TouchableOpacity>
              </View>
            )}
          />
        </View>
      </View>
    </Modal>
  );
}

// ---------- Success Modal ----------
function SuccessModal({
  data,
  onClose,
}: {
  data: null | { order_number: string; total: number; paid: number; change: number; method: string };
  onClose: () => void;
}) {
  return (
    <Modal visible={!!data} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        {data && (
          <View style={styles.successModal} testID="success-modal">
            <View style={styles.successIcon}>
              <Ionicons name="checkmark" size={56} color="#FFFFFF" />
            </View>
            <Text style={styles.successTitle}>Payment Successful</Text>
            <Text style={styles.successOrder}>{data.order_number}</Text>

            <View style={styles.successRow}>
              <Text style={styles.successLabel}>Method</Text>
              <Text style={styles.successVal}>{data.method}</Text>
            </View>
            <View style={styles.successRow}>
              <Text style={styles.successLabel}>Total</Text>
              <Text style={styles.successVal}>{THB(data.total)}</Text>
            </View>
            <View style={styles.successRow}>
              <Text style={styles.successLabel}>Received</Text>
              <Text style={styles.successVal}>{THB(data.paid)}</Text>
            </View>
            <View style={[styles.successRow, styles.changeRow]}>
              <Text style={styles.changeRowLabel}>Change Due</Text>
              <Text style={styles.changeRowVal}>{THB(data.change)}</Text>
            </View>

            <TouchableOpacity style={styles.successBtn} onPress={onClose} testID="success-done">
              <Text style={styles.successBtnText}>Done · New Order</Text>
            </TouchableOpacity>
          </View>
        )}
      </View>
    </Modal>
  );
}

// ---------- Drawer Modal ----------
function DrawerModal({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <TouchableOpacity style={styles.drawerOverlay} onPress={onClose} activeOpacity={1}>
        <TouchableOpacity activeOpacity={1} style={styles.drawerPanel} testID="drawer-panel">
          <Text style={styles.drawerTitle}>Cash Drawer</Text>
          <Text style={styles.drawerSub}>Today's quick view</Text>

          <View style={styles.drawerStats}>
            <StatCard label="Sales" value="฿12,480.00" icon="trending-up" color="#00B14F" />
            <StatCard label="Orders" value="24" icon="receipt-outline" color="#3B82F6" />
            <StatCard label="Avg Ticket" value="฿520.00" icon="pulse" color="#F59E0B" />
          </View>

          <Text style={styles.drawerNote}>More reports coming soon.</Text>
          <TouchableOpacity style={styles.drawerClose} onPress={onClose}>
            <Text style={styles.drawerCloseText}>Close</Text>
          </TouchableOpacity>
        </TouchableOpacity>
      </TouchableOpacity>
    </Modal>
  );
}

function StatCard({
  label,
  value,
  icon,
  color,
}: {
  label: string;
  value: string;
  icon: any;
  color: string;
}) {
  return (
    <View style={[styles.statCard, { borderLeftColor: color }]}>
      <Ionicons name={icon} size={20} color={color} />
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={styles.statValue}>{value}</Text>
    </View>
  );
}

// ============ STYLES ============
const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#F1F5F9" },

  // Top bar
  topBar: {
    height: 64,
    backgroundColor: "#FFFFFF",
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 12,
    gap: 8,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  menuBtn: { width: 40, height: 40, alignItems: "center", justifyContent: "center" },
  searchWrap: {
    flex: 1,
    height: 40,
    backgroundColor: "#F1F5F9",
    borderRadius: 10,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 12,
    gap: 8,
    maxWidth: 360,
  },
  searchInput: { flex: 1, fontSize: 14, color: "#0F172A", ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}) },
  tbItem: { alignItems: "center", paddingHorizontal: 12, minWidth: 64 },
  tbItemCompact: { minWidth: 40, paddingHorizontal: 6 },
  tbLabel: { fontSize: 10, color: "#475569", marginTop: 2, fontWeight: "500" },
  badge: {
    position: "absolute",
    top: -4,
    right: -8,
    backgroundColor: "#EF4444",
    minWidth: 16,
    height: 16,
    borderRadius: 8,
    paddingHorizontal: 4,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { color: "#FFFFFF", fontSize: 10, fontWeight: "700" },
  staffChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 20,
    backgroundColor: "#E5F7ED",
  },
  staffText: { fontSize: 12, color: "#00B14F", fontWeight: "600" },
  adminBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#00B14F",
  },
  adminBtnText: { fontSize: 12, fontWeight: "700", color: "#00B14F" },
  logoutBtn: { padding: 8 },

  // Main
  main: { flex: 1, flexDirection: "row" },
  mainStacked: { flexDirection: "column" },

  // Mobile search
  mobileSearchWrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    margin: 12,
    marginBottom: 0,
    height: 44,
    backgroundColor: "#FFFFFF",
    borderRadius: 10,
    paddingHorizontal: 14,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },

  // Horizontal category strip (mobile)
  catStrip: {
    maxHeight: 58,
    backgroundColor: "#FFFFFF",
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
    marginTop: 12,
    paddingVertical: 10,
    flexGrow: 0,
  },
  catChip: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 20,
    backgroundColor: "#F1F5F9",
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  catChipActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  catChipText: { fontSize: 13, fontWeight: "600", color: "#475569" },
  catChipTextActive: { color: "#FFFFFF" },

  // FAB cart (mobile)
  fabCart: {
    position: "absolute",
    left: 12,
    right: 12,
    bottom: 12,
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#00B14F",
    borderRadius: 16,
    paddingHorizontal: 16,
    paddingVertical: 12,
    gap: 14,
    shadowColor: "#000",
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.2,
    shadowRadius: 12,
    elevation: 8,
  },
  fabLeft: { flexDirection: "row", alignItems: "center" },
  fabBadge: {
    marginLeft: -8,
    marginTop: -10,
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    backgroundColor: "#FFFFFF",
    paddingHorizontal: 6,
    alignItems: "center",
    justifyContent: "center",
  },
  fabBadgeText: { color: "#00B14F", fontSize: 11, fontWeight: "700" },
  fabMid: { flex: 1 },
  fabTotalLabel: { color: "rgba(255,255,255,0.8)", fontSize: 10, fontWeight: "600" },
  fabTotal: { color: "#FFFFFF", fontSize: 18, fontWeight: "700" },
  fabRight: { flexDirection: "row", alignItems: "center", gap: 4 },
  fabView: { color: "#FFFFFF", fontSize: 13, fontWeight: "700" },

  // Mobile cart bottom sheet
  cartSheetOverlay: { flex: 1, backgroundColor: "rgba(15,23,42,0.5)", justifyContent: "flex-end" },
  cartSheet: {
    backgroundColor: "#FFFFFF",
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    paddingTop: 10,
    paddingBottom: 20,
    maxHeight: "85%",
  },
  cartSheetHandle: {
    width: 40,
    height: 4,
    borderRadius: 2,
    backgroundColor: "#CBD5E1",
    alignSelf: "center",
    marginBottom: 8,
  },
  cartSheetHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingBottom: 8,
    borderBottomWidth: 1,
    borderBottomColor: "#F1F5F9",
  },
  cartSheetTitle: { fontSize: 17, fontWeight: "700", color: "#0F172A" },
  mobileDiscBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    marginHorizontal: 14,
    marginTop: 8,
    paddingVertical: 10,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#00B14F",
  },
  mobileDiscText: { color: "#00B14F", fontSize: 13, fontWeight: "600" },

  // Left rail
  leftRail: {
    width: 104,
    backgroundColor: "#FFFFFF",
    borderRightWidth: 1,
    borderRightColor: "#E2E8F0",
    paddingVertical: 8,
  },
  catPill: {
    margin: 8,
    padding: 12,
    borderRadius: 12,
    backgroundColor: "#FFFFFF",
    borderWidth: 2,
    borderColor: "transparent",
    alignItems: "center",
    minHeight: 56,
    justifyContent: "center",
  },
  catPillActive: {
    backgroundColor: "#00B14F",
    borderColor: "#00B14F",
  },
  catText: { fontSize: 12, fontWeight: "600", color: "#475569", textAlign: "center" },
  catTextActive: { color: "#FFFFFF" },
  catSub: { fontSize: 9, color: "#94A3B8", marginTop: 2, textAlign: "center" },
  catSubActive: { color: "#E5F7ED" },

  // Center
  center: { flex: 1 },
  productCard: {
    flex: 1,
    backgroundColor: "#FFFFFF",
    borderRadius: 14,
    overflow: "hidden",
    maxWidth: "24%",
    shadowColor: "#000",
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.05,
    shadowRadius: 4,
    elevation: 2,
  },
  productImg: { width: "100%", height: 110, backgroundColor: "#F1F5F9" },
  productInfo: { padding: 10 },
  productName: { fontSize: 13, fontWeight: "600", color: "#0F172A", minHeight: 34 },
  productPrice: { fontSize: 15, fontWeight: "700", color: "#00B14F", marginTop: 4 },
  scanBar: {
    height: 44,
    backgroundColor: "#FFFFFF",
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderTopWidth: 1,
    borderTopColor: "#E2E8F0",
  },
  scanText: { fontSize: 13, color: "#475569", fontWeight: "500" },
  empty: { flex: 1, alignItems: "center", justifyContent: "center", padding: 40, gap: 10 },
  emptyText: { color: "#94A3B8", fontSize: 14 },

  // Right cart
  rightCart: {
    width: 320,
    backgroundColor: "#FFFFFF",
    borderLeftWidth: 1,
    borderLeftColor: "#E2E8F0",
    padding: 14,
  },
  rightCartEmbedded: {
    width: "100%",
    borderLeftWidth: 0,
  },
  cartHeader: { gap: 8, marginBottom: 8 },
  tablePill: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 8,
    backgroundColor: "#F1F5F9",
  },
  tableText: { fontSize: 11, fontWeight: "600", color: "#0F172A" },
  custChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    backgroundColor: "#E5F7ED",
    paddingHorizontal: 8,
    paddingVertical: 6,
    borderRadius: 10,
  },
  custDot: {
    width: 24,
    height: 24,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  custInitial: { color: "#FFFFFF", fontWeight: "700", fontSize: 11 },
  custName: { flex: 1, fontSize: 12, fontWeight: "600", color: "#0F172A" },

  totalBox: {
    backgroundColor: "#F8FAFC",
    padding: 14,
    borderRadius: 12,
    marginBottom: 10,
  },
  totalLabel: { fontSize: 12, color: "#94A3B8" },
  subTotalVal: { fontSize: 16, color: "#475569", fontWeight: "600", marginTop: 2 },
  discRow: { flexDirection: "row", justifyContent: "space-between", marginTop: 6 },
  discLabel: { fontSize: 11, color: "#EF4444" },
  discVal: { fontSize: 12, color: "#EF4444", fontWeight: "600" },
  totalRow: {
    flexDirection: "row",
    alignItems: "flex-end",
    justifyContent: "flex-end",
    marginTop: 8,
    gap: 6,
  },
  thbText: { fontSize: 13, color: "#94A3B8", fontWeight: "600", marginBottom: 4 },
  totalVal: { fontSize: 32, fontWeight: "700", color: "#0F172A", letterSpacing: -1 },
  payBtn: {
    backgroundColor: "#00B14F",
    height: 56,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 14,
  },
  payBtnDisabled: { backgroundColor: "#CBD5E1" },
  payBtnText: { color: "#FFFFFF", fontSize: 18, fontWeight: "700" },
  cartListHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginBottom: 8,
  },
  cartListTitle: { fontSize: 13, fontWeight: "700", color: "#0F172A" },
  cartListCount: { fontSize: 11, color: "#94A3B8" },
  cartItem: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 10,
    borderBottomWidth: 1,
    borderBottomColor: "#F1F5F9",
  },
  cartItemName: { fontSize: 12, color: "#0F172A", fontWeight: "600" },
  cartItemPrice: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  qtyCtrl: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    backgroundColor: "#F1F5F9",
    borderRadius: 8,
    padding: 2,
  },
  qtyBtn: { width: 28, height: 28, alignItems: "center", justifyContent: "center" },
  qtyText: { fontSize: 13, fontWeight: "700", minWidth: 18, textAlign: "center" },
  trashBtn: { padding: 4 },
  emptyCart: { alignItems: "center", paddingVertical: 40, gap: 6 },
  emptyCartText: { fontSize: 13, color: "#94A3B8", fontWeight: "600" },
  emptyCartSub: { fontSize: 11, color: "#CBD5E1" },
  clearBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    marginTop: 8,
  },
  clearBtnText: { color: "#EF4444", fontSize: 12, fontWeight: "600" },

  // Generic modal
  overlay: {
    flex: 1,
    backgroundColor: "rgba(15,23,42,0.45)",
    alignItems: "center",
    justifyContent: "center",
    padding: 20,
  },
  modalHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  modalTitle: { fontSize: 17, fontWeight: "700", color: "#0F172A" },

  // Payment modal
  paymentModal: {
    width: "92%",
    maxWidth: 1000,
    height: "88%",
    backgroundColor: "#FFFFFF",
    borderRadius: 20,
    overflow: "hidden",
  },
  paymentBody: { flex: 1, flexDirection: "row", padding: 16, gap: 16 },
  methodsCol: { width: 140, gap: 10 },
  methodBtn: {
    padding: 14,
    borderRadius: 12,
    borderWidth: 2,
    borderColor: "#E2E8F0",
    alignItems: "center",
    gap: 6,
  },
  methodBtnActive: { borderColor: "#00B14F", backgroundColor: "#E5F7ED" },
  methodText: { fontSize: 12, fontWeight: "600", color: "#475569" },
  methodTextActive: { color: "#00B14F" },

  padCol: { flex: 1, gap: 10 },
  amountDisplay: {
    backgroundColor: "#F1F5F9",
    borderRadius: 12,
    padding: 18,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  thbSmall: { fontSize: 14, color: "#94A3B8", fontWeight: "600" },
  amountText: { fontSize: 32, fontWeight: "700", color: "#0F172A" },
  padGrid: {
    flex: 1,
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  padBtn: {
    width: "31.5%",
    aspectRatio: 1.8,
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  padText: { fontSize: 24, fontWeight: "600", color: "#0F172A" },
  clearPadBtn: {
    backgroundColor: "#FEF3C7",
    padding: 12,
    borderRadius: 12,
    alignItems: "center",
  },
  clearPadText: { fontSize: 14, fontWeight: "700", color: "#D97706" },

  quickCol: { width: 120, gap: 10 },
  netBox: {
    backgroundColor: "#F1F5F9",
    borderRadius: 12,
    padding: 12,
    alignItems: "center",
  },
  netLabel: { fontSize: 11, color: "#475569", fontWeight: "600" },
  netHint: { fontSize: 9, color: "#94A3B8", marginTop: 2 },
  netVal: { fontSize: 16, color: "#00B14F", fontWeight: "700", marginTop: 6 },
  quickBtn: {
    backgroundColor: "#F8FAFC",
    borderRadius: 10,
    padding: 14,
    alignItems: "center",
  },
  quickText: { fontSize: 16, fontWeight: "700", color: "#0F172A" },

  // QR pane (Thai QR / KBank)
  qrPane: {
    flex: 1,
    backgroundColor: "#FFFFFF",
    borderRadius: 14,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    padding: 16,
    alignItems: "center",
    gap: 10,
  },
  qrHeader: {
    backgroundColor: "#1E3A8A",
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignSelf: "stretch",
    alignItems: "center",
  },
  qrHeaderText: { color: "#FFFFFF", fontSize: 16, fontWeight: "700" },
  qrBrandRow: { flexDirection: "row", gap: 10, flexWrap: "wrap", justifyContent: "center" },
  brandPill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    backgroundColor: "#F1F5F9",
    borderRadius: 4,
    fontSize: 11,
    color: "#475569",
    fontWeight: "700",
  },
  qrHint: { fontSize: 11, color: "#475569", textAlign: "center" },
  qrBy: { fontSize: 11, color: "#94A3B8" },
  qrBox: {
    width: 160,
    height: 160,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#FFFFFF",
    borderWidth: 2,
    borderColor: "#00B14F",
    borderRadius: 10,
  },
  qrAmount: { fontSize: 22, fontWeight: "700", color: "#00B14F" },
  qrInstructBox: {
    backgroundColor: "#F1F5F9",
    padding: 10,
    borderRadius: 8,
    flexDirection: "row",
    gap: 6,
    alignItems: "flex-start",
  },
  qrInstructText: { flex: 1, fontSize: 11, color: "#475569" },

  // Custom pane
  customPane: {
    flex: 1,
    backgroundColor: "#FFFFFF",
    borderRadius: 14,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    padding: 14,
    gap: 12,
  },
  customRef: {
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
    paddingVertical: 10,
    fontSize: 14,
    color: "#0F172A",
    ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}),
  },
  customAmountRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingBottom: 10,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  customAmountLabel: { fontSize: 14, color: "#475569" },
  customAmountVal: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  customGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 10,
    marginTop: 6,
  },
  customOption: {
    width: "48%",
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 12,
    paddingVertical: 14,
    borderRadius: 24,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  customOptionActive: { borderColor: "#00B14F", backgroundColor: "#E5F7ED" },
  radio: {
    width: 18,
    height: 18,
    borderRadius: 9,
    borderWidth: 2,
    borderColor: "#CBD5E1",
    alignItems: "center",
    justifyContent: "center",
  },
  radioActive: { borderColor: "#00B14F" },
  radioInner: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: "#00B14F",
  },
  customOptionText: { flex: 1, fontSize: 12, color: "#0F172A", fontWeight: "500" },
  summaryBox: {
    backgroundColor: "#F8FAFC",
    padding: 12,
    borderRadius: 10,
    alignItems: "center",
    marginTop: 10,
  },
  summaryVal: {
    fontSize: 18,
    fontWeight: "700",
    color: "#0F172A",
    marginTop: 4,
  },

  paymentFooter: {
    flexDirection: "row",
    alignItems: "center",
    padding: 16,
    gap: 16,
    borderTopWidth: 1,
    borderTopColor: "#E2E8F0",
  },
  changeBox: { flex: 1 },
  changeLabel: { fontSize: 11, color: "#94A3B8" },
  changeVal: { fontSize: 20, fontWeight: "700", color: "#0F172A" },
  confirmBtn: {
    flex: 1,
    backgroundColor: "#00B14F",
    paddingVertical: 16,
    borderRadius: 12,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
  },
  confirmText: { color: "#FFFFFF", fontSize: 16, fontWeight: "700" },

  // Discount modal
  discountModal: {
    width: "60%",
    maxWidth: 520,
    backgroundColor: "#FFFFFF",
    borderRadius: 20,
    padding: 4,
    overflow: "hidden",
  },
  toggleRow: {
    flexDirection: "row",
    margin: 16,
    backgroundColor: "#F1F5F9",
    borderRadius: 10,
    padding: 4,
  },
  toggleBtn: { flex: 1, paddingVertical: 10, alignItems: "center", borderRadius: 8 },
  toggleBtnActive: { backgroundColor: "#FFFFFF" },
  toggleText: { fontSize: 13, fontWeight: "600", color: "#94A3B8" },
  toggleTextActive: { color: "#0F172A" },
  discountInput: {
    marginHorizontal: 16,
    backgroundColor: "#F8FAFC",
    padding: 20,
    borderRadius: 12,
    alignItems: "center",
  },
  discountInputText: { fontSize: 32, fontWeight: "700", color: "#00B14F" },
  quickRow: {
    flexDirection: "row",
    justifyContent: "space-around",
    marginVertical: 16,
    paddingHorizontal: 16,
  },
  quickPct: {
    width: 64,
    height: 64,
    borderRadius: 32,
    borderWidth: 2,
    borderColor: "#00B14F",
    alignItems: "center",
    justifyContent: "center",
  },
  quickPctText: { fontSize: 16, fontWeight: "700", color: "#00B14F" },
  discPad: {
    flexDirection: "row",
    flexWrap: "wrap",
    paddingHorizontal: 16,
    gap: 8,
  },
  discKey: {
    width: "31.5%",
    aspectRatio: 2,
    alignItems: "center",
    justifyContent: "center",
  },
  discKeyText: { fontSize: 26, fontWeight: "500", color: "#00B14F" },
  doneBtn: {
    backgroundColor: "#00B14F",
    padding: 16,
    alignItems: "center",
    margin: 16,
    borderRadius: 12,
  },
  doneBtnText: { color: "#FFFFFF", fontSize: 16, fontWeight: "700" },

  // Customer modal
  customerModal: {
    width: "60%",
    maxWidth: 560,
    height: "80%",
    backgroundColor: "#FFFFFF",
    borderRadius: 20,
    overflow: "hidden",
  },
  cancelText: { color: "#EF4444", fontSize: 14, fontWeight: "600" },
  searchBox: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    margin: 16,
    backgroundColor: "#F1F5F9",
    borderRadius: 10,
    paddingHorizontal: 12,
    height: 40,
  },
  searchInput2: { flex: 1, fontSize: 14, color: "#0F172A", ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}) },
  custRow: { flexDirection: "row", gap: 12, alignItems: "center", padding: 16 },
  custAvatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    alignItems: "center",
    justifyContent: "center",
  },
  custAvatarText: { color: "#FFFFFF", fontWeight: "700", fontSize: 18 },
  custRowName: { fontSize: 14, fontWeight: "600", color: "#0F172A" },
  custRowPhone: { fontSize: 12, color: "#475569", marginTop: 2 },
  custRowLast: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  sep: { height: 1, backgroundColor: "#F1F5F9", marginLeft: 76 },
  textInput: {
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 10,
    padding: 14,
    fontSize: 14,
    color: "#0F172A",
  },
  saveCustBtn: {
    backgroundColor: "#00B14F",
    padding: 14,
    borderRadius: 10,
    alignItems: "center",
  },
  saveCustText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },

  // Order Hub
  orderHub: {
    width: "96%",
    height: "92%",
    backgroundColor: "#FFFFFF",
    borderRadius: 20,
    overflow: "hidden",
  },
  deliveryCtrl: { flexDirection: "row", alignItems: "center", gap: 10 },
  delToggle: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 20,
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  delDot: { width: 10, height: 10, borderRadius: 5 },
  delText: { fontSize: 11, color: "#475569", fontWeight: "600" },
  delMenu: {
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 20,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  delMenuText: { fontSize: 11, color: "#475569", fontWeight: "600" },
  hubTabs: {
    flexDirection: "row",
    marginHorizontal: 16,
    marginTop: 14,
    backgroundColor: "#F1F5F9",
    borderRadius: 10,
    padding: 4,
  },
  hubTab: { flex: 1, paddingVertical: 10, alignItems: "center", borderRadius: 8 },
  hubTabActive: { backgroundColor: "#FFFFFF" },
  hubTabText: { fontSize: 13, color: "#94A3B8", fontWeight: "600" },
  hubTabTextActive: { color: "#0F172A" },
  hubSearch: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    margin: 16,
    backgroundColor: "#F1F5F9",
    borderRadius: 10,
    padding: 12,
  },
  hubSearchText: { fontSize: 13, color: "#94A3B8" },
  kanban: { flex: 1, flexDirection: "row", paddingHorizontal: 12, paddingBottom: 16, gap: 10 },
  kanCol: { flex: 1, backgroundColor: "#F8FAFC", borderRadius: 12, padding: 10 },
  kanHead: { flexDirection: "row", alignItems: "center", gap: 6, marginBottom: 12 },
  kanTitle: { flex: 1, fontSize: 13, fontWeight: "700", color: "#0F172A" },
  kanCount: {
    minWidth: 22,
    height: 22,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 6,
  },
  kanCountText: { color: "#FFFFFF", fontSize: 11, fontWeight: "700" },
  orderCard: {
    backgroundColor: "#FFFFFF",
    borderRadius: 10,
    padding: 10,
    marginBottom: 8,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  delBadge: {
    alignSelf: "flex-start",
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 4,
    marginBottom: 6,
  },
  delBadgeText: { fontSize: 9, fontWeight: "700", letterSpacing: 0.5 },
  orderRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  grabPill: {
    backgroundColor: "#00B14F",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
  },
  grabText: { color: "#FFFFFF", fontSize: 10, fontWeight: "700" },
  orderNum: { fontSize: 14, fontWeight: "700", color: "#0F172A" },
  orderMeta: { flexDirection: "row", gap: 12, marginTop: 8 },
  metaItem: { flexDirection: "row", alignItems: "center", gap: 3 },
  metaText: { fontSize: 11, color: "#475569" },

  // Parked
  parkedModal: {
    width: "60%",
    maxWidth: 560,
    height: "70%",
    backgroundColor: "#FFFFFF",
    borderRadius: 20,
    overflow: "hidden",
  },
  parkBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    padding: 14,
    margin: 16,
    backgroundColor: "#E5F7ED",
    borderRadius: 12,
    borderWidth: 2,
    borderColor: "#00B14F",
    borderStyle: "dashed",
  },
  parkBtnText: { color: "#00B14F", fontSize: 14, fontWeight: "700" },
  parkRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: 14,
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
  },
  parkLabel: { fontSize: 14, fontWeight: "700", color: "#0F172A" },
  parkSub: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  retrieveBtn: {
    backgroundColor: "#00B14F",
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 8,
  },
  retrieveBtnText: { color: "#FFFFFF", fontSize: 12, fontWeight: "700" },

  // Success
  successModal: {
    width: "50%",
    maxWidth: 440,
    backgroundColor: "#FFFFFF",
    borderRadius: 24,
    padding: 28,
    alignItems: "center",
  },
  successIcon: {
    width: 80,
    height: 80,
    borderRadius: 40,
    backgroundColor: "#00B14F",
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 16,
  },
  successTitle: { fontSize: 20, fontWeight: "700", color: "#0F172A" },
  successOrder: { fontSize: 14, color: "#94A3B8", marginTop: 4, marginBottom: 20 },
  successRow: {
    width: "100%",
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 8,
    borderBottomWidth: 1,
    borderBottomColor: "#F1F5F9",
  },
  successLabel: { fontSize: 13, color: "#475569" },
  successVal: { fontSize: 13, color: "#0F172A", fontWeight: "600" },
  changeRow: { borderBottomWidth: 0, marginTop: 8, paddingTop: 12 },
  changeRowLabel: { fontSize: 14, color: "#0F172A", fontWeight: "700" },
  changeRowVal: { fontSize: 18, color: "#00B14F", fontWeight: "700" },
  successBtn: {
    backgroundColor: "#00B14F",
    paddingVertical: 14,
    paddingHorizontal: 40,
    borderRadius: 12,
    marginTop: 20,
    width: "100%",
    alignItems: "center",
  },
  successBtnText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },

  // Drawer
  drawerOverlay: {
    flex: 1,
    backgroundColor: "rgba(15,23,42,0.45)",
    flexDirection: "row",
  },
  drawerPanel: {
    width: 360,
    height: "100%",
    backgroundColor: "#FFFFFF",
    padding: 24,
  },
  drawerTitle: { fontSize: 22, fontWeight: "700", color: "#0F172A" },
  drawerSub: { fontSize: 13, color: "#94A3B8", marginTop: 4, marginBottom: 20 },
  drawerStats: { gap: 10 },
  statCard: {
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
    padding: 14,
    borderLeftWidth: 4,
    gap: 4,
  },
  statLabel: { fontSize: 11, color: "#94A3B8", fontWeight: "600" },
  statValue: { fontSize: 18, color: "#0F172A", fontWeight: "700" },
  drawerNote: { fontSize: 12, color: "#94A3B8", marginTop: 24, fontStyle: "italic" },
  drawerClose: {
    marginTop: "auto" as any,
    padding: 14,
    backgroundColor: "#F1F5F9",
    borderRadius: 12,
    alignItems: "center",
  },
  drawerCloseText: { fontSize: 14, fontWeight: "600", color: "#475569" },
});
