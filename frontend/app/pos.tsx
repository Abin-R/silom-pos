import { useEffect, useState, useMemo, useCallback, useRef } from "react";
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
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { useRouter, useFocusEffect } from "expo-router";
import { Ionicons, MaterialCommunityIcons } from "@expo/vector-icons";
import PhoneInput from "../components/PhoneInput";
import { useStarPrinter } from "../lib/useStarPrinter";
import { loadLocalPrinterConfig } from "../lib/localPrinterConfig";
import { SidebarDrawer } from "../components/SidebarDrawer";
import { apiFetch, clearAuthToken } from "../lib/api";
import AsyncStorage from "@react-native-async-storage/async-storage";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const AUTH_KEY = "bravepos:auth:v1";

async function doLogout(): Promise<void> {
  try {
    await apiFetch("/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch {}
  clearAuthToken();
  try { await AsyncStorage.removeItem(AUTH_KEY); } catch {}
}

// How often we poll the backend for Beam charge status while a QR is on screen.
const BEAM_POLL_INTERVAL_MS = 3000;

// ---- Types ----
type Category = { id: string; name: string; name_th?: string; color: string; order: number };
type Product = {
  id: string;
  name: string;
  name_th?: string;
  price: number;
  category_id: string;
  image_url: string;
  image_base64?: string;
  is_favorite: boolean;
};
type CartItem = { product_id: string; name: string; price: number; qty: number; discount?: number };
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
  const insets = useSafeAreaInsets();
  // Receipt-printing hook.  printReceipt renders ReceiptImage to a PNG
  // and ships it via the native module's printImage().  ReceiptOverlay
  // is the off-screen component that gets captured — must be in JSX.
  const { printReceipt, ReceiptOverlay } = useStarPrinter();
  // The URL is just a display hint.  Real auth state lives in AsyncStorage and
  // gets loaded on mount, so manual URL edits can't desync role / branch from
  // the actual session token.
  const [staff, setStaff] = useState<string>("");
  const [role, setRole] = useState<string>("");
  const [activeBranchId, setActiveBranchId] = useState<string>("");
  const [activeBranchName, setActiveBranchName] = useState<string>("");
  const [authLoaded, setAuthLoaded] = useState(false);
  const isAdmin = role === "admin";

  useEffect(() => {
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(AUTH_KEY);
        const parsed = raw ? JSON.parse(raw) : null;
        if (!parsed?.token) {
          router.replace("/");
          return;
        }
        setStaff(parsed.staff?.name || "");
        setRole(parsed.staff?.role || "");
        setActiveBranchId(parsed.branch?.id || "");
        setActiveBranchName(parsed.branch?.name || "");
        setAuthLoaded(true);
      } catch {
        router.replace("/");
      }
    })();
  }, [router]);
  const { width } = useWindowDimensions();
  const isWide = width >= 720;
  const isMid = width >= 600;
  const gridCols = isWide ? 4 : isMid ? 3 : 2;

  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("favorite");
  const [cart, setCart] = useState<CartItem[]>([]);
  const [editItem, setEditItem] = useState<CartItem | null>(null); // cart-item edit modal
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [parkedCount, setParkedCount] = useState(0);
  const [orderHubCount, setOrderHubCount] = useState(0);

  // modal states
  const [showPayment, setShowPayment] = useState(false);
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
  // Surfaces the local-print outcome inside SuccessModal so the cashier
  // knows whether the receipt actually came out of the printer — or got
  // queued because the printer is offline / unreachable.  `null` = not
  // attempted (no local printer configured) so we render nothing.
  const [printStatus, setPrintStatus] = useState<
    | null
    | { state: "printing" }
    | { state: "printed" }
    | { state: "queued"; error?: string }
  >(null);
  const [showDrawer, setShowDrawer] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Selling gate: until a shift is open, the product grid / cart are blocked.
  // `null` = still loading (render nothing rather than flash the gate).
  const [shiftOpen, setShiftOpen] = useState<boolean | null>(null);
  const [showOpenShift, setShowOpenShift] = useState(false);
  const [startCash, setStartCash] = useState("0");
  const [openingShift, setOpeningShift] = useState(false);

  const loadShift = useCallback(async () => {
    try {
      const cur = await apiFetch(`${API}/shifts/current`).then((r) => r.json());
      setShiftOpen(!!(cur && cur.id));
    } catch {
      // Treat a failed lookup as "no shift" so the cashier is told to open one
      // rather than silently selling against a closed drawer.
      setShiftOpen(false);
    }
  }, []);
  useEffect(() => { loadShift(); }, [loadShift]);
  // Re-check on focus so closing a shift in the admin Drawer re-raises the gate
  // the moment the cashier returns to the POS screen.
  useFocusEffect(useCallback(() => { loadShift(); }, [loadShift]));

  const openShift = async () => {
    if (openingShift) return;
    setOpeningShift(true);
    try {
      await apiFetch(`${API}/shifts/open`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start_cash: parseFloat(startCash) || 0, opened_by: staff || "Admin" }),
      });
      setShowOpenShift(false);
      setStartCash("0");
      await loadShift();
    } catch (e) {
      console.error("open shift failed", e);
    } finally {
      setOpeningShift(false);
    }
  };

  // Load initial data
  const reloadPosData = useCallback(async () => {
    setLoading(true);
    try {
      const [catsRes, prodsRes] = await Promise.all([
        apiFetch(`${API}/categories`),
        apiFetch(`${API}/products`),
      ]);
      const cats: Category[] = await catsRes.json();
      const prods: Product[] = await prodsRes.json();
      setCategories(Array.isArray(cats) ? cats : []);
      setProducts(Array.isArray(prods) ? prods : []);
    } catch (e) {
      console.error("Load failed", e);
    } finally {
      setLoading(false);
    }
    refreshBadges();
  }, []);

  useEffect(() => {
    reloadPosData();
  }, [reloadPosData]);

  const refreshBadges = async () => {
    try {
      const [po, oh] = await Promise.all([
        apiFetch(`${API}/parked-orders`).then((r) => r.json()),
        apiFetch(`${API}/orders?source=delivery`).then((r) => r.json()),
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

  // Subtotal is the gross line value; discounts are per-product only (no
  // common/order-level discount). A line discount is clamped to its line total.
  const subtotal = useMemo(
    () => cart.reduce((s, i) => s + i.price * i.qty, 0),
    [cart]
  );
  const discountAmount = useMemo(
    () => cart.reduce((s, i) => s + Math.min(i.discount || 0, i.price * i.qty), 0),
    [cart]
  );
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

  // Apply qty/discount edits from the cart-item modal. A zero qty removes the line.
  const applyItemEdit = (pid: string, qty: number, discount: number) => {
    setCart((c) =>
      c
        .map((i) =>
          i.product_id === pid ? { ...i, qty, discount: discount > 0 ? discount : undefined } : i
        )
        .filter((i) => i.qty > 0)
    );
  };

  const clearCart = () => {
    setCart([]);
    setCustomer(null);
  };

  const handlePaySuccess = async (
    method: string,
    paid: number,
    meta?: { beamChargeId?: string }
  ) => {
    try {
      const res = await apiFetch(`${API}/orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: cart,
          subtotal,
          discount_type: discountAmount > 0 ? "item" : "none",
          discount_value: 0,
          discount_amount: discountAmount,
          total,
          payment_method: method,
          paid_amount: paid,
          change: Math.max(0, paid - total),
          source: "table",
          customer_id: customer?.id,
          customer_name: customer?.name,
          beam_charge_id: meta?.beamChargeId || null,
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

      // Fire-and-forget local print: if THIS tablet has a configured local
      // printer (USB/BT/LAN via Star SDK), print straight from the device.
      // Backend's own auto-print path is independent — they don't conflict.
      // The outcome is surfaced via printStatus → SuccessModal so the
      // cashier sees whether the receipt printed or got queued for retry.
      (async () => {
        try {
          const cfg = await loadLocalPrinterConfig();
          if (!cfg.enabled) return;     // no local printer → nothing to show
          setPrintStatus({ state: "printing" });
          const shopRes = await apiFetch(`${API}/settings`);
          const shop = shopRes.ok ? await shopRes.json() : {};
          // Settings.branch is a shop-wide string default ("Main"), not
          // the cashier's actually-selected branch.  Override with the
          // active branch so receipts carry the correct location.
          if (activeBranchName) shop.branch = activeBranchName;
          const r = await printReceipt(
            cfg,
            {
              order_number: order.order_number,
              items: cart.map((c) => ({ name: c.name, qty: c.qty, price: c.price })),
              total,
              payment_method: method,
              paid_amount: paid,
              change: Math.max(0, paid - total),
              created_at_local: new Date().toLocaleString("en-GB"),
              staff: staff || "",
            },
            shop,
          );
          // useStarPrinter enqueues the job on failure, so "not ok" means
          // queued-for-retry, not dropped.
          setPrintStatus(r.ok ? { state: "printed" } : { state: "queued", error: r.error });
        } catch (printErr: any) {
          console.warn("local print failed", printErr);
          setPrintStatus({ state: "queued", error: printErr?.message || String(printErr) });
        }
      })();
    } catch (e) {
      console.error("checkout fail", e);
    }
  };

  const parkCurrentOrder = async () => {
    if (cart.length === 0) return;
    await apiFetch(`${API}/parked-orders`, {
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

  if (!authLoaded) {
    // Don't render with empty staff/role/branch — flashes the wrong state and
    // the data fetches below would have nothing meaningful to display anyway.
    return (
      <SafeAreaView style={styles.root} edges={["top", "bottom"]}>
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
          <ActivityIndicator color="#00B14F" />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.root} edges={["top", "bottom"]}>
      {/* ============ TOP BAR ============ */}
      <View style={styles.topBar} testID="top-bar">
        <TouchableOpacity
          style={styles.menuBtn}
          onPress={() => setSidebarOpen(true)}
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
        <ToolbarIcon
          icon="albums-outline"
          label="Drawer"
          onPress={() => setShowDrawer(true)}
          testId="toolbar-drawer"
          compact={!isWide}
        />
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
        {/* Branch chip lives in the top bar only on tablet/desktop; on phone
            it moves into the mobile search row to avoid overflowing the bar. */}
        {isMid && !!activeBranchName && (
          <View style={styles.branchChip} testID="branch-chip">
            <Ionicons name="storefront-outline" size={16} color="#00B14F" />
            <Text style={styles.branchChipText} numberOfLines={1}>{activeBranchName}</Text>
          </View>
        )}
        {isWide && (
          <View style={styles.staffChip}>
            <Ionicons name="person-circle" size={22} color="#00B14F" />
            <Text style={styles.staffText}>{staff || "Admin"}</Text>
          </View>
        )}
        <TouchableOpacity
          style={styles.logoutBtn}
          onPress={async () => { await doLogout(); router.replace("/"); }}
          testID="logout-btn"
        >
          <Ionicons name="log-out-outline" size={20} color="#EF4444" />
        </TouchableOpacity>
      </View>

      {/* Mobile search bar (below top bar on narrow) + branch chip */}
      {!isMid && (
        <View style={styles.mobileTopRow}>
          <View style={[styles.mobileSearchWrap, { flex: 1, marginRight: 0 }]}>
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
          {!!activeBranchName && (
            <View style={styles.branchChipMobile} testID="branch-chip">
              <Ionicons name="storefront-outline" size={14} color="#00B14F" />
              <Text style={styles.branchChipMobileText} numberOfLines={1}>{activeBranchName}</Text>
            </View>
          )}
        </View>
      )}

      {/* ============ MAIN LAYOUT ============ */}
      <View style={{ flex: 1 }}>
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
                <Image source={{ uri: item.image_base64 || item.image_url }} style={styles.productImg} />
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
            discountAmount={discountAmount}
            total={total}
            cartCount={cartCount}
            onClear={clearCart}
            onRemoveCustomer={() => setCustomer(null)}
            onPay={() => setShowPayment(true)}
            onInc={(pid) => updateQty(pid, 1)}
            onDec={(pid) => updateQty(pid, -1)}
            onRemove={removeItem}
            onEdit={setEditItem}
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

        {/* Selling gate — blocks the grid/cart until a shift is open. The top
            bar (and its sidebar → admin/reports) stays reachable above this. */}
        {shiftOpen === false && (
          <View style={styles.shiftGate} testID="shift-gate">
            <Ionicons name="lock-closed-outline" size={44} color="#CBD5E1" />
            <Text style={styles.shiftGateText}>Open shift to continue</Text>
            <TouchableOpacity
              style={styles.shiftGateBtn}
              onPress={() => setShowOpenShift(true)}
              testID="gate-open-shift"
            >
              <Text style={styles.shiftGateBtnText}>OPEN SHIFT</Text>
            </TouchableOpacity>
          </View>
        )}
      </View>

      {/* Open Shift dialog (from the selling gate) */}
      <Modal visible={showOpenShift} transparent animationType="fade" onRequestClose={() => setShowOpenShift(false)}>
        <View style={styles.gateModalOverlay}>
          <View style={styles.gateModal}>
            <View style={styles.gateModalHead}>
              <TouchableOpacity onPress={() => setShowOpenShift(false)}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.gateModalTitle}>Open Shift</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 12 }}>
              <Text style={styles.gateModalLabel}>Start Cash in Drawer (THB)</Text>
              <TextInput
                style={styles.gateModalInput}
                value={startCash}
                onChangeText={setStartCash}
                keyboardType="decimal-pad"
                placeholder="0.00"
                selectTextOnFocus
                testID="gate-start-cash"
              />
              <TouchableOpacity
                style={[styles.shiftGateBtn, openingShift && { opacity: 0.6 }]}
                onPress={openShift}
                disabled={openingShift}
                testID="gate-confirm-open"
              >
                <Text style={styles.shiftGateBtnText}>{openingShift ? "Opening…" : "OPEN SHIFT"}</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* Mobile cart modal */}
      <Modal
        visible={showCart && !isWide}
        transparent
        animationType="slide"
        onRequestClose={() => setShowCart(false)}
      >
        <View style={styles.cartSheetOverlay}>
          <TouchableOpacity style={StyleSheet.absoluteFill} onPress={() => setShowCart(false)} />
          <View style={[styles.cartSheet, { paddingBottom: Math.max(insets.bottom, 12) + 12 }]}>
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
              onEdit={setEditItem}
              embedded
            />
          </View>
        </View>
      </Modal>

      {/* ============ MODALS ============ */}
      <PaymentModal
        visible={showPayment}
        total={total}
        itemsCount={cart.length}
        cartCount={cartCount}
        onClose={() => setShowPayment(false)}
        onPay={handlePaySuccess}
      />
      <CartItemModal
        item={editItem}
        onClose={() => setEditItem(null)}
        onSave={(pid, qty, discount) => {
          applyItemEdit(pid, qty, discount);
          setEditItem(null);
        }}
        onRemove={(pid) => {
          removeItem(pid);
          setEditItem(null);
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
        printStatus={printStatus}
        onClose={() => {
          setShowSuccess(null);
          setPrintStatus(null);
          clearCart();
        }}
      />
      <DrawerModal visible={showDrawer} onClose={() => setShowDrawer(false)} />
      {/* Shared sidebar drawer — opens via the top-bar hamburger.  Picking
          "Shop" just closes it (we're already here); other sections push
          to /admin with that section's key so admin lands directly on the
          chosen page without an intermediate Reports flash. */}
      <SidebarDrawer
        visible={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        staff={staff || "Admin"}
        role={role || ""}
        branchName={activeBranchName || undefined}
        activeKey="shop"
        onNavigate={(key) => {
          setSidebarOpen(false);
          if (key === "shop") return; // already here
          router.push({
            pathname: "/admin",
            params: {
              staff: staff || "Admin",
              role: role || "",
              branch_id: activeBranchId,
              branch_name: activeBranchName,
              section: key,
            },
          });
        }}
        onLogout={async () => {
          setSidebarOpen(false);
          await clearAuthToken();
          router.replace("/");
        }}
      />
      {/* Off-screen receipt rendering target for view-shot capture.
          Only mounts when a print is in flight; invisible to the user. */}
      <ReceiptOverlay />

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
  discountAmount,
  total,
  cartCount,
  onClear,
  onRemoveCustomer,
  onPay,
  onInc,
  onDec,
  onRemove,
  onEdit,
  embedded,
}: {
  cart: CartItem[];
  customer: Customer | null;
  subtotal: number;
  discountAmount: number;
  total: number;
  cartCount: number;
  onClear: () => void;
  onRemoveCustomer: () => void;
  onPay: () => void;
  onInc: (pid: string) => void;
  onDec: (pid: string) => void;
  onRemove: (pid: string) => void;
  onEdit: (item: CartItem) => void;
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

      {/* TODO: unify toFixed(2) → THB() formatting across cart summary for consistency */}
      <View style={styles.totalBox}>
        <View style={styles.sumTotalRow}>
          <Text style={styles.totalLabel}>Sub Total</Text>
          <Text style={styles.subTotalVal}>{subtotal.toFixed(2)}</Text>
        </View>
        {discountAmount > 0 && (
          <View style={styles.discRow}>
            <Text style={styles.discLabel}>Discount</Text>
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
          {cart.length === 0
            ? "No items"
            : `${cart.length} Item${cart.length !== 1 ? "s" : ""} / ${cartCount} pcs.`}
        </Text>
      </View>

      <FlatList
        data={cart}
        keyExtractor={(i) => i.product_id}
        style={{ flex: 1 }}
        contentContainerStyle={cart.length === 0 ? { flex: 1 } : undefined}
        ListEmptyComponent={
          <View style={styles.emptyCart}>
            <MaterialCommunityIcons name="cart-outline" size={40} color="#CBD5E1" />
            <Text style={styles.emptyCartText}>Cart is empty</Text>
            <Text style={styles.emptyCartSub}>Tap a product to add</Text>
          </View>
        }
        renderItem={({ item }) => (
          <View style={styles.cartItem} testID={`cart-item-${item.product_id}`}>
            <TouchableOpacity
              style={{ flex: 1 }}
              onPress={() => onEdit(item)}
              testID={`cart-item-edit-${item.product_id}`}
            >
              <Text style={styles.cartItemName} numberOfLines={2}>
                {item.name}
              </Text>
              <Text style={styles.cartItemPrice}>
                {THB(item.price)} × {item.qty}
              </Text>
              {!!item.discount && item.discount > 0 && (
                <Text style={styles.cartItemDisc}>Discount -{THB(item.discount)}</Text>
              )}
            </TouchableOpacity>
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

      {/* Bottom pinned Store row — matches reference design */}
      <View style={styles.cartFooterRow}>
        <View style={styles.storePill}>
          <Ionicons name="storefront" size={14} color="#FFFFFF" />
        </View>
        <Text style={styles.storeFooterText}>Store</Text>
        <Ionicons name="chevron-forward" size={16} color="#94A3B8" />
        <View style={{ flex: 1 }} />
        {cart.length > 0 && (
          <TouchableOpacity onPress={onClear} style={styles.footerTrash} testID="clear-cart">
            <Ionicons name="trash-outline" size={18} color="#EF4444" />
          </TouchableOpacity>
        )}
      </View>
    </View>
  );
}

// ---------- Payment Method Constants ----------
const PAYMENT_METHODS = {
  CASH: "Cash",
  BEAM: "Beam",
  EASY_PAY: "Easy Pay",
  CREDIT: "Credit",
  PROMPTPAY: "PromptPay",
  QR_KBANK: "QR Kbank",
  EDC: "EDC",
  CUSTOM: "Custom",
} as const;

// ---------- Payment Modal ----------
function PaymentModal({
  visible,
  total,
  itemsCount,
  cartCount,
  onClose,
  onPay,
}: {
  visible: boolean;
  total: number;
  itemsCount: number;
  cartCount: number;
  onClose: () => void;
  onPay: (method: string, paid: number, meta?: { beamChargeId?: string }) => void;
}) {
  const [amount, setAmount] = useState("");
  const [method, setMethod] = useState("Cash");

  useEffect(() => {
    if (visible) {
      setAmount("");
      setMethod("Cash");
    }
  }, [visible]);

  // For now, only Cash and Beam are exposed.  Other methods are still
  // implemented (EasyPay, Credit, PromptPay, QR Kbank, EDC, Custom) —
  // we just hide their tiles from the payment modal.  Re-enable by
  // un-commenting any of the lines below.
  const methods = [
    { key: PAYMENT_METHODS.CASH, icon: "cash-outline" as const },
    { key: PAYMENT_METHODS.BEAM, icon: "scan-outline" as const },
    // { key: PAYMENT_METHODS.EASY_PAY, icon: "qr-code-outline" as const },
    // { key: PAYMENT_METHODS.CREDIT, icon: "card-outline" as const },
    // { key: PAYMENT_METHODS.PROMPTPAY, icon: "phone-portrait-outline" as const },
    // { key: PAYMENT_METHODS.QR_KBANK, icon: "qr-code" as const },
    // { key: PAYMENT_METHODS.EDC, icon: "print-outline" as const },
    // { key: PAYMENT_METHODS.CUSTOM, icon: "wallet-outline" as const },
  ];

  const customOptions = [
    "EDC Kbank", "EDC Bangkok", "Brave Brand Co.,Ltd",
    "Thai Dot Com Pay", "คนละครึ่ง", "EDC SCB", "QR",
  ];
  const [customPick, setCustomPick] = useState("");
  const [orderRef, setOrderRef] = useState("");
  const [cardLast4, setCardLast4] = useState("");
  const [cardType, setCardType] = useState("");
  const [bankPick, setBankPick] = useState("");
  // Beam QR state
  const [beamChargeId, setBeamChargeId] = useState<string | null>(null);
  const [beamQrImage, setBeamQrImage] = useState<string | null>(null);
  const [beamStatus, setBeamStatus] = useState<"idle" | "loading" | "pending" | "completed" | "failed">("idle");
  const [beamError, setBeamError] = useState<string | null>(null);

  // Reset Beam state to idle (used by modal-open cleanup, Cancel, and Retry).
  const resetBeam = useCallback(() => {
    setBeamStatus("idle");
    setBeamChargeId(null);
    setBeamQrImage(null);
    setBeamError(null);
  }, []);

  useEffect(() => {
    if (visible) {
      setCustomPick(""); setOrderRef("");
      setCardLast4(""); setCardType(""); setBankPick("");
      resetBeam();
    }
  }, [visible, resetBeam]);

  const paid = amount ? parseFloat(amount) : total;
  const canPay = paid >= total;

  const { width: winW } = useWindowDimensions();
  const isNarrow = winW < 720;

  const onKey = (k: string) => {
    if (k === "clear") setAmount("");
    else if (k === "back") setAmount((a) => a.slice(0, -1));
    else if (k === ".") {
      if (!amount.includes(".")) setAmount((a) => (a || "0") + ".");
    } else setAmount((a) => (a === "0" ? k : a + k));
  };

  const quicks = [1000, 500, 100, 50, 20];

  // Create a Beam QR charge and start polling for completion.
  // `referenceId` is a temporary client-side ref (e.g. POS-<timestamp>) — the real order
  // is created in handlePaySuccess after the polling effect confirms COMPLETED.
  const startBeamCharge = async (referenceId: string) => {
    setBeamStatus("loading");
    setBeamError(null);
    try {
      const res = await apiFetch(`${API}/beam/charge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: total, reference_id: referenceId, description: `Order ${referenceId}` }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBeamStatus("failed");
        setBeamError(data.detail || "Failed to create Beam charge");
        return;
      }
      setBeamChargeId(data.charge_id);
      setBeamQrImage(data.qr_image || null);
      setBeamStatus(data.status === "COMPLETED" ? "completed" : "pending");
    } catch {
      setBeamStatus("failed");
      setBeamError("Cannot reach payment server");
    }
  };

  // Capture the latest onPay / total in refs so the polling effect's deps stay stable
  // and the interval doesn't get torn down on every parent re-render.
  const onPayRef = useRef(onPay);
  const totalRef = useRef(total);
  useEffect(() => { onPayRef.current = onPay; }, [onPay]);
  useEffect(() => { totalRef.current = total; }, [total]);

  // Poll Beam charge status until completed/failed
  useEffect(() => {
    if (beamStatus !== "pending" || !beamChargeId) return;
    const interval = setInterval(async () => {
      try {
        const res = await apiFetch(`${API}/beam/charge/${beamChargeId}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.status === "COMPLETED") {
          setBeamStatus("completed");
          clearInterval(interval);
          onPayRef.current("Beam QR", totalRef.current, { beamChargeId });
        } else if (data.status === "FAILED" || data.status === "EXPIRED") {
          setBeamStatus("failed");
          setBeamError("Payment " + data.status.toLowerCase() + ". Please try again.");
          clearInterval(interval);
        }
      } catch { /* ignore transient errors */ }
    }, BEAM_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [beamStatus, beamChargeId]);

  // Derived flags for the right-panel "Payment Confirm" button — extracted so
  // the JSX below stays readable.
  const isQrLikeMethod = method === PAYMENT_METHODS.QR_KBANK || method === PAYMENT_METHODS.PROMPTPAY || method === PAYMENT_METHODS.BEAM;
  const isCustomReady = method === PAYMENT_METHODS.CUSTOM && !!customPick;
  const isCreditReady = method === PAYMENT_METHODS.CREDIT && !!(cardType || bankPick);
  const isBeamBusy =
    method === PAYMENT_METHODS.BEAM &&
    (beamStatus === "loading" || beamStatus === "pending" || beamStatus === "completed");
  const canConfirm = (isQrLikeMethod || isCustomReady || isCreditReady || canPay) && !isBeamBusy;

  const confirmLabel = (() => {
    if (method !== PAYMENT_METHODS.BEAM) return "Payment Confirm";
    if (beamStatus === "loading") return "Generating…";
    if (beamStatus === "pending") return "Waiting for scan…";
    return "Generate QR";
  })();

  const handleConfirmPayment = () => {
    if (method === PAYMENT_METHODS.BEAM) {
      // Generate QR — use a temp reference ID; actual order is created when polling confirms
      const ref = `POS-${Date.now()}`;
      startBeamCharge(ref);
      return;
    }
    const finalMethod =
      method === PAYMENT_METHODS.CUSTOM && customPick
        ? `${PAYMENT_METHODS.CUSTOM} · ${customPick}`
        : method === PAYMENT_METHODS.CREDIT && (cardType || bankPick)
          ? `${PAYMENT_METHODS.CREDIT} · ${cardType || bankPick}${cardLast4 ? ` ····${cardLast4}` : ""}`
          : method;
    const finalPaid =
      method === PAYMENT_METHODS.QR_KBANK ||
      method === PAYMENT_METHODS.PROMPTPAY ||
      method === PAYMENT_METHODS.CUSTOM ||
      method === PAYMENT_METHODS.CREDIT
        ? total
        : paid;
    onPay(finalMethod, finalPaid);
  };

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

          <ScrollView
            style={{ flex: 1 }}
            contentContainerStyle={[
              styles.paymentBody,
              isNarrow && { flex: 0, flexDirection: "column", gap: 12, padding: 12 },
            ]}
          >
            {/* Methods */}
            <View style={[styles.methodsCol, isNarrow && styles.methodsColNarrow]}>
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
            {method === PAYMENT_METHODS.EASY_PAY ? (
              <View style={styles.easyPayPane} testID="easypay-pane">
                <Text style={styles.easyPayThai}>ชำระเงินครบวงจร</Text>
                <Text style={styles.easyPayTitle}>Pay Easy</Text>
                <View style={styles.easyPayBadges}>
                  {["รองรับบัตรเครดิต/เดบิต", "QR Payment ทุกธนาคาร", "e-Wallet ทุกค่าย"].map((b) => (
                    <View key={b} style={styles.easyPayBadge}><Text style={styles.easyPayBadgeText}>{b}</Text></View>
                  ))}
                </View>
                <View style={styles.easyPayCard}>
                  <View style={styles.easyPayDevicePlaceholder}>
                    <Ionicons name="tablet-landscape" size={80} color="#00B14F" />
                    <Ionicons name="qr-code" size={32} color="#0F172A" style={{ position: "absolute", bottom: 4, right: 4 }} />
                  </View>
                  <View style={styles.easyPayBrandGrid}>
                    {[
                      { name: "VISA", color: "#1A1F71" },
                      { name: "MC", color: "#EB001B" },
                      { name: "UP", color: "#E21836" },
                      { name: "JCB", color: "#0E4C96" },
                      { name: "LINE", color: "#06C755" },
                      { name: "True", color: "#EF4444" },
                      { name: "Ali", color: "#1677FF" },
                      { name: "PP", color: "#00457C" },
                      { name: "Shop", color: "#EE4D2D" },
                      { name: "KB", color: "#138F2D" },
                      { name: "SCB", color: "#4E2D80" },
                      { name: "Laz", color: "#0F146D" },
                    ].map((b) => (
                      <View key={b.name} style={[styles.easyPayBrandPill, { backgroundColor: b.color }]}>
                        <Text style={styles.easyPayBrandText}>{b.name}</Text>
                      </View>
                    ))}
                  </View>
                </View>
                <TouchableOpacity style={styles.easyPayRegister}>
                  <Text style={styles.easyPayRegisterText}>สมัคร ใช้บริการ</Text>
                </TouchableOpacity>
              </View>
            ) : method === PAYMENT_METHODS.PROMPTPAY ? (
              <View style={styles.promptPayPane} testID="promptpay-pane">
                <View style={styles.thaiQrHeader}>
                  <Ionicons name="grid" size={28} color="#fff" />
                  <Text style={styles.thaiQrTitle}>THAI QR{"\n"}PAYMENT</Text>
                </View>
                <View style={styles.promptPayLogoBox}>
                  <View style={styles.promptPayLogoPill}>
                    <Text style={styles.promptPayLogoText}>PromptPay</Text>
                  </View>
                </View>
                <Text style={styles.promptPayInvalid}>Invalid PromptPay ID</Text>
                <Text style={styles.promptPayHint}>
                  Please enter Mobile No. / Citizen ID / Tax ID in Shop{"\n"}Setting -{">"} Payment
                </Text>
                <TouchableOpacity style={styles.printQrBtn}>
                  <Ionicons name="print-outline" size={16} color="#475569" />
                  <Text style={styles.printQrText}>Print QR Code</Text>
                </TouchableOpacity>
              </View>
            ) : method === PAYMENT_METHODS.QR_KBANK ? (
              <View style={styles.qrKbankPane} testID="qrkbank-pane">
                <View style={styles.thaiQrHeader}>
                  <Ionicons name="grid" size={28} color="#fff" />
                  <Text style={styles.thaiQrTitle}>THAI QR{"\n"}PAYMENT</Text>
                </View>
                <View style={styles.kbankBrandRow}>
                  {[
                    { t: "PromptPay", bg: "#00457C" },
                    { t: "VISA", bg: "#1A1F71" },
                    { t: "Mastercard", bg: "#EB001B" },
                    { t: "UnionPay", bg: "#E21836" },
                  ].map((p) => (
                    <View key={p.t} style={[styles.kbankBrandPill, { backgroundColor: p.bg }]}>
                      <Text style={styles.kbankBrandText}>{p.t}</Text>
                    </View>
                  ))}
                </View>
                <Text style={styles.kbankSupportText}>
                  Support payment type Thai QR (PromptPay), Credit Card
                </Text>
                <View style={styles.kbankByRow}>
                  <Text style={styles.kbankByLabel}>By</Text>
                  <View style={[styles.kbankLogoPill, { backgroundColor: "#138F2D" }]}>
                    <Text style={styles.kbankLogoText}>KBank</Text>
                  </View>
                </View>
                <View style={styles.kbankIllustration}>
                  <Ionicons name="phone-portrait-outline" size={56} color="#475569" />
                  <Ionicons name="qr-code" size={32} color="#0F172A" style={{ position: "absolute", bottom: 0, right: 0 }} />
                </View>
                <TouchableOpacity style={styles.kbankRegisterBtn}>
                  <Text style={styles.kbankRegisterText}>Register</Text>
                </TouchableOpacity>
              </View>
            ) : method === PAYMENT_METHODS.BEAM ? (
              <View style={styles.beamPane} testID="beam-pane">
                {/* Header */}
                <View style={styles.beamHeader}>
                  <View style={styles.beamLogoBox}>
                    <Ionicons name="scan-outline" size={28} color="#00B14F" />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.beamTitle}>Beam QR Payment</Text>
                    <Text style={styles.beamSub}>PromptPay · e-Wallet · All banks</Text>
                  </View>
                </View>

                {beamStatus === "idle" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="qr-code-outline" size={72} color="#CBD5E1" />
                    <Text style={styles.beamIdleText}>{'Tap "Generate QR" to create a QR code'}</Text>
                  </View>
                )}

                {beamStatus === "loading" && (
                  <View style={styles.beamIdleBox}>
                    <ActivityIndicator size="large" color="#00B14F" />
                    <Text style={styles.beamIdleText}>Generating QR code…</Text>
                  </View>
                )}

                {(beamStatus === "pending") && beamQrImage && (
                  <View style={styles.beamQrBox}>
                    <Image
                      source={{ uri: beamQrImage.startsWith("data:") ? beamQrImage : `data:image/png;base64,${beamQrImage}` }}
                      style={styles.beamQrImage}
                      resizeMode="contain"
                    />
                    <View style={styles.beamWaiting}>
                      <ActivityIndicator size="small" color="#00B14F" />
                      <Text style={styles.beamWaitingText}>Waiting for customer to scan…</Text>
                    </View>
                    <TouchableOpacity
                      style={styles.beamCancelBtn}
                      onPress={resetBeam}
                      testID="beam-cancel"
                    >
                      <Text style={styles.beamCancelText}>Cancel</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {beamStatus === "failed" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="alert-circle-outline" size={48} color="#EF4444" />
                    <Text style={[styles.beamIdleText, { color: "#EF4444" }]}>{beamError || "Payment failed"}</Text>
                    <TouchableOpacity
                      style={styles.beamRetryBtn}
                      onPress={resetBeam}
                    >
                      <Text style={styles.beamRetryText}>Try Again</Text>
                    </TouchableOpacity>
                  </View>
                )}

                <Text style={styles.beamAmount}>{THB(total)}</Text>
              </View>
            ) : method === PAYMENT_METHODS.EDC ? (
              <View style={styles.edcPane} testID="edc-pane">
                <View style={styles.edcHeroRow}>
                  <View style={styles.edcDeviceGroup}>
                    <Ionicons name="tablet-landscape" size={64} color="#0F172A" />
                    <Ionicons name="phone-portrait-outline" size={40} color="#0F172A" />
                  </View>
                  <View style={styles.edcArrowWrap}>
                    <View style={styles.edcArrowLine} />
                    <Text style={styles.edcArrowLabel}>Send data</Text>
                  </View>
                  <Ionicons name="print" size={64} color="#00B14F" />
                </View>
                <Text style={styles.edcTitle}>
                  Connect Brave POS with Electronic Data Capture (EDC) payment terminal no need
                  to manual input data, supporting both credit and debit cards (VISA, MasterCard,
                  JCB, UnionPay)
                </Text>
                <View style={styles.edcByRow}>
                  <Text style={styles.edcBy}>โดย</Text>
                  <View style={[styles.kbankLogoPill, { backgroundColor: "#138F2D" }]}>
                    <Text style={styles.kbankLogoText}>KBank</Text>
                  </View>
                </View>
                <Ionicons name="phone-portrait-outline" size={32} color="#475569" style={{ marginTop: 4 }} />
                <TouchableOpacity style={styles.edcRegister} testID="edc-register">
                  <Text style={styles.edcRegisterText}>สมัครใช้บริการ</Text>
                </TouchableOpacity>
              </View>
            ) : method === PAYMENT_METHODS.CUSTOM ? (
              <View style={styles.customPane} testID="custom-pane">
                <View style={styles.customRefRow}>
                  <Text style={styles.customRefLabel}>Order Ref.</Text>
                  <TextInput
                    placeholder="Optional"
                    style={styles.customRefInput}
                    value={orderRef}
                    onChangeText={setOrderRef}
                    placeholderTextColor="#94A3B8"
                    testID="custom-order-ref"
                  />
                </View>
                <View style={styles.customAmountRow}>
                  <Text style={styles.customAmountLabel}>Amount</Text>
                  <Text style={styles.customAmountVal}>{THB(total)}</Text>
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
            ) : method === PAYMENT_METHODS.CREDIT ? (
              <ScrollView style={{ flex: 1 }} contentContainerStyle={styles.creditPane} testID="credit-pane">
                {/* TODO: consider using THB(total) here for consistency; currently "THB" is a separate label */}
                <View style={styles.creditAmtRow}>
                  <Text style={styles.creditAmtLabel}>THB</Text>
                  <Text style={styles.creditAmtVal}>{total.toFixed(2)}</Text>
                </View>
                <TextInput
                  placeholder="Card Number (Last 4 digits)"
                  placeholderTextColor="#94A3B8"
                  style={styles.creditInput}
                  value={cardLast4}
                  onChangeText={(v) => setCardLast4(v.replace(/[^0-9]/g, "").slice(0, 4))}
                  maxLength={4}
                  keyboardType="number-pad"
                  testID="card-last4"
                />
                <Text style={styles.creditDivider}>เลือกประเภทบัตรเครดิต</Text>
                <View style={styles.creditGrid}>
                  {[
                    { k: "VISA", icon: "card" as const, color: "#1A1F71" },
                    { k: "MASTER CARD", icon: "card" as const, color: "#EB001B" },
                    { k: "JCB", icon: "card" as const, color: "#0E4C96" },
                    { k: "Union Pay", icon: "card" as const, color: "#E21836" },
                  ].map((c) => (
                    <TouchableOpacity
                      key={c.k}
                      style={[styles.creditOption, cardType === c.k && styles.creditOptionActive]}
                      onPress={() => setCardType(c.k)}
                      testID={`card-${c.k}`}
                    >
                      <View style={[styles.radio, cardType === c.k && styles.radioActive]}>
                        {cardType === c.k && <View style={styles.radioInner} />}
                      </View>
                      <Ionicons name={c.icon} size={18} color={c.color} style={{ marginHorizontal: 6 }} />
                      <Text style={styles.creditOptionText}>{c.k}</Text>
                    </TouchableOpacity>
                  ))}
                </View>
                <Text style={styles.creditDivider}>หรือ เลือกธนาคาร</Text>
                <View style={styles.creditGrid}>
                  {[
                    { k: "WELFARE", th: "บัตรสวัสดิการแห่งรัฐ", color: "#3B82F6" },
                    { k: "BAY", th: "กรุงศรีอยุธยา", color: "#F59E0B" },
                    { k: "BBL", th: "กรุงเทพ", color: "#0EA5E9" },
                    { k: "KTC", th: "กรุงไทย", color: "#06B6D4" },
                  ].map((b) => (
                    <TouchableOpacity
                      key={b.k}
                      style={[styles.creditOption, bankPick === b.k && styles.creditOptionActive]}
                      onPress={() => setBankPick(b.k)}
                      testID={`bank-${b.k}`}
                    >
                      <View style={[styles.radio, bankPick === b.k && styles.radioActive]}>
                        {bankPick === b.k && <View style={styles.radioInner} />}
                      </View>
                      <View style={[styles.bankBadge, { backgroundColor: b.color }]}>
                        <Text style={styles.bankBadgeText}>{b.k[0]}</Text>
                      </View>
                      <View style={{ flex: 1 }}>
                        <Text style={styles.creditOptionText} numberOfLines={1}>{b.th}</Text>
                        <Text style={styles.bankSubText}>{b.k}</Text>
                      </View>
                    </TouchableOpacity>
                  ))}
                </View>
              </ScrollView>
            ) : (
              <View style={[styles.padCol, isNarrow && { width: "100%", minHeight: 0 }]}>
                <View style={[styles.amountDisplay, isNarrow && { padding: 12 }]}>
                  <Text style={styles.thbSmall}>THB</Text>
                  <Text style={[styles.amountText, isNarrow && { fontSize: 26 }]} testID="amount-display">
                    {amount || "0"}
                  </Text>
                </View>
                {/* On phone the quicks row above the keypad gives each pad
                    button the full width instead of squeezing it into ~70%. */}
                {isNarrow && (
                  <View style={styles.quickRowMobile}>
                    {quicks.map((q) => (
                      <TouchableOpacity
                        key={q}
                        style={styles.quickChipMobile}
                        onPress={() =>
                          setAmount((a) => {
                            const cur = parseFloat(a || "0");
                            return String(cur + q);
                          })
                        }
                        testID={`quick-${q}`}
                      >
                        <Text style={styles.quickChipMobileText}>+{q}</Text>
                      </TouchableOpacity>
                    ))}
                  </View>
                )}
                {/* Numpad + (on tablet/desktop only) quick amounts side by side */}
                <View style={styles.padWithQuicks}>
                  <View style={styles.padGridWrap}>
                    <View style={styles.padGrid}>
                      {["7", "8", "9", "4", "5", "6", "1", "2", "3", "0", "."].map((k) => (
                        <TouchableOpacity
                          key={k}
                          style={[styles.padBtn, isNarrow && { paddingVertical: 12 }]}
                          onPress={() => onKey(k)}
                          testID={`pad-${k}`}
                        >
                          <Text style={[styles.padText, isNarrow && { fontSize: 22 }]}>{k}</Text>
                        </TouchableOpacity>
                      ))}
                    </View>
                    {/* Clear + Backspace row */}
                    <View style={styles.padBottomRow}>
                      <TouchableOpacity
                        style={[styles.clearPadBtnHalf, isNarrow && { paddingVertical: 8 }]}
                        onPress={() => onKey("clear")}
                        testID="pad-clear"
                      >
                        <Text style={styles.clearPadText}>Clear</Text>
                      </TouchableOpacity>
                      <TouchableOpacity
                        style={[styles.backspacePadBtn, isNarrow && { paddingVertical: 8 }]}
                        onPress={() => onKey("back")}
                        testID="pad-back"
                      >
                        <Ionicons name="backspace-outline" size={22} color="#EF4444" />
                      </TouchableOpacity>
                    </View>
                  </View>
                  {/* Quick amounts column — only on tablet/desktop */}
                  {!isNarrow && (
                  <View style={styles.quickColInline}>
                    {quicks.map((q) => (
                      <TouchableOpacity
                        key={q}
                        style={styles.quickBtnInline}
                        onPress={() =>
                          setAmount((a) => {
                            const cur = parseFloat(a || "0");
                            return String(cur + q);
                          })
                        }
                        testID={`quick-${q}`}
                      >
                        <Text style={styles.quickTextInline}>{q.toLocaleString()}</Text>
                      </TouchableOpacity>
                    ))}
                  </View>
                  )}
                </View>
              </View>
            )}

            {/* Right column: totals */}
            <View style={[styles.quickCol, isNarrow && { width: "100%" }]}>
              <View style={styles.netBoxV2}>
                <View style={styles.rightPanelTop}>
                  <View style={styles.guestRow}>
                    <Ionicons name="person-outline" size={14} color="#475569" />
                    <Text style={styles.guestText}>Guest</Text>
                  </View>
                  <View style={styles.netRowV2}>
                    <Text style={styles.netLabelV2}>Net Total</Text>
                    <Text style={styles.netValV2}>{THB(total)}</Text>
                  </View>
                  <TouchableOpacity onPress={() => setAmount(String(total))} testID="net-total">
                    <View style={styles.tapEqualRow}>
                      <Text style={styles.tapEqualLabel}>Tap to equal Total</Text>
                      <Text style={styles.tapEqualVal}>{THB(total)}</Text>
                    </View>
                  </TouchableOpacity>
                </View>

                <View style={styles.rightPanelMiddle}>
                  <TouchableOpacity
                    style={[styles.payConfirmBtn, !canConfirm && styles.payBtnDisabled]}
                    disabled={!canConfirm}
                    onPress={handleConfirmPayment}
                    testID="confirm-payment-right"
                  >
                    <Text style={styles.payConfirmText}>{confirmLabel}</Text>
                  </TouchableOpacity>
                  <Text style={styles.itemCountText}>{itemsCount} Item{itemsCount !== 1 ? "s" : ""} / {cartCount} pcs.</Text>
                </View>

                {/* Sticky footer recap — intentionally repeats total for at-a-glance confirmation
                   when the panel is tall and "Net Total" scrolls out of view */}
                <View style={styles.summaryCard}>
                  <Text style={styles.summaryLabel}>Summary</Text>
                  <Text style={styles.summaryValue}>{THB(total)}</Text>
                </View>
              </View>
            </View>
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}

// ---------- Cart Item Modal (per-line quantity + discount) ----------
function CartItemModal({
  item,
  onClose,
  onSave,
  onRemove,
}: {
  item: CartItem | null;
  onClose: () => void;
  onSave: (pid: string, qty: number, discount: number) => void;
  onRemove: (pid: string) => void;
}) {
  const [qty, setQty] = useState(1);
  const [disc, setDisc] = useState("");

  useEffect(() => {
    if (item) {
      setQty(item.qty);
      setDisc(item.discount ? String(item.discount) : "");
    }
  }, [item]);

  if (!item) return null;

  const discNum = Math.max(0, parseFloat(disc) || 0);
  const lineTotal = Math.max(0, item.price * qty - discNum);

  return (
    <Modal visible transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.itemModal} testID="cart-item-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose} testID="close-item-modal">
              <Ionicons name="chevron-back" size={26} color="#00B14F" />
            </TouchableOpacity>
            <Text style={styles.modalTitle} numberOfLines={1}>
              {item.name}
            </Text>
            <TouchableOpacity onPress={() => onRemove(item.product_id)} testID="item-modal-remove">
              <Ionicons name="trash-outline" size={22} color="#EF4444" />
            </TouchableOpacity>
          </View>

          <View style={styles.itemRow}>
            <Text style={styles.itemRowLabel}>Quantity</Text>
            <View style={styles.itemStepper}>
              <TouchableOpacity
                style={styles.itemStepBtn}
                onPress={() => setQty((q) => Math.max(1, q - 1))}
                testID="item-qty-dec"
              >
                <Ionicons name="remove" size={20} color="#0F172A" />
              </TouchableOpacity>
              <Text style={styles.itemStepVal}>{qty}</Text>
              <TouchableOpacity
                style={styles.itemStepBtn}
                onPress={() => setQty((q) => q + 1)}
                testID="item-qty-inc"
              >
                <Ionicons name="add" size={20} color="#0F172A" />
              </TouchableOpacity>
            </View>
          </View>

          <View style={styles.itemRow}>
            <Text style={styles.itemRowLabel}>Discount (฿)</Text>
            <TextInput
              style={styles.itemDiscInput}
              value={disc}
              onChangeText={(t) => setDisc(t.replace(/[^0-9.]/g, ""))}
              keyboardType="decimal-pad"
              placeholder="0.00"
              placeholderTextColor="#CBD5E1"
              testID="item-discount-input"
            />
          </View>

          <View style={styles.itemTotalRow}>
            <Text style={styles.itemRowLabel}>Line Total</Text>
            <Text style={styles.itemTotalVal}>{THB(lineTotal)}</Text>
          </View>

          <TouchableOpacity
            style={styles.doneBtn}
            onPress={() => onSave(item.product_id, qty, discNum)}
            testID="item-modal-save"
          >
            <Text style={styles.doneBtnText}>Save</Text>
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
  const [phoneValid, setPhoneValid] = useState(true);  // true when empty or well-formed

  useEffect(() => {
    if (visible) {
      setShowAdd(false);
      setQ("");
      apiFetch(`${API}/customers`)
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
    if (!name.trim() || !phoneValid) return;
    const res = await apiFetch(`${API}/customers`, {
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
              <PhoneInput
                value={phone}
                onChange={(e164, valid) => { setPhone(e164); setPhoneValid(valid); }}
                placeholder="Phone (optional)"
                defaultCountryCode="TH"
                testID="new-cust-phone"
              />
              <TouchableOpacity
                style={[styles.saveCustBtn, (!name.trim() || !phoneValid) && { opacity: 0.4 }]}
                onPress={addCustomer}
                disabled={!name.trim() || !phoneValid}
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
  const [expandedCol, setExpandedCol] = useState<string | null>("new");
  const { width: winW } = useWindowDimensions();
  const isNarrow = winW < 720;

  const deliveryCtrl = (
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
  );

  const load = async (source: string) => {
    const url = source === "all" ? `${API}/orders` : `${API}/orders?source=${source}`;
    const res = await apiFetch(url);
    const body = await res.json().catch(() => []);
    setOrders(Array.isArray(body) ? body : []);
  };

  useEffect(() => {
    if (visible) load(tab);
  }, [visible, tab]);

  const cols: { key: string; label: string; icon: any; color: string }[] = [
    { key: "new", label: "New Order", icon: "list-outline", color: "#F59E0B" },
    { key: "preparing", label: "Preparing", icon: "restaurant-outline", color: "#3B82F6" },
    { key: "completed", label: "Completed", icon: "checkmark-circle-outline", color: "#00B14F" },
    { key: "cancel", label: "Cancel", icon: "close-circle-outline", color: "#EF4444" },
  ];

  const grouped = (col: string) => orders.filter((o) => o.status === col);

  const updateStatus = async (id: string, status: string) => {
    await apiFetch(`${API}/orders/${id}/status`, {
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
            {isNarrow ? <View style={{ width: 26 }} /> : deliveryCtrl}
          </View>
          {isNarrow && (
            <View style={styles.deliveryCtrlNarrow}>{deliveryCtrl}</View>
          )}

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

          <ScrollView
            style={{ flex: 1 }}
            showsVerticalScrollIndicator={false}
            contentContainerStyle={[styles.kanban, isNarrow && styles.kanbanNarrow]}
          >
            {cols.map((c) => {
              const items = grouped(c.key);
              const ListContainer: any = isNarrow ? View : ScrollView;
              const listProps = isNarrow ? {} : { style: { flex: 1 } };
              const isExpanded = !isNarrow || expandedCol === c.key;
              const HeadContainer: any = isNarrow ? TouchableOpacity : View;
              const headProps = isNarrow
                ? { onPress: () => setExpandedCol((cur) => (cur === c.key ? null : c.key)), activeOpacity: 0.7 }
                : {};
              return (
                <View
                  key={c.key}
                  style={[styles.kanCol, isNarrow && styles.kanColNarrow]}
                  testID={`kan-col-${c.key}`}
                >
                  <HeadContainer style={styles.kanHead} {...headProps}>
                    <Ionicons name={c.icon} size={18} color={c.color} />
                    <Text style={styles.kanTitle}>{c.label}</Text>
                    <View style={[styles.kanCount, { backgroundColor: c.color }]}>
                      <Text style={styles.kanCountText}>{items.length}</Text>
                    </View>
                    {isNarrow && (
                      <Ionicons
                        name={isExpanded ? "chevron-up" : "chevron-down"}
                        size={18}
                        color="#94A3B8"
                      />
                    )}
                  </HeadContainer>
                  {isExpanded && (
                  <ListContainer {...listProps}>
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
                  </ListContainer>
                  )}
                </View>
              );
            })}
          </ScrollView>
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
    const res = await apiFetch(`${API}/parked-orders`);
    setParked(await res.json());
  };
  useEffect(() => {
    if (visible) load();
  }, [visible]);

  const del = async (id: string) => {
    await apiFetch(`${API}/parked-orders/${id}`, { method: "DELETE" });
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
type PrintStatus =
  | null
  | { state: "printing" }
  | { state: "printed" }
  | { state: "queued"; error?: string };

function SuccessModal({
  data,
  printStatus,
  onClose,
}: {
  data: null | { order_number: string; total: number; paid: number; change: number; method: string };
  printStatus: PrintStatus;
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

            <PrintStatusPill status={printStatus} />

            <TouchableOpacity style={styles.successBtn} onPress={onClose} testID="success-done">
              <Text style={styles.successBtnText}>Done · New Order</Text>
            </TouchableOpacity>
          </View>
        )}
      </View>
    </Modal>
  );
}

function PrintStatusPill({ status }: { status: PrintStatus }) {
  if (!status) return null;

  if (status.state === "printing") {
    return (
      <View style={[styles.printPill, styles.printPillNeutral]} testID="print-status-printing">
        <ActivityIndicator size="small" color="#475569" />
        <Text style={styles.printPillText}>Printing receipt…</Text>
      </View>
    );
  }

  if (status.state === "printed") {
    return (
      <View style={[styles.printPill, styles.printPillOk]} testID="print-status-printed">
        <Ionicons name="checkmark-circle" size={16} color="#00B14F" />
        <Text style={[styles.printPillText, { color: "#00875A" }]}>Receipt printed</Text>
      </View>
    );
  }

  // queued — printer unreachable; useStarPrinter has saved the job and
  // will retry every 30s until the printer comes back online.
  return (
    <View style={[styles.printPill, styles.printPillWarn]} testID="print-status-queued">
      <Ionicons name="time-outline" size={16} color="#B45309" />
      <View style={{ flex: 1 }}>
        <Text style={[styles.printPillText, { color: "#92400E", fontWeight: "700" }]}>
          Printer offline — receipt queued
        </Text>
        <Text style={styles.printPillSub}>Will print automatically when the printer is back online.</Text>
      </View>
    </View>
  );
}

// ---------- Drawer Modal ----------
function DrawerModal({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <TouchableOpacity style={styles.drawerOverlay} onPress={onClose} activeOpacity={1}>
        <TouchableOpacity activeOpacity={1} style={styles.drawerPanel} testID="drawer-panel">
          <Text style={styles.drawerTitle}>Cash Drawer</Text>
          <Text style={styles.drawerSub}>{"Today's quick view"}</Text>

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

/** Workaround for React Native Web typing gap — `marginTop: "auto"` is valid CSS but not typed. */
const MARGIN_TOP_AUTO = { marginTop: "auto" as any };

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
  branchChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
    maxWidth: 200,
  },
  branchChipText: { fontSize: 12, color: "#0F172A", fontWeight: "600" },
  mobileTopRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 12,
    paddingTop: 8,
  },
  branchChipMobile: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 8,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
    maxWidth: 130,
  },
  branchChipMobileText: { fontSize: 12, color: "#0F172A", fontWeight: "600" },
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
    flex: 1,
    backgroundColor: "#FFFFFF",
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    paddingTop: 10,
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
    width: 112,
    backgroundColor: "#FFFFFF",
    borderRightWidth: 1,
    borderRightColor: "#E2E8F0",
    paddingVertical: 6,
  },
  catPill: {
    marginHorizontal: 8,
    marginVertical: 4,
    paddingHorizontal: 8,
    paddingVertical: 14,
    borderRadius: 10,
    backgroundColor: "#00B14F",
    alignItems: "center",
    justifyContent: "center",
    minHeight: 64,
  },
  catPillActive: {
    backgroundColor: "#FFFFFF",
    borderWidth: 2,
    borderColor: "#00B14F",
  },
  catText: { fontSize: 11, fontWeight: "700", color: "#FFFFFF", textAlign: "center" },
  catTextActive: { color: "#00B14F" },
  catSub: { fontSize: 9, color: "#E5F7ED", marginTop: 2, textAlign: "center" },
  catSubActive: { color: "#00B14F" },

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
    flexDirection: "column",
  },
  rightCartEmbedded: {
    flex: 1,
    width: "100%",
    borderLeftWidth: 0,
  },
  cartHeader: { alignItems: "center", marginBottom: 8 },
  tablePill: {
    alignSelf: "center",
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 12,
    paddingVertical: 6,
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
    backgroundColor: "#FFFFFF",
    paddingVertical: 14,
    marginBottom: 10,
  },
  sumTotalRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 4 },
  totalLabel: { fontSize: 13, color: "#475569", fontWeight: "500" },
  subTotalVal: { fontSize: 14, color: "#0F172A", fontWeight: "600" },
  discRow: { flexDirection: "row", justifyContent: "space-between", marginTop: 6 },
  discLabel: { fontSize: 11, color: "#EF4444" },
  discVal: { fontSize: 12, color: "#EF4444", fontWeight: "600" },
  totalRow: {
    flexDirection: "row",
    alignItems: "baseline",
    justifyContent: "space-between",
    marginTop: 8,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: "#E2E8F0",
  },
  thbText: { fontSize: 14, color: "#94A3B8", fontWeight: "600" },
  totalVal: { fontSize: 36, fontWeight: "300", color: "#0F172A", letterSpacing: -1 },
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
  cartFooterRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 10,
    paddingHorizontal: 6,
    borderTopWidth: 1,
    borderTopColor: "#E2E8F0",
    marginTop: 6,
  },
  storePill: {
    width: 26,
    height: 26,
    borderRadius: 6,
    backgroundColor: "#00B14F",
    alignItems: "center",
    justifyContent: "center",
  },
  storeFooterText: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  footerTrash: {
    width: 34,
    height: 34,
    borderRadius: 8,
    backgroundColor: "#FEE2E2",
    alignItems: "center",
    justifyContent: "center",
  },
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
  modalTitle: { fontSize: 17, fontWeight: "700", color: "#0F172A", flex: 1, textAlign: "center", marginHorizontal: 8 },

  // Cart item edit modal (per-line qty + discount)
  itemModal: { width: "100%", maxWidth: 440, backgroundColor: "#FFFFFF", borderRadius: 16, overflow: "hidden" },
  itemRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 16,
    borderBottomWidth: 1,
    borderBottomColor: "#F1F5F9",
  },
  itemRowLabel: { fontSize: 15, color: "#0F172A", fontWeight: "600" },
  itemStepper: { flexDirection: "row", alignItems: "center", gap: 18 },
  itemStepBtn: {
    width: 38,
    height: 38,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    alignItems: "center",
    justifyContent: "center",
  },
  itemStepVal: { fontSize: 18, fontWeight: "700", minWidth: 28, textAlign: "center", color: "#0F172A" },
  itemDiscInput: {
    minWidth: 110,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 8,
    fontSize: 16,
    fontWeight: "700",
    color: "#EF4444",
    textAlign: "right",
  },
  itemTotalRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 16,
  },
  itemTotalVal: { fontSize: 18, fontWeight: "800", color: "#00B14F" },
  cartItemDisc: { fontSize: 11, color: "#EF4444", marginTop: 2, fontWeight: "600" },

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
  methodsColNarrow: {
    width: "100%",
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  methodBtn: {
    padding: 14,
    borderRadius: 12,
    borderWidth: 2,
    borderColor: "#E2E8F0",
    alignItems: "center",
    gap: 6,
    minWidth: 100,
    flexGrow: 1,
    flexBasis: 100,
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

  // Numpad + quick amounts side-by-side layout
  padWithQuicks: {
    flex: 1,
    flexDirection: "row",
    gap: 8,
  },
  padGridWrap: {
    flex: 1,
    gap: 8,
  },
  padGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  padBtn: {
    width: "31.5%",
    flexGrow: 1,
    maxWidth: "33.33%",
    paddingVertical: 14,
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  padText: { fontSize: 24, fontWeight: "600", color: "#0F172A" },

  // Bottom row: Clear + Backspace side-by-side
  padBottomRow: {
    flexDirection: "row",
    gap: 8,
  },
  clearPadBtnHalf: {
    flex: 1,
    backgroundColor: "#FEF0D9",
    borderRadius: 12,
    paddingVertical: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  backspacePadBtn: {
    flex: 1,
    backgroundColor: "#FEE2E2",
    borderRadius: 12,
    paddingVertical: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  clearPadBtn: {
    backgroundColor: "#FEF3C7",
    padding: 12,
    borderRadius: 12,
    alignItems: "center",
  },
  clearPadText: { fontSize: 16, fontWeight: "700", color: "#D97706" },

  // Quick amounts as a vertical column beside numpad
  quickColInline: {
    width: 64,
    gap: 8,
    justifyContent: "flex-start",
  },
  quickBtnInline: {
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    paddingVertical: 12,
  },
  quickTextInline: { fontSize: 14, fontWeight: "700", color: "#00B14F" },
  // Quick-amount chips shown horizontally above the keypad on phone, so the
  // numpad gets the full width instead of fighting with a side column.
  quickRowMobile: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
  },
  quickChipMobile: {
    backgroundColor: "#E5F7ED",
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 999,
  },
  quickChipMobileText: { fontSize: 13, fontWeight: "700", color: "#00B14F" },

  quickCol: { width: 168, minWidth: 168, gap: 12 },
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

  // Credit pane
  creditPane: { padding: 16, gap: 14 },
  creditAmtRow: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    paddingHorizontal: 16, paddingVertical: 14,
    backgroundColor: "#F1F5F9", borderRadius: 12,
  },
  creditAmtLabel: { fontSize: 14, color: "#475569", fontWeight: "600" },
  creditAmtVal: { fontSize: 22, fontWeight: "700", color: "#0F172A" },
  creditInput: {
    height: 46, paddingHorizontal: 14,
    backgroundColor: "#FFFFFF", borderWidth: 1, borderColor: "#E2E8F0",
    borderRadius: 10, fontSize: 14, color: "#0F172A",
  },
  creditDivider: {
    fontSize: 12, fontWeight: "600", color: "#475569",
    textAlign: "center", marginVertical: 4,
  },
  creditGrid: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  creditOption: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 12, paddingVertical: 12,
    backgroundColor: "#FFFFFF",
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    width: "48%",
    gap: 4,
  },
  creditOptionActive: { borderColor: "#00B14F", backgroundColor: "#F0FDF4" },
  creditOptionText: { fontSize: 13, color: "#0F172A", fontWeight: "600" },
  bankBadge: {
    width: 24, height: 24, borderRadius: 6, marginRight: 6,
    alignItems: "center", justifyContent: "center",
  },
  bankBadgeText: { color: "#FFF", fontSize: 12, fontWeight: "700" },
  bankSubText: { fontSize: 10, color: "#94A3B8", fontWeight: "600" },
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

  // Payment right panel v2 (matching screenshot)
  netBoxV2: {
    flex: 1,
    backgroundColor: "#FFFFFF",
    borderRadius: 16,
    padding: 14,
    gap: 14,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  rightPanelTop: {
    flex: 1,
    gap: 12,
  },
  rightPanelMiddle: {
    gap: 8,
  },
  guestRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingBottom: 10,
    borderBottomWidth: 1,
    borderBottomColor: "#F1F5F9",
  },
  guestText: { fontSize: 13, color: "#475569", fontWeight: "600" },
  netRowV2: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  netLabelV2: { fontSize: 13, color: "#475569", fontWeight: "600" },
  netValV2: { fontSize: 18, color: "#0F172A", fontWeight: "700" },
  tapEqualRow: {
    flexDirection: "column",
    alignItems: "flex-start",
    gap: 2,
    backgroundColor: "#F0FDF4",
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderWidth: 1,
    borderColor: "#BBF7D0",
  },
  tapEqualLabel: { fontSize: 12, color: "#00B14F", fontWeight: "600" },
  tapEqualVal: { fontSize: 18, color: "#EF4444", fontWeight: "700" },
  payConfirmBtn: {
    backgroundColor: "#00B14F",
    paddingVertical: 15,
    paddingHorizontal: 14,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    minHeight: 52,
  },
  payConfirmText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },
  itemCountText: {
    fontSize: 11,
    color: "#64748B",
    textAlign: "center",
    marginTop: 2,
    lineHeight: 16,
  },
  summaryCard: {
    paddingTop: 12,
    borderTopWidth: 1,
    borderTopColor: "#F1F5F9",
    gap: 4,
  },
  summaryLabel: { fontSize: 12, color: "#64748B", fontWeight: "600" },
  summaryValue: { fontSize: 20, color: "#0F172A", fontWeight: "700" },

  // EDC marketing pane
  edcPane: {
    flex: 1,
    backgroundColor: "#FFFFFF",
    borderRadius: 14,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    padding: 24,
    alignItems: "center",
    gap: 14,
  },
  edcHero: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    marginVertical: 8,
  },
  edcTitle: {
    fontSize: 17,
    fontWeight: "700",
    color: "#0F172A",
    textAlign: "center",
    lineHeight: 24,
  },
  edcBody: {
    fontSize: 13,
    color: "#475569",
    textAlign: "center",
    lineHeight: 20,
  },
  edcBy: { fontSize: 12, color: "#94A3B8" },
  edcRegister: {
    backgroundColor: "#00B14F",
    paddingVertical: 14,
    paddingHorizontal: 40,
    borderRadius: 10,
    marginTop: 8,
  },
  edcRegisterText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },

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
    width: "92%",
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
  scanBarBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 6,
    backgroundColor: "#F1F5F9",
  },
  scanBarText: { fontSize: 10, color: "#475569", fontWeight: "600" },
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
    width: "92%",
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
  deliveryCtrl: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap" },
  deliveryCtrlNarrow: {
    paddingHorizontal: 16,
    paddingTop: 10,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
    paddingBottom: 10,
  },
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
  kanbanNarrow: { flex: 0, flexDirection: "column" },
  kanCol: { flex: 1, backgroundColor: "#F8FAFC", borderRadius: 12, padding: 10 },
  kanColNarrow: { flex: 0, width: "100%" },
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
    width: "92%",
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
    width: "92%",
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
    paddingHorizontal: 20,
    borderRadius: 12,
    marginTop: 20,
    width: "100%",
    alignItems: "center",
  },
  successBtnText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },

  // Print status pill shown inside SuccessModal — tells the cashier
  // whether the local printer actually printed the receipt or whether
  // it got queued for retry because the printer was offline.
  printPill: {
    width: "100%",
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 10,
    paddingHorizontal: 12,
    borderRadius: 10,
    marginTop: 12,
    borderWidth: 1,
  },
  printPillNeutral: { backgroundColor: "#F1F5F9", borderColor: "#E2E8F0" },
  printPillOk: { backgroundColor: "#ECFDF5", borderColor: "#A7F3D0" },
  printPillWarn: { backgroundColor: "#FEF3C7", borderColor: "#FCD34D" },
  printPillText: { fontSize: 13, color: "#475569", fontWeight: "600" },
  printPillSub: { fontSize: 11, color: "#92400E", marginTop: 2 },

  // Selling gate (no open shift)
  shiftGate: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: "rgba(241,245,249,0.92)",
    alignItems: "center",
    justifyContent: "center",
    gap: 14,
  },
  shiftGateText: { fontSize: 17, color: "#475569", fontWeight: "600" },
  shiftGateBtn: {
    backgroundColor: "#00B14F",
    paddingHorizontal: 40,
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: "center",
  },
  shiftGateBtnText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700", letterSpacing: 1 },
  gateModalOverlay: {
    flex: 1,
    backgroundColor: "rgba(15,23,42,0.45)",
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  gateModal: { width: "100%", maxWidth: 380, backgroundColor: "#FFFFFF", borderRadius: 16, overflow: "hidden" },
  gateModalHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  gateModalTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  gateModalLabel: { fontSize: 13, color: "#475569", fontWeight: "500" },
  gateModalInput: {
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    paddingHorizontal: 14, paddingVertical: 12, fontSize: 18, color: "#0F172A",
    ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}),
  },

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
    ...MARGIN_TOP_AUTO,
    padding: 14,
    backgroundColor: "#F1F5F9",
    borderRadius: 12,
    alignItems: "center",
  },
  drawerCloseText: { fontSize: 14, fontWeight: "600", color: "#475569" },

  // ---------- Easy Pay pane ----------
  easyPayPane: {
    flex: 1,
    backgroundColor: "#F0FDF4",
    borderRadius: 16,
    padding: 20,
    alignItems: "center",
    gap: 10,
  },
  easyPayThai: {
    fontSize: 22,
    fontWeight: "700",
    color: "#0F172A",
    textAlign: "center",
  },
  easyPayTitle: {
    fontSize: 30,
    fontWeight: "700",
    color: "#00B14F",
    textAlign: "center",
  },
  easyPayBadges: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    justifyContent: "center",
  },
  easyPayBadge: {
    backgroundColor: "#00B14F",
    paddingHorizontal: 12,
    paddingVertical: 5,
    borderRadius: 20,
  },
  easyPayBadgeText: { color: "#fff", fontSize: 12, fontWeight: "600" },
  easyPayCard: {
    flexDirection: "row",
    gap: 16,
    backgroundColor: "#fff",
    borderRadius: 14,
    padding: 16,
    alignItems: "center",
    width: "100%",
  },
  easyPayDevicePlaceholder: {
    width: 100,
    height: 80,
    alignItems: "center",
    justifyContent: "center",
  },
  easyPayBrandGrid: {
    flex: 1,
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
  },
  easyPayBrandPill: {
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 6,
    minWidth: 36,
    alignItems: "center",
  },
  easyPayBrandText: { color: "#fff", fontSize: 10, fontWeight: "700" },
  easyPayRegister: {
    backgroundColor: "#00B14F",
    paddingVertical: 14,
    paddingHorizontal: 40,
    borderRadius: 30,
    marginTop: 4,
    width: "70%",
    alignItems: "center",
  },
  easyPayRegisterText: { color: "#fff", fontSize: 16, fontWeight: "700" },

  // ---------- PromptPay pane ----------
  promptPayPane: {
    flex: 1,
    backgroundColor: "#fff",
    borderRadius: 16,
    overflow: "hidden",
    alignItems: "center",
    gap: 16,
    paddingBottom: 20,
  },
  thaiQrHeader: {
    backgroundColor: "#0F2A5E",
    width: "100%",
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    paddingVertical: 14,
    paddingHorizontal: 16,
  },
  thaiQrTitle: {
    color: "#fff",
    fontSize: 18,
    fontWeight: "700",
    letterSpacing: 1,
    textAlign: "center",
  },
  promptPayLogoBox: {
    marginTop: 8,
    alignItems: "center",
  },
  promptPayLogoPill: {
    borderWidth: 2,
    borderColor: "#00457C",
    borderRadius: 8,
    paddingHorizontal: 16,
    paddingVertical: 8,
  },
  promptPayLogoText: {
    fontSize: 16,
    fontWeight: "700",
    color: "#00457C",
    letterSpacing: 0.5,
  },
  promptPayInvalid: {
    fontSize: 16,
    fontWeight: "600",
    color: "#EF4444",
    textAlign: "center",
  },
  promptPayHint: {
    fontSize: 13,
    color: "#475569",
    textAlign: "center",
    lineHeight: 20,
  },
  printQrBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: 1,
    borderColor: "#CBD5E1",
    borderRadius: 8,
    paddingVertical: 10,
    paddingHorizontal: 20,
    ...MARGIN_TOP_AUTO,
  },
  printQrText: { fontSize: 14, color: "#475569", fontWeight: "600" },

  // ---------- QR Kbank pane ----------
  qrKbankPane: {
    flex: 1,
    backgroundColor: "#fff",
    borderRadius: 16,
    overflow: "hidden",
    alignItems: "center",
    gap: 12,
    paddingBottom: 20,
  },
  kbankBrandRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    justifyContent: "center",
    paddingHorizontal: 12,
  },
  kbankBrandPill: {
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 14,
  },
  kbankBrandText: { color: "#fff", fontSize: 12, fontWeight: "600" },
  kbankSupportText: {
    fontSize: 13,
    color: "#475569",
    textAlign: "center",
    paddingHorizontal: 12,
    lineHeight: 20,
  },
  kbankByRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  kbankByLabel: { fontSize: 13, color: "#475569" },
  kbankLogoPill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 10,
  },
  kbankLogoText: { color: "#fff", fontSize: 12, fontWeight: "700" },
  kbankIllustration: {
    width: 80,
    height: 80,
    alignItems: "center",
    justifyContent: "center",
  },
  kbankRegisterBtn: {
    backgroundColor: "#00B14F",
    width: "90%",
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: "center",
    ...MARGIN_TOP_AUTO,
  },
  kbankRegisterText: { color: "#fff", fontSize: 15, fontWeight: "700" },

  // ---------- EDC pane additions ----------
  edcHeroRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    paddingHorizontal: 8,
    marginBottom: 4,
  },
  edcDeviceGroup: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: 4,
  },
  edcArrowWrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    height: 40,
  },
  edcArrowLine: {
    position: "absolute",
    left: 0,
    right: 0,
    height: 2,
    backgroundColor: "#00B14F",
  },
  edcArrowLabel: {
    backgroundColor: "#fff",
    paddingHorizontal: 6,
    fontSize: 11,
    color: "#00B14F",
    fontWeight: "600",
  },
  edcByRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginTop: 8,
  },

  // ---------- Custom pane additions ----------
  customRefRow: {
    flexDirection: "row",
    alignItems: "center",
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
    paddingVertical: 10,
    paddingHorizontal: 2,
    gap: 8,
  },
  customRefLabel: {
    fontSize: 14,
    color: "#64748B",
    minWidth: 70,
  },
  customRefInput: {
    flex: 1,
    fontSize: 14,
    color: "#0F172A",
    textAlign: "right",
    outlineStyle: "none" as any,
  },

  // ---------- Beam pane ----------
  beamPane: {
    flex: 1,
    padding: 16,
    gap: 12,
    alignItems: "stretch",
  },
  beamHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingBottom: 12,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  beamLogoBox: {
    width: 44,
    height: 44,
    borderRadius: 10,
    backgroundColor: "#E5F7ED",
    alignItems: "center",
    justifyContent: "center",
  },
  beamTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  beamSub: { fontSize: 12, color: "#64748B", marginTop: 2 },
  beamIdleBox: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    paddingVertical: 24,
  },
  beamIdleText: { fontSize: 13, color: "#94A3B8", textAlign: "center" },
  beamQrBox: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
  },
  beamQrImage: { width: 200, height: 200 },
  beamWaiting: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  beamWaitingText: { fontSize: 13, color: "#475569" },
  beamAmount: {
    fontSize: 22,
    fontWeight: "700",
    color: "#0F172A",
    textAlign: "center",
    paddingTop: 8,
    borderTopWidth: 1,
    borderTopColor: "#E2E8F0",
  },
  beamRetryBtn: {
    backgroundColor: "#EF4444",
    paddingHorizontal: 24,
    paddingVertical: 10,
    borderRadius: 8,
    marginTop: 8,
  },
  beamRetryText: { color: "#fff", fontWeight: "700", fontSize: 14 },
  beamCancelBtn: {
    borderWidth: 1,
    borderColor: "#CBD5E1",
    paddingHorizontal: 20,
    paddingVertical: 8,
    borderRadius: 8,
    marginTop: 4,
  },
  beamCancelText: { color: "#64748B", fontSize: 13 },
});
