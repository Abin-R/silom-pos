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
  RefreshControl,
  ActivityIndicator,
  Platform,
  useWindowDimensions,
  Alert,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import { useRouter, useFocusEffect } from "expo-router";
import { Ionicons, MaterialCommunityIcons } from "@expo/vector-icons";
import PhoneInput from "../components/PhoneInput";
import { useStarPrinter } from "../lib/useStarPrinter";
import { useSelfOrderPrinting } from "../lib/useSelfOrderPrinting";
import { loadLocalPrinterConfig } from "../lib/localPrinterConfig";
import { SidebarDrawer } from "../components/SidebarDrawer";
import { apiFetch, clearAuthToken } from "../lib/api";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Sentry from "@sentry/react-native";
import qrcode from "qrcode-generator";
import { C } from "../lib/theme";

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
// Same cadence for polling the Omise payment-link charge status.
const OMISE_POLL_INTERVAL_MS = 3000;

// Render an arbitrary string (e.g. an Omise hosted-checkout URL) as a QR-code
// data URI that <Image> can display.  Uses error-correction level "M" and
// auto type number (0 = smallest that fits).
function makeQrDataUrl(text: string): string | null {
  try {
    const qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    return qr.createDataURL(6, 8);
  } catch {
    return null;
  }
}

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

  // Customers ordering on their own phones can't print their own slip — the
  // printer is here.  Poll for orders they've paid for and print them.  Reuses
  // the printReceipt above rather than mounting a second useStarPrinter, which
  // would give view-shot two hidden overlays to fight over.  Mounted only here,
  // never in admin.tsx: both screens can be mounted at once, and that would
  // double every receipt.  Waits for auth — the endpoint is session-scoped.
  // Gated on the branch's self_order_enabled flag inside the hook — a branch
  // that doesn't use self-ordering never polls and never prints.
  useSelfOrderPrinting(printReceipt, activeBranchName, authLoaded, activeBranchId);

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
  // The vertical category rail used to eat 112px of the grid; with categories
  // on top there is room for a fifth column on a full-size tablet.
  const gridCols = width >= 1100 ? 5 : isWide ? 4 : isMid ? 3 : 2;

  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("favorite");
  const [cart, setCart] = useState<CartItem[]>([]);
  const [editItem, setEditItem] = useState<CartItem | null>(null); // cart-item edit modal
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
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

  // Load initial data. `silent` skips the full-screen spinner (used by
  // pull-to-refresh, which shows its own inline spinner instead).
  const reloadPosData = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
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
      if (!silent) setLoading(false);
    }
    refreshBadges();
  }, []);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try { await reloadPosData(true); } finally { setRefreshing(false); }
  }, [reloadPosData]);

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
    meta?: { beamChargeId?: string; beamLinkId?: string; omiseLinkId?: string; omiseChargeId?: string }
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
          // ``total`` is the goods total; the backend adds VAT bookkeeping and,
          // for card payments, the processing fee + fee VAT to the grand total.
          total,
          payment_method: method,
          paid_amount: paid,
          change: Math.max(0, paid - total),
          source: "table",
          staff: staff || "",
          customer_id: customer?.id,
          customer_name: customer?.name,
          beam_charge_id: meta?.beamChargeId || null,
          beam_link_id: meta?.beamLinkId || null,
          omise_link_id: meta?.omiseLinkId || null,
          omise_charge_id: meta?.omiseChargeId || null,
        }),
      });
      if (!res.ok) {
        // Reached only after the customer has paid, so a failed save means
        // money taken with no order recorded.  Throwing routes it to the catch
        // below, which tells both Sentry and the cashier — previously res.json()
        // ran regardless and the success modal showed an undefined order number.
        throw new Error(`Order save failed (HTTP ${res.status})`);
      }
      const order = await res.json();
      // The server is authoritative for the grand total (it adds the card fee
      // + VAT), so display its numbers rather than the local goods total.
      const grandTotal = Number(order.total) || total;
      const paidShown = Number(order.paid_amount) || paid;
      setShowPayment(false);
      setShowSuccess({
        order_number: order.order_number,
        total: grandTotal,
        paid: paidShown,
        change: Math.max(0, paidShown - grandTotal),
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
              // Server-assigned, per branch per day.  Previously the receipt
              // derived this itself from the last two digits of the *global*
              // order number, which collided across branches and wrapped every
              // 100 sales — and it's the number the customer gets called by.
              queue_number: order.queue_number ?? undefined,
              items: cart.map((c) => ({ name: c.name, qty: c.qty, price: c.price })),
              subtotal,
              discount_amount: discountAmount,
              vat_amount: Number(order.vat_amount) || 0,
              processing_fee: Number(order.processing_fee) || 0,
              processing_fee_vat: Number(order.processing_fee_vat) || 0,
              total: grandTotal,
              payment_method: method,
              paid_amount: paidShown,
              change: Math.max(0, paidShown - grandTotal),
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
      // The sale already happened — cash is in the drawer or the card/QR charge
      // is captured — but the order never made it to the backend.  console.error
      // is invisible on a tablet, so this used to lose the order silently.
      Sentry.captureException(e, {
        level: "fatal",
        tags: { flow: "checkout", payment_method: method },
        extra: {
          paid,
          goods_total: total,
          item_count: cart.length,
          branch: activeBranchName,
          staff: staff || "",
          ...meta,
        },
      });
      // Keep the cart intact: the cashier can retry the save once the network
      // is back, and clearing it would destroy the only record of the sale.
      Alert.alert(
        "Order NOT saved",
        "The payment went through but the order could not be saved to the server.\n\n" +
          "Do NOT take payment again. Write this order down, then tell an admin.",
      );
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
        <ActivityIndicator color={C.brand} size="large" />
      </View>
    );
  }

  if (!authLoaded) {
    // Don't render with empty staff/role/branch — flashes the wrong state and
    // the data fetches below would have nothing meaningful to display anyway.
    return (
      <SafeAreaView style={styles.root} edges={["top", "bottom"]}>
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
          <ActivityIndicator color={C.brand} />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.root} edges={["bottom"]}>
      <StatusBar style="dark" />
      {/* ============ TOP BAR ============ */}
      <View style={[styles.topBar, { height: 60 + insets.top, paddingTop: insets.top }]} testID="top-bar">
        <TouchableOpacity
          style={styles.menuBtn}
          onPress={() => setSidebarOpen(true)}
          testID="menu-btn"
        >
          <Ionicons name="menu" size={24} color={C.ink} />
        </TouchableOpacity>

        <View style={styles.topBrand}>
          <Image
            source={require("../assets/images/icon.png")}
            style={styles.topBrandLogo}
            resizeMode="cover"
          />
          {isWide && <Text style={styles.topBrandName}>The Rolling Pinn</Text>}
        </View>

        {isMid && (
          <View style={styles.searchWrap}>
            <Ionicons name="search" size={18} color={C.ink3} />
            <TextInput
              placeholder="Search Products"
              placeholderTextColor={C.ink3}
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
          label="Orders"
          badge={orderHubCount}
          onPress={() => setShowOrderHub(true)}
          testId="toolbar-order-hub"
          compact={!isWide}
        />
        <ToolbarIcon
          icon="albums-outline"
          label="Cash"
          onPress={() => setShowDrawer(true)}
          testId="toolbar-drawer"
          compact={!isWide}
        />
        <ToolbarIcon
          icon="bookmark-outline"
          label="Hold"
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
            <Ionicons name="storefront-outline" size={16} color={C.brand} />
            <Text style={styles.branchChipText} numberOfLines={1}>{activeBranchName}</Text>
          </View>
        )}
        {isWide && (
          <View style={styles.staffChip}>
            <Ionicons name="person-circle" size={22} color={C.brand} />
            <Text style={styles.staffText}>{staff || "Admin"}</Text>
          </View>
        )}
        <TouchableOpacity
          style={styles.logoutBtn}
          onPress={async () => { await doLogout(); router.replace("/"); }}
          testID="logout-btn"
        >
          <Ionicons name="log-out-outline" size={20} color={C.danger} />
        </TouchableOpacity>
      </View>

      {/* Mobile search bar (below top bar on narrow) + branch chip */}
      {!isMid && (
        <View style={styles.mobileTopRow}>
          <View style={[styles.mobileSearchWrap, { flex: 1, marginRight: 0 }]}>
            <Ionicons name="search" size={18} color={C.ink3} />
            <TextInput
              placeholder="Search Products"
              placeholderTextColor={C.ink3}
              style={styles.searchInput}
              value={search}
              onChangeText={setSearch}
              testID="product-search-mobile"
            />
          </View>
          {!!activeBranchName && (
            <View style={styles.branchChipMobile} testID="branch-chip">
              <Ionicons name="storefront-outline" size={14} color={C.brand} />
              <Text style={styles.branchChipMobileText} numberOfLines={1}>{activeBranchName}</Text>
            </View>
          )}
        </View>
      )}

      {/* ============ MAIN LAYOUT ============ */}
      <View style={{ flex: 1 }}>
      <View style={[styles.main, !isWide && styles.mainStacked]}>
        {/* Order panel — pinned to the left edge on tablet, so the running
            order is the first thing in reading order rather than an
            afterthought parked on the right. On phone it collapses into the
            bottom sheet behind the cart button. */}
        {isWide && (
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
        )}

        {/* Products — categories run across the top at every width, grid below. */}
        <View style={styles.center}>
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            style={[styles.catStrip, !isMid && styles.catStripStacked]}
            contentContainerStyle={{ paddingHorizontal: 12, gap: 8, alignItems: "center" }}
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
                  sub={isWide ? c.name_th : undefined}
                  active={activeCat === c.id}
                  onPress={() => {
                    setActiveCat(c.id);
                    setSearch("");
                  }}
                  testId={`cat-${c.id}`}
                />
              ))}
          </ScrollView>

          <FlatList
            key={`grid-${gridCols}`}
            data={filteredProducts}
            keyExtractor={(i) => i.id}
            numColumns={gridCols}
            contentContainerStyle={{ padding: 12, paddingBottom: 120 }}
            columnWrapperStyle={{ gap: 10 }}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={onRefresh} colors={[C.brand]} tintColor={C.brand} />
            }
            ItemSeparatorComponent={() => <View style={{ height: 10 }} />}
            ListEmptyComponent={
              <View style={styles.empty}>
                <Ionicons name="search-outline" size={40} color={C.lineStrong} />
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
                {/* Photo-first card: the picture is the card, not a thumbnail
                    stuck above a caption. Name sits on a scrim so it stays
                    legible over any photo; price rides a brand badge. */}
                <Image source={{ uri: item.image_base64 || item.image_url }} style={styles.productImg} />
                {!item.image_base64 && !item.image_url && (
                  <View style={styles.productNoImg}>
                    <Ionicons name="cafe-outline" size={26} color="rgba(255,255,255,0.5)" />
                  </View>
                )}
                <View style={styles.priceTag}>
                  <Text style={styles.priceTagText}>{THB(item.price)}</Text>
                </View>
                <View style={styles.productFooter}>
                  <Text style={styles.productName} numberOfLines={2}>
                    {item.name}
                  </Text>
                </View>
              </TouchableOpacity>
            )}
          />
        </View>

        {/* Phone: the order rides in a bottom sheet behind this button. */}
        {!isWide && cart.length > 0 && (
          <TouchableOpacity
            style={styles.fabCart}
            onPress={() => setShowCart(true)}
            testID="fab-cart"
          >
            <View style={styles.fabLeft}>
              <MaterialCommunityIcons name="cart" size={22} color={C.surface} />
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
              <Ionicons name="chevron-up" size={18} color={C.surface} />
            </View>
          </TouchableOpacity>
        )}
      </View>

        {/* Selling gate — blocks the grid/cart until a shift is open. The top
            bar (and its sidebar → admin/reports) stays reachable above this. */}
        {shiftOpen === false && (
          <View style={styles.shiftGate} testID="shift-gate">
            <Ionicons name="lock-closed-outline" size={44} color={C.lineStrong} />
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
              <TouchableOpacity onPress={() => setShowOpenShift(false)}><Ionicons name="close" size={24} color={C.ink2} /></TouchableOpacity>
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
                <Ionicons name="close" size={24} color={C.ink2} />
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
          // Full logout: backend session + in-memory token + AsyncStorage.
          // Just clearing the in-memory token leaves AUTH_KEY on disk, so
          // index.tsx's /auth/me check succeeds and bounces back to /pos.
          await doLogout();
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
        <Ionicons name={icon} size={compact ? 20 : 22} color={C.ink} />
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
      style={[styles.catChip, active && styles.catChipActive]}
      onPress={onPress}
      testID={testId}
    >
      <Text style={[styles.catChipText, active && styles.catChipTextActive]} numberOfLines={1}>
        {label}
      </Text>
      {!!sub && (
        <Text style={[styles.catChipSub, active && styles.catChipSubActive]} numberOfLines={1}>
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
    <View style={[styles.orderPanel, embedded && styles.orderPanelEmbedded]} testID="cart-sidebar">
      <View style={styles.cartHeader}>
        {customer && (
          <View style={styles.custChip}>
            <View style={[styles.custDot, { backgroundColor: customer.color }]}>
              <Text style={styles.custInitial}>
                {customer.name?.[0]?.toUpperCase()}
              </Text>
            </View>
            <Text style={styles.custName} numberOfLines={1}>
              {customer.name}
            </Text>
            <TouchableOpacity onPress={onRemoveCustomer} testID="remove-customer">
              <Ionicons name="close" size={16} color={C.ink3} />
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

      {cart.length > 0 && (
        <View style={styles.cartListHeader}>
          <Text style={styles.cartListCount}>
            {`${cart.length} Item${cart.length !== 1 ? "s" : ""} / ${cartCount} pcs.`}
          </Text>
        </View>
      )}

      <FlatList
        data={cart}
        keyExtractor={(i) => i.product_id}
        style={{ flex: 1 }}
        contentContainerStyle={cart.length === 0 ? { flex: 1 } : undefined}
        ListEmptyComponent={
          <View style={styles.emptyCart}>
            <MaterialCommunityIcons name="cart-outline" size={40} color={C.lineStrong} />
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
                <Ionicons name="remove" size={16} color={C.ink} />
              </TouchableOpacity>
              <Text style={styles.qtyText}>{item.qty}</Text>
              <TouchableOpacity
                style={styles.qtyBtn}
                onPress={() => onInc(item.product_id)}
                testID={`qty-inc-${item.product_id}`}
              >
                <Ionicons name="add" size={16} color={C.ink} />
              </TouchableOpacity>
            </View>
            <TouchableOpacity
              onPress={() => onRemove(item.product_id)}
              style={styles.trashBtn}
              testID={`remove-${item.product_id}`}
            >
              <Ionicons name="trash-outline" size={18} color={C.danger} />
            </TouchableOpacity>
          </View>
        )}
      />

      {cart.length > 0 && (
        <View style={styles.cartFooterRow}>
          <TouchableOpacity onPress={onClear} style={styles.footerTrash} testID="clear-cart">
            <Ionicons name="trash-outline" size={18} color={C.danger} />
          </TouchableOpacity>
        </View>
      )}
    </View>
  );
}

// ---------- Payment Method Constants ----------
const PAYMENT_METHODS = {
  CASH: "Cash",
  BEAM: "Beam",
  CARD_LINK: "Credit Card",
  BEAM_CARD: "Beam Card",
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
  onPay: (
    method: string,
    paid: number,
    meta?: {
      beamChargeId?: string;
      beamLinkId?: string;
      omiseLinkId?: string;
      omiseChargeId?: string;
    },
  ) => void;
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
    { key: PAYMENT_METHODS.CARD_LINK, icon: "card-outline" as const },
    { key: PAYMENT_METHODS.BEAM_CARD, icon: "card-outline" as const },
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

  // Omise credit-card payment-link state (mirrors the Beam flow: generate a
  // hosted-checkout link, render its URL as a QR, poll until the card charge
  // succeeds).  ``omiseFinal`` is the grand total the customer is charged
  // (goods + processing fee + fee VAT) so the success screen is accurate.
  const [omiseLinkId, setOmiseLinkId] = useState<string | null>(null);
  const [omiseQrImage, setOmiseQrImage] = useState<string | null>(null);
  const [omiseStatus, setOmiseStatus] = useState<"idle" | "loading" | "pending" | "completed" | "failed">("idle");
  const [omiseError, setOmiseError] = useState<string | null>(null);
  const [omiseBreakdown, setOmiseBreakdown] = useState<{
    goods: number; vat: number; fee: number; feeVat: number; total: number;
  } | null>(null);

  // Beam credit-card payment-link state (same flow as Omise, but via Beam's
  // payment-link API and the Beam card surcharge).
  const [beamCardLinkId, setBeamCardLinkId] = useState<string | null>(null);
  const [beamCardQrImage, setBeamCardQrImage] = useState<string | null>(null);
  const [beamCardStatus, setBeamCardStatus] = useState<"idle" | "loading" | "pending" | "completed" | "failed">("idle");
  const [beamCardError, setBeamCardError] = useState<string | null>(null);
  const [beamCardBreakdown, setBeamCardBreakdown] = useState<{
    goods: number; vat: number; fee: number; feeVat: number; total: number;
  } | null>(null);

  // Reset Beam state to idle (used by modal-open cleanup, Cancel, and Retry).
  const resetBeam = useCallback(() => {
    setBeamStatus("idle");
    setBeamChargeId(null);
    setBeamQrImage(null);
    setBeamError(null);
  }, []);

  const resetOmise = useCallback(() => {
    setOmiseStatus("idle");
    setOmiseLinkId(null);
    setOmiseQrImage(null);
    setOmiseError(null);
    setOmiseBreakdown(null);
  }, []);

  const resetBeamCard = useCallback(() => {
    setBeamCardStatus("idle");
    setBeamCardLinkId(null);
    setBeamCardQrImage(null);
    setBeamCardError(null);
    setBeamCardBreakdown(null);
  }, []);

  useEffect(() => {
    if (visible) {
      setCustomPick(""); setOrderRef("");
      setCardLast4(""); setCardType(""); setBankPick("");
      resetBeam();
      resetOmise();
      resetBeamCard();
    }
  }, [visible, resetBeam, resetOmise, resetBeamCard]);

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
      setBeamStatus(data.status === "SUCCEEDED" || data.status === "COMPLETED" ? "completed" : "pending");
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
        // Beam reports a paid charge as "SUCCEEDED" (the charge.succeeded webhook /
        // GET /charges/{id} status). Accept "COMPLETED" too for forward-compat.
        if (data.status === "SUCCEEDED" || data.status === "COMPLETED") {
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

  // Create an Omise payment Link for a credit-card charge.  The backend adds
  // the 3.65% processing fee + 7% VAT on that fee on top of the goods total
  // and returns the hosted-checkout URL we render as a QR.
  const startOmiseLink = async (referenceId: string) => {
    setOmiseStatus("loading");
    setOmiseError(null);
    try {
      const res = await apiFetch(`${API}/omise/link`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: total, title: `Order ${referenceId}`, description: `Order ${referenceId}` }),
      });
      const data = await res.json();
      if (!res.ok) {
        setOmiseStatus("failed");
        setOmiseError(data.detail || "Failed to create payment link");
        return;
      }
      setOmiseLinkId(data.link_id);
      setOmiseQrImage(makeQrDataUrl(data.payment_uri));
      setOmiseBreakdown({
        goods: Number(data.goods_total) || total,
        vat: Number(data.vat_amount) || 0,
        fee: Number(data.processing_fee) || 0,
        feeVat: Number(data.processing_fee_vat) || 0,
        total: Number(data.amount_total) || total,
      });
      setOmiseStatus("pending");
    } catch {
      setOmiseStatus("failed");
      setOmiseError("Cannot reach payment server");
    }
  };

  // Poll the Omise link until a card charge succeeds (or fails).  On success
  // the grand total (goods + fee + fee VAT) is what was charged.
  const omiseBreakdownRef = useRef(omiseBreakdown);
  useEffect(() => { omiseBreakdownRef.current = omiseBreakdown; }, [omiseBreakdown]);
  useEffect(() => {
    if (omiseStatus !== "pending" || !omiseLinkId) return;
    const interval = setInterval(async () => {
      try {
        const res = await apiFetch(`${API}/omise/link/${omiseLinkId}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.status === "successful") {
          setOmiseStatus("completed");
          clearInterval(interval);
          const charged = omiseBreakdownRef.current?.total ?? totalRef.current;
          onPayRef.current(PAYMENT_METHODS.CARD_LINK, charged, {
            omiseLinkId,
            omiseChargeId: data.charge_id,
          });
        } else if (data.status === "failed") {
          setOmiseStatus("failed");
          setOmiseError("Card payment failed. Please try again.");
          clearInterval(interval);
        }
      } catch { /* ignore transient errors */ }
    }, OMISE_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [omiseStatus, omiseLinkId]);

  // Create a Beam card payment Link.  Same shape as the Omise flow: the backend
  // adds the Beam card fee + 7% VAT on top of the goods total and returns a
  // hosted-checkout URL we render as a QR.
  const startBeamCardLink = async (referenceId: string) => {
    setBeamCardStatus("loading");
    setBeamCardError(null);
    try {
      const res = await apiFetch(`${API}/beam/payment-link`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: total, reference_id: referenceId, description: `Order ${referenceId}` }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBeamCardStatus("failed");
        setBeamCardError(data.detail || "Failed to create payment link");
        return;
      }
      setBeamCardLinkId(data.link_id);
      setBeamCardQrImage(makeQrDataUrl(data.payment_uri));
      setBeamCardBreakdown({
        goods: Number(data.goods_total) || total,
        vat: Number(data.vat_amount) || 0,
        fee: Number(data.processing_fee) || 0,
        feeVat: Number(data.processing_fee_vat) || 0,
        total: Number(data.amount_total) || total,
      });
      setBeamCardStatus("pending");
    } catch {
      setBeamCardStatus("failed");
      setBeamCardError("Cannot reach payment server");
    }
  };

  // Poll the Beam payment link until the card charge succeeds (or fails).
  const beamCardBreakdownRef = useRef(beamCardBreakdown);
  useEffect(() => { beamCardBreakdownRef.current = beamCardBreakdown; }, [beamCardBreakdown]);
  useEffect(() => {
    if (beamCardStatus !== "pending" || !beamCardLinkId) return;
    const interval = setInterval(async () => {
      try {
        const res = await apiFetch(`${API}/beam/payment-link/${beamCardLinkId}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.status === "successful") {
          setBeamCardStatus("completed");
          clearInterval(interval);
          const charged = beamCardBreakdownRef.current?.total ?? totalRef.current;
          onPayRef.current(PAYMENT_METHODS.BEAM_CARD, charged, {
            beamLinkId: beamCardLinkId,
            beamChargeId: data.charge_id,
          });
        } else if (data.status === "failed") {
          setBeamCardStatus("failed");
          setBeamCardError("Card payment failed. Please try again.");
          clearInterval(interval);
        }
      } catch { /* ignore transient errors */ }
    }, BEAM_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [beamCardStatus, beamCardLinkId]);

  // Derived flags for the right-panel "Payment Confirm" button — extracted so
  // the JSX below stays readable.
  const isQrLikeMethod = method === PAYMENT_METHODS.QR_KBANK || method === PAYMENT_METHODS.PROMPTPAY || method === PAYMENT_METHODS.BEAM;
  const isCustomReady = method === PAYMENT_METHODS.CUSTOM && !!customPick;
  const isCreditReady = method === PAYMENT_METHODS.CREDIT && !!(cardType || bankPick);
  const isBeamBusy =
    method === PAYMENT_METHODS.BEAM &&
    (beamStatus === "loading" || beamStatus === "pending" || beamStatus === "completed");
  const isOmiseBusy =
    method === PAYMENT_METHODS.CARD_LINK &&
    (omiseStatus === "loading" || omiseStatus === "pending" || omiseStatus === "completed");
  const isBeamCardBusy =
    method === PAYMENT_METHODS.BEAM_CARD &&
    (beamCardStatus === "loading" || beamCardStatus === "pending" || beamCardStatus === "completed");
  const isCardLink = method === PAYMENT_METHODS.CARD_LINK;
  const isBeamCard = method === PAYMENT_METHODS.BEAM_CARD;
  const canConfirm =
    (isQrLikeMethod || isCustomReady || isCreditReady || isCardLink || isBeamCard || canPay) &&
    !isBeamBusy && !isOmiseBusy && !isBeamCardBusy;

  const confirmLabel = (() => {
    if (method === PAYMENT_METHODS.BEAM) {
      if (beamStatus === "loading") return "Generating…";
      if (beamStatus === "pending") return "Waiting for scan…";
      return "Generate QR";
    }
    if (method === PAYMENT_METHODS.CARD_LINK) {
      if (omiseStatus === "loading") return "Generating…";
      if (omiseStatus === "pending") return "Waiting for payment…";
      return "Generate Card QR";
    }
    if (method === PAYMENT_METHODS.BEAM_CARD) {
      if (beamCardStatus === "loading") return "Generating…";
      if (beamCardStatus === "pending") return "Waiting for payment…";
      return "Generate Card QR";
    }
    return "Payment Confirm";
  })();

  const handleConfirmPayment = () => {
    if (method === PAYMENT_METHODS.BEAM) {
      // Generate QR — use a temp reference ID; actual order is created when polling confirms
      const ref = `POS-${Date.now()}`;
      startBeamCharge(ref);
      return;
    }
    if (method === PAYMENT_METHODS.CARD_LINK) {
      // Create the Omise hosted-checkout link; the order is created when the
      // polling effect confirms the card charge succeeded.
      const ref = `POS-${Date.now()}`;
      startOmiseLink(ref);
      return;
    }
    if (method === PAYMENT_METHODS.BEAM_CARD) {
      // Create the Beam card payment link; the order is created when the
      // polling effect confirms the card charge succeeded.
      const ref = `POS-${Date.now()}`;
      startBeamCardLink(ref);
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
              <Ionicons name="close" size={26} color={C.ink2} />
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
                    color={method === m.key ? C.brand : C.ink2}
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
                    <Ionicons name="tablet-landscape" size={80} color={C.brand} />
                    <Ionicons name="qr-code" size={32} color={C.ink} style={{ position: "absolute", bottom: 4, right: 4 }} />
                  </View>
                  <View style={styles.easyPayBrandGrid}>
                    {[
                      { name: "VISA", color: "#1A1F71" },
                      { name: "MC", color: "#EB001B" },
                      { name: "UP", color: "#E21836" },
                      { name: "JCB", color: "#0E4C96" },
                      { name: "LINE", color: "#06C755" },
                      { name: "True", color: C.danger },
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
                  <Ionicons name="grid" size={28} color={C.surface} />
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
                  <Ionicons name="print-outline" size={16} color={C.ink2} />
                  <Text style={styles.printQrText}>Print QR Code</Text>
                </TouchableOpacity>
              </View>
            ) : method === PAYMENT_METHODS.QR_KBANK ? (
              <View style={styles.qrKbankPane} testID="qrkbank-pane">
                <View style={styles.thaiQrHeader}>
                  <Ionicons name="grid" size={28} color={C.surface} />
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
                  <Ionicons name="phone-portrait-outline" size={56} color={C.ink2} />
                  <Ionicons name="qr-code" size={32} color={C.ink} style={{ position: "absolute", bottom: 0, right: 0 }} />
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
                    <Ionicons name="scan-outline" size={28} color={C.brand} />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.beamTitle}>Beam QR Payment</Text>
                    <Text style={styles.beamSub}>PromptPay · e-Wallet · All banks</Text>
                  </View>
                </View>

                {beamStatus === "idle" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="qr-code-outline" size={72} color={C.lineStrong} />
                    <Text style={styles.beamIdleText}>{'Tap "Generate QR" to create a QR code'}</Text>
                  </View>
                )}

                {beamStatus === "loading" && (
                  <View style={styles.beamIdleBox}>
                    <ActivityIndicator size="large" color={C.brand} />
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
                      <ActivityIndicator size="small" color={C.brand} />
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
                    <Ionicons name="alert-circle-outline" size={48} color={C.danger} />
                    <Text style={[styles.beamIdleText, { color: C.danger }]}>{beamError || "Payment failed"}</Text>
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
            ) : method === PAYMENT_METHODS.CARD_LINK ? (
              <View style={styles.beamPane} testID="cardlink-pane">
                {/* Header */}
                <View style={styles.beamHeader}>
                  <View style={styles.beamLogoBox}>
                    <Ionicons name="card-outline" size={28} color={C.brand} />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.beamTitle}>Credit Card</Text>
                    <Text style={styles.beamSub}>VISA · Mastercard · JCB · scan to pay by card</Text>
                  </View>
                </View>

                {omiseStatus === "idle" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="qr-code-outline" size={72} color={C.lineStrong} />
                    <Text style={styles.beamIdleText}>
                      {'Tap "Generate Card QR" — the customer scans it to pay by card'}
                    </Text>
                  </View>
                )}

                {omiseStatus === "loading" && (
                  <View style={styles.beamIdleBox}>
                    <ActivityIndicator size="large" color={C.brand} />
                    <Text style={styles.beamIdleText}>Generating payment link…</Text>
                  </View>
                )}

                {omiseStatus === "pending" && omiseQrImage && (
                  <View style={styles.beamQrBox}>
                    <Image
                      source={{ uri: omiseQrImage }}
                      style={styles.beamQrImage}
                      resizeMode="contain"
                    />
                    <View style={styles.beamWaiting}>
                      <ActivityIndicator size="small" color={C.brand} />
                      <Text style={styles.beamWaitingText}>Waiting for card payment…</Text>
                    </View>
                    <TouchableOpacity
                      style={styles.beamCancelBtn}
                      onPress={resetOmise}
                      testID="cardlink-cancel"
                    >
                      <Text style={styles.beamCancelText}>Cancel</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {omiseStatus === "failed" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="alert-circle-outline" size={48} color={C.danger} />
                    <Text style={[styles.beamIdleText, { color: C.danger }]}>{omiseError || "Payment failed"}</Text>
                    <TouchableOpacity style={styles.beamRetryBtn} onPress={resetOmise}>
                      <Text style={styles.beamRetryText}>Try Again</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {/* Fee + VAT breakdown so the cashier can explain the surcharge */}
                {omiseBreakdown && (
                  <View style={styles.feeBreakdown}>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabel}>Goods (VAT incl.)</Text>
                      <Text style={styles.feeVal}>{THB(omiseBreakdown.goods)}</Text>
                    </View>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabelMuted}>VAT 7% (incl.)</Text>
                      <Text style={styles.feeValMuted}>{THB(omiseBreakdown.vat)}</Text>
                    </View>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabel}>Processing fee 3.65%</Text>
                      <Text style={styles.feeVal}>{THB(omiseBreakdown.fee)}</Text>
                    </View>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabelMuted}>VAT 7% on fee</Text>
                      <Text style={styles.feeValMuted}>{THB(omiseBreakdown.feeVat)}</Text>
                    </View>
                    <View style={[styles.feeRow, styles.feeRowTotal]}>
                      <Text style={styles.feeTotalLabel}>Total charged</Text>
                      <Text style={styles.feeTotalVal}>{THB(omiseBreakdown.total)}</Text>
                    </View>
                  </View>
                )}

                {!omiseBreakdown && <Text style={styles.beamAmount}>{THB(total)}</Text>}
              </View>
            ) : method === PAYMENT_METHODS.BEAM_CARD ? (
              <View style={styles.beamPane} testID="beamcard-pane">
                {/* Header */}
                <View style={styles.beamHeader}>
                  <View style={styles.beamLogoBox}>
                    <Ionicons name="card-outline" size={28} color={C.brand} />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.beamTitle}>Beam Card</Text>
                    <Text style={styles.beamSub}>VISA · Mastercard · JCB · scan to pay by card</Text>
                  </View>
                </View>

                {beamCardStatus === "idle" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="qr-code-outline" size={72} color={C.lineStrong} />
                    <Text style={styles.beamIdleText}>
                      {'Tap "Generate Card QR" — the customer scans it to pay by card'}
                    </Text>
                  </View>
                )}

                {beamCardStatus === "loading" && (
                  <View style={styles.beamIdleBox}>
                    <ActivityIndicator size="large" color={C.brand} />
                    <Text style={styles.beamIdleText}>Generating payment link…</Text>
                  </View>
                )}

                {beamCardStatus === "pending" && beamCardQrImage && (
                  <View style={styles.beamQrBox}>
                    <Image
                      source={{ uri: beamCardQrImage }}
                      style={styles.beamQrImage}
                      resizeMode="contain"
                    />
                    <View style={styles.beamWaiting}>
                      <ActivityIndicator size="small" color={C.brand} />
                      <Text style={styles.beamWaitingText}>Waiting for card payment…</Text>
                    </View>
                    <TouchableOpacity
                      style={styles.beamCancelBtn}
                      onPress={resetBeamCard}
                      testID="beamcard-cancel"
                    >
                      <Text style={styles.beamCancelText}>Cancel</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {beamCardStatus === "failed" && (
                  <View style={styles.beamIdleBox}>
                    <Ionicons name="alert-circle-outline" size={48} color={C.danger} />
                    <Text style={[styles.beamIdleText, { color: C.danger }]}>{beamCardError || "Payment failed"}</Text>
                    <TouchableOpacity style={styles.beamRetryBtn} onPress={resetBeamCard}>
                      <Text style={styles.beamRetryText}>Try Again</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {/* Fee + VAT breakdown so the cashier can explain the surcharge */}
                {beamCardBreakdown && (
                  <View style={styles.feeBreakdown}>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabel}>Goods (VAT incl.)</Text>
                      <Text style={styles.feeVal}>{THB(beamCardBreakdown.goods)}</Text>
                    </View>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabelMuted}>VAT 7% (incl.)</Text>
                      <Text style={styles.feeValMuted}>{THB(beamCardBreakdown.vat)}</Text>
                    </View>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabel}>Processing fee</Text>
                      <Text style={styles.feeVal}>{THB(beamCardBreakdown.fee)}</Text>
                    </View>
                    <View style={styles.feeRow}>
                      <Text style={styles.feeLabelMuted}>VAT 7% on fee</Text>
                      <Text style={styles.feeValMuted}>{THB(beamCardBreakdown.feeVat)}</Text>
                    </View>
                    <View style={[styles.feeRow, styles.feeRowTotal]}>
                      <Text style={styles.feeTotalLabel}>Total charged</Text>
                      <Text style={styles.feeTotalVal}>{THB(beamCardBreakdown.total)}</Text>
                    </View>
                  </View>
                )}

                {!beamCardBreakdown && <Text style={styles.beamAmount}>{THB(total)}</Text>}
              </View>
            ) : method === PAYMENT_METHODS.EDC ? (
              <View style={styles.edcPane} testID="edc-pane">
                <View style={styles.edcHeroRow}>
                  <View style={styles.edcDeviceGroup}>
                    <Ionicons name="tablet-landscape" size={64} color={C.ink} />
                    <Ionicons name="phone-portrait-outline" size={40} color={C.ink} />
                  </View>
                  <View style={styles.edcArrowWrap}>
                    <View style={styles.edcArrowLine} />
                    <Text style={styles.edcArrowLabel}>Send data</Text>
                  </View>
                  <Ionicons name="print" size={64} color={C.brand} />
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
                <Ionicons name="phone-portrait-outline" size={32} color={C.ink2} style={{ marginTop: 4 }} />
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
                    placeholderTextColor={C.ink3}
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
                  placeholderTextColor={C.ink3}
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
                    { k: "BAY", th: "กรุงศรีอยุธยา", color: C.warn },
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
                        <Ionicons name="backspace-outline" size={22} color={C.danger} />
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
                    <Ionicons name="person-outline" size={14} color={C.ink2} />
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
  const [discMode, setDiscMode] = useState<"thb" | "pct">("thb");

  useEffect(() => {
    if (item) {
      setQty(item.qty);
      setDisc(item.discount ? String(item.discount) : "");
      setDiscMode("thb");
    }
  }, [item]);

  if (!item) return null;

  const gross = item.price * qty;
  const entered = Math.max(0, parseFloat(disc) || 0);
  // Resolve the entered value into an absolute ฿ amount (the cart stores ฿).
  const discAmount =
    discMode === "pct" ? Math.min(gross, (gross * entered) / 100) : Math.min(gross, entered);
  const discPct = gross > 0 ? (discAmount / gross) * 100 : 0;
  const lineTotal = Math.max(0, gross - discAmount);

  return (
    <Modal visible transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View style={styles.itemModal} testID="cart-item-modal">
          <View style={styles.modalHeader}>
            <TouchableOpacity onPress={onClose} testID="close-item-modal">
              <Ionicons name="chevron-back" size={26} color={C.brand} />
            </TouchableOpacity>
            <Text style={styles.modalTitle} numberOfLines={1}>
              {item.name}
            </Text>
            <TouchableOpacity onPress={() => onRemove(item.product_id)} testID="item-modal-remove">
              <Ionicons name="trash-outline" size={22} color={C.danger} />
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
                <Ionicons name="remove" size={20} color={C.ink} />
              </TouchableOpacity>
              <Text style={styles.itemStepVal}>{qty}</Text>
              <TouchableOpacity
                style={styles.itemStepBtn}
                onPress={() => setQty((q) => q + 1)}
                testID="item-qty-inc"
              >
                <Ionicons name="add" size={20} color={C.ink} />
              </TouchableOpacity>
            </View>
          </View>

          <View style={styles.itemRow}>
            <Text style={styles.itemRowLabel}>Discount</Text>
            <View style={styles.itemDiscControls}>
              <View style={styles.discModeToggle}>
                <TouchableOpacity
                  style={[styles.discModeBtn, discMode === "thb" && styles.discModeBtnActive]}
                  onPress={() => setDiscMode("thb")}
                  testID="item-discount-mode-thb"
                >
                  <Text
                    style={[styles.discModeText, discMode === "thb" && styles.discModeTextActive]}
                  >
                    ฿
                  </Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={[styles.discModeBtn, discMode === "pct" && styles.discModeBtnActive]}
                  onPress={() => setDiscMode("pct")}
                  testID="item-discount-mode-pct"
                >
                  <Text
                    style={[styles.discModeText, discMode === "pct" && styles.discModeTextActive]}
                  >
                    %
                  </Text>
                </TouchableOpacity>
              </View>
              <TextInput
                style={styles.itemDiscInput}
                value={disc}
                onChangeText={(t) => setDisc(t.replace(/[^0-9.]/g, ""))}
                keyboardType="decimal-pad"
                placeholder={discMode === "pct" ? "0%" : "0.00"}
                placeholderTextColor={C.lineStrong}
                testID="item-discount-input"
              />
            </View>
          </View>

          {discAmount > 0 && (
            <View style={styles.itemDiscSummaryRow}>
              <Text style={styles.itemDiscSummaryText}>
                −{THB(discAmount)} ({discPct.toFixed(discPct % 1 === 0 ? 0 : 1)}%)
              </Text>
            </View>
          )}

          <View style={styles.itemTotalRow}>
            <Text style={styles.itemRowLabel}>Line Total</Text>
            <Text style={styles.itemTotalVal}>{THB(lineTotal)}</Text>
          </View>

          <TouchableOpacity
            style={styles.doneBtn}
            onPress={() => onSave(item.product_id, qty, discAmount)}
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
    let c: Customer | null = null;
    try {
      const res = await apiFetch(`${API}/customers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), phone: phone.trim() || null }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        throw new Error(detail || `Server error (${res.status})`);
      }
      c = await res.json();
    } catch (e: any) {
      Alert.alert("Couldn't save customer", e?.message || "Please try again.");
      return;
    }
    // Guard against a success response missing the required field — never select
    // an object the cart can't render (it reads customer.name[0]).
    if (!c || !c.name) {
      Alert.alert("Couldn't save customer", "Unexpected response from server.");
      return;
    }
    setCustomers((list) => [c!, ...list]);
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
                color={C.brand}
              />
            </TouchableOpacity>
          </View>

          {showAdd ? (
            <View style={{ padding: 20, gap: 14 }}>
              <TextInput
                placeholder="Name"
                placeholderTextColor={C.ink3}
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
                <Ionicons name="search" size={18} color={C.ink3} />
                <TextInput
                  placeholder="Search"
                  placeholderTextColor={C.ink3}
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
            { backgroundColor: deliveryOn ? C.brand : C.lineStrong },
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
    { key: "new", label: "New Order", icon: "list-outline", color: C.warn },
    { key: "preparing", label: "Preparing", icon: "restaurant-outline", color: "#3B82F6" },
    { key: "completed", label: "Completed", icon: "checkmark-circle-outline", color: C.ok },
    { key: "cancel", label: "Cancel", icon: "close-circle-outline", color: C.danger },
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
              <Ionicons name="close" size={26} color={C.ink2} />
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
            <Ionicons name="search" size={16} color={C.ink3} />
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
                        color={C.ink3}
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
                                    ? C.dangerTint
                                    : C.okTint,
                              },
                            ]}
                          >
                            <Text
                              style={[
                                styles.delBadgeText,
                                {
                                  color:
                                    o.delivery_status === "DELIVERING"
                                      ? C.dangerDark
                                      : C.okDark,
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
                            <Ionicons name="cube-outline" size={13} color={C.ink3} />
                            <Text style={styles.metaText}>
                              {o.items.reduce((s, i) => s + i.qty, 0)}
                            </Text>
                          </View>
                          <View style={styles.metaItem}>
                            <Text style={styles.metaText}>฿{o.total.toFixed(2)}</Text>
                          </View>
                          <View style={styles.metaItem}>
                            <Ionicons name="time-outline" size={13} color={C.ink3} />
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
              <Ionicons name="close" size={26} color={C.ink2} />
            </TouchableOpacity>
            <Text style={styles.modalTitle}>Save & Retrieve</Text>
            <View style={{ width: 26 }} />
          </View>

          {currentCart.length > 0 && (
            <TouchableOpacity style={styles.parkBtn} onPress={onPark} testID="park-current">
              <Ionicons name="bookmark" size={18} color={C.brand} />
              <Text style={styles.parkBtnText}>Park current order ({currentCart.length} items)</Text>
            </TouchableOpacity>
          )}

          <FlatList
            data={parked}
            keyExtractor={(i) => i.id}
            contentContainerStyle={{ padding: 16, gap: 10 }}
            ListEmptyComponent={
              <View style={styles.empty}>
                <Ionicons name="bookmarks-outline" size={40} color={C.lineStrong} />
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
                  <Ionicons name="trash-outline" size={20} color={C.danger} />
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
              <Ionicons name="checkmark" size={56} color={C.surface} />
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
        <ActivityIndicator size="small" color={C.ink2} />
        <Text style={styles.printPillText}>Printing receipt…</Text>
      </View>
    );
  }

  if (status.state === "printed") {
    return (
      <View style={[styles.printPill, styles.printPillOk]} testID="print-status-printed">
        <Ionicons name="checkmark-circle" size={16} color={C.ok} />
        <Text style={[styles.printPillText, { color: C.okDark }]}>Receipt printed</Text>
      </View>
    );
  }

  // queued — printer unreachable; useStarPrinter has saved the job and
  // will retry every 30s until the printer comes back online.
  return (
    <View style={[styles.printPill, styles.printPillWarn]} testID="print-status-queued">
      <Ionicons name="time-outline" size={16} color={C.warnDark} />
      <View style={{ flex: 1 }}>
        <Text style={[styles.printPillText, { color: C.warnDark, fontWeight: "700" }]}>
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
            <StatCard label="Sales" value="฿12,480.00" icon="trending-up" color={C.brand} />
            <StatCard label="Orders" value="24" icon="receipt-outline" color="#3B82F6" />
            <StatCard label="Avg Ticket" value="฿520.00" icon="pulse" color={C.warn} />
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
  root: { flex: 1, backgroundColor: C.bg },

  // Top bar
  topBar: {
    height: 60,
    backgroundColor: C.surface,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 12,
    gap: 8,
    borderBottomWidth: 1,
    borderBottomColor: C.line,
  },
  menuBtn: { width: 40, height: 40, alignItems: "center", justifyContent: "center" },
  topBrand: { flexDirection: "row", alignItems: "center", gap: 9, marginRight: 4 },
  topBrandLogo: { width: 36, height: 36, borderRadius: 10 },
  topBrandName: { fontSize: 14, fontWeight: "800", color: C.ink, letterSpacing: -0.2 },
  searchWrap: {
    flex: 1,
    height: 40,
    backgroundColor: C.bg,
    borderRadius: 10,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 12,
    gap: 8,
    maxWidth: 360,
  },
  searchInput: { flex: 1, fontSize: 14, color: C.ink, ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}) },
  tbItem: { alignItems: "center", paddingHorizontal: 12, minWidth: 64 },
  tbItemCompact: { minWidth: 40, paddingHorizontal: 6 },
  tbLabel: { fontSize: 10, color: C.ink2, marginTop: 2, fontWeight: "500" },
  badge: {
    position: "absolute",
    top: -4,
    right: -8,
    backgroundColor: C.brand,
    minWidth: 16,
    height: 16,
    borderRadius: 8,
    paddingHorizontal: 4,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { color: C.surface, fontSize: 10, fontWeight: "700" },
  staffChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 20,
    backgroundColor: C.brandTint,
  },
  staffText: { fontSize: 12, color: C.brand, fontWeight: "600" },
  branchChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
    maxWidth: 200,
  },
  branchChipText: { fontSize: 12, color: C.ink, fontWeight: "600" },
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
    borderColor: C.line,
    backgroundColor: C.surface,
    maxWidth: 130,
  },
  branchChipMobileText: { fontSize: 12, color: C.ink, fontWeight: "600" },
  adminBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: C.brand,
  },
  adminBtnText: { fontSize: 12, fontWeight: "700", color: C.brand },
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
    backgroundColor: C.surface,
    borderRadius: 10,
    paddingHorizontal: 14,
    borderWidth: 1,
    borderColor: C.line,
  },

  // Horizontal category strip — sits above the grid at every width now that
  // the vertical rail is gone.
  catStrip: {
    maxHeight: 64,
    backgroundColor: C.surface,
    borderBottomWidth: 1,
    borderBottomColor: C.line,
    paddingVertical: 10,
    flexGrow: 0,
  },
  catStripStacked: { marginTop: 12 },
  catChip: {
    paddingHorizontal: 16,
    paddingVertical: 7,
    borderRadius: 12,
    backgroundColor: C.bg,
    borderWidth: 1,
    borderColor: C.line,
    alignItems: "center",
    justifyContent: "center",
    minWidth: 72,
  },
  catChipActive: { backgroundColor: C.brand, borderColor: C.brand },
  catChipText: { fontSize: 13, fontWeight: "700", color: C.ink2 },
  catChipTextActive: { color: C.surface },
  catChipSub: { fontSize: 10, fontWeight: "500", color: C.ink3, marginTop: 1 },
  catChipSubActive: { color: C.brandTint },

  // FAB cart (mobile)
  fabCart: {
    position: "absolute",
    left: 12,
    right: 12,
    bottom: 12,
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: C.brand,
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
    backgroundColor: C.surface,
    paddingHorizontal: 6,
    alignItems: "center",
    justifyContent: "center",
  },
  fabBadgeText: { color: C.brand, fontSize: 11, fontWeight: "700" },
  fabMid: { flex: 1 },
  fabTotalLabel: { color: "rgba(255,255,255,0.8)", fontSize: 10, fontWeight: "600" },
  fabTotal: { color: C.surface, fontSize: 18, fontWeight: "700" },
  fabRight: { flexDirection: "row", alignItems: "center", gap: 4 },
  fabView: { color: C.surface, fontSize: 13, fontWeight: "700" },

  // Mobile cart bottom sheet
  cartSheetOverlay: { flex: 1, backgroundColor: C.scrim, justifyContent: "flex-end" },
  cartSheet: {
    flex: 1,
    backgroundColor: C.surface,
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    paddingTop: 10,
    maxHeight: "85%",
  },
  cartSheetHandle: {
    width: 40,
    height: 4,
    borderRadius: 2,
    backgroundColor: C.lineStrong,
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
    borderBottomColor: C.bg,
  },
  cartSheetTitle: { fontSize: 17, fontWeight: "700", color: C.ink },
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
    borderColor: C.brand,
  },
  mobileDiscText: { color: C.brand, fontSize: 13, fontWeight: "600" },

  // Center
  center: { flex: 1 },
  productCard: {
    flex: 1,
    height: 168,
    backgroundColor: C.ink,
    borderRadius: 16,
    overflow: "hidden",
    maxWidth: "24%",
    shadowColor: "#000",
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.12,
    shadowRadius: 8,
    elevation: 3,
  },
  productImg: {
    position: "absolute",
    top: 0, left: 0, right: 0, bottom: 0,
    width: "100%", height: "100%",
    backgroundColor: C.ink,
  },
  productNoImg: {
    position: "absolute",
    top: 0, left: 0, right: 0, bottom: 0,
    alignItems: "center",
    justifyContent: "center",
  },
  priceTag: {
    position: "absolute",
    top: 8,
    right: 8,
    backgroundColor: C.brand,
    paddingHorizontal: 9,
    paddingVertical: 4,
    borderRadius: 9,
  },
  priceTagText: { fontSize: 12, fontWeight: "800", color: C.surface },
  productFooter: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingHorizontal: 10,
    paddingTop: 14,
    paddingBottom: 10,
    backgroundColor: "rgba(20,20,22,0.62)",
  },
  productName: { fontSize: 13, fontWeight: "700", color: C.surface, lineHeight: 17 },
  empty: { flex: 1, alignItems: "center", justifyContent: "center", padding: 40, gap: 10 },
  emptyText: { color: C.ink3, fontSize: 14 },

  // Order panel (left column on tablet, sheet body on phone)
  orderPanel: {
    width: 330,
    backgroundColor: C.surface,
    borderRightWidth: 1,
    borderRightColor: C.line,
    padding: 14,
    flexDirection: "column",
  },
  orderPanelEmbedded: {
    flex: 1,
    width: "100%",
    borderRightWidth: 0,
  },
  cartHeader: { alignItems: "center", marginBottom: 8 },
  custChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    backgroundColor: C.brandTint,
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
  custInitial: { color: C.surface, fontWeight: "700", fontSize: 11 },
  custName: { flex: 1, fontSize: 12, fontWeight: "600", color: C.ink },

  totalBox: {
    backgroundColor: C.surface,
    paddingVertical: 14,
    marginBottom: 10,
  },
  sumTotalRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 4 },
  totalLabel: { fontSize: 13, color: C.ink2, fontWeight: "500" },
  subTotalVal: { fontSize: 14, color: C.ink, fontWeight: "600" },
  discRow: { flexDirection: "row", justifyContent: "space-between", marginTop: 6 },
  discLabel: { fontSize: 11, color: C.danger },
  discVal: { fontSize: 12, color: C.danger, fontWeight: "600" },
  totalRow: {
    flexDirection: "row",
    alignItems: "baseline",
    justifyContent: "space-between",
    marginTop: 8,
    paddingTop: 10,
    borderTopWidth: 1,
    borderTopColor: C.line,
  },
  thbText: { fontSize: 14, color: C.ink3, fontWeight: "600" },
  totalVal: { fontSize: 36, fontWeight: "300", color: C.ink, letterSpacing: -1 },
  payBtn: {
    backgroundColor: C.brand,
    height: 56,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 14,
  },
  payBtnDisabled: { backgroundColor: C.lineStrong },
  payBtnText: { color: C.surface, fontSize: 18, fontWeight: "700" },
  cartListHeader: {
    flexDirection: "row",
    justifyContent: "flex-end",
    marginBottom: 8,
  },
  cartListCount: { fontSize: 11, color: C.ink3 },
  cartFooterRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "flex-end",
    paddingVertical: 10,
    paddingHorizontal: 6,
    borderTopWidth: 1,
    borderTopColor: C.line,
    marginTop: 6,
  },
  footerTrash: {
    width: 34,
    height: 34,
    borderRadius: 8,
    backgroundColor: C.dangerTint,
    alignItems: "center",
    justifyContent: "center",
  },
  cartItem: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 10,
    borderBottomWidth: 1,
    borderBottomColor: C.bg,
  },
  cartItemName: { fontSize: 12, color: C.ink, fontWeight: "600" },
  cartItemPrice: { fontSize: 11, color: C.ink3, marginTop: 2 },
  qtyCtrl: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    backgroundColor: C.bg,
    borderRadius: 8,
    padding: 2,
  },
  qtyBtn: { width: 28, height: 28, alignItems: "center", justifyContent: "center" },
  qtyText: { fontSize: 13, fontWeight: "700", minWidth: 18, textAlign: "center" },
  trashBtn: { padding: 4 },
  emptyCart: { alignItems: "center", paddingVertical: 40, gap: 6 },
  emptyCartText: { fontSize: 13, color: C.ink3, fontWeight: "600" },
  emptyCartSub: { fontSize: 11, color: C.lineStrong },
  clearBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    marginTop: 8,
  },
  clearBtnText: { color: C.danger, fontSize: 12, fontWeight: "600" },

  // Generic modal
  overlay: {
    flex: 1,
    backgroundColor: C.scrim,
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
    borderBottomColor: C.line,
  },
  modalTitle: { fontSize: 17, fontWeight: "700", color: C.ink, flex: 1, textAlign: "center", marginHorizontal: 8 },

  // Cart item edit modal (per-line qty + discount)
  itemModal: { width: "100%", maxWidth: 440, backgroundColor: C.surface, borderRadius: 16, overflow: "hidden" },
  itemRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 16,
    borderBottomWidth: 1,
    borderBottomColor: C.bg,
  },
  itemRowLabel: { fontSize: 15, color: C.ink, fontWeight: "600" },
  itemStepper: { flexDirection: "row", alignItems: "center", gap: 18 },
  itemStepBtn: {
    width: 38,
    height: 38,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.line,
    alignItems: "center",
    justifyContent: "center",
  },
  itemStepVal: { fontSize: 18, fontWeight: "700", minWidth: 28, textAlign: "center", color: C.ink },
  itemDiscControls: { flexDirection: "row", alignItems: "center", gap: 10 },
  discModeToggle: {
    flexDirection: "row",
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 10,
    overflow: "hidden",
  },
  discModeBtn: {
    width: 38,
    height: 38,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: C.surface,
  },
  discModeBtnActive: { backgroundColor: C.brand },
  discModeText: { fontSize: 16, fontWeight: "700", color: C.ink2Soft },
  discModeTextActive: { color: C.surface },
  itemDiscInput: {
    minWidth: 90,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 8,
    fontSize: 16,
    fontWeight: "700",
    color: C.danger,
    textAlign: "right",
  },
  itemDiscSummaryRow: {
    paddingHorizontal: 20,
    paddingBottom: 12,
    marginTop: -4,
    alignItems: "flex-end",
  },
  itemDiscSummaryText: { fontSize: 13, fontWeight: "700", color: C.danger },
  itemTotalRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 16,
  },
  itemTotalVal: { fontSize: 18, fontWeight: "800", color: C.brand },
  cartItemDisc: { fontSize: 11, color: C.danger, marginTop: 2, fontWeight: "600" },

  // Payment modal
  paymentModal: {
    width: "92%",
    maxWidth: 1000,
    height: "88%",
    backgroundColor: C.surface,
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
    borderColor: C.line,
    alignItems: "center",
    gap: 6,
    minWidth: 100,
    flexGrow: 1,
    flexBasis: 100,
  },
  methodBtnActive: { borderColor: C.brand, backgroundColor: C.brandTint },
  methodText: { fontSize: 12, fontWeight: "600", color: C.ink2 },
  methodTextActive: { color: C.brand },

  padCol: { flex: 1, gap: 10 },
  amountDisplay: {
    backgroundColor: C.bg,
    borderRadius: 12,
    padding: 18,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  thbSmall: { fontSize: 14, color: C.ink3, fontWeight: "600" },
  amountText: { fontSize: 32, fontWeight: "700", color: C.ink },

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
    backgroundColor: C.bgSoft,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  padText: { fontSize: 24, fontWeight: "600", color: C.ink },

  // Bottom row: Clear + Backspace side-by-side
  padBottomRow: {
    flexDirection: "row",
    gap: 8,
  },
  clearPadBtnHalf: {
    flex: 1,
    backgroundColor: C.warnTint,
    borderRadius: 12,
    paddingVertical: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  backspacePadBtn: {
    flex: 1,
    backgroundColor: C.dangerTint,
    borderRadius: 12,
    paddingVertical: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  clearPadBtn: {
    backgroundColor: C.warnTint,
    padding: 12,
    borderRadius: 12,
    alignItems: "center",
  },
  clearPadText: { fontSize: 16, fontWeight: "700", color: C.warnDark },

  // Quick amounts as a vertical column beside numpad
  quickColInline: {
    width: 64,
    gap: 8,
    justifyContent: "flex-start",
  },
  quickBtnInline: {
    backgroundColor: C.bgSoft,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    paddingVertical: 12,
  },
  quickTextInline: { fontSize: 14, fontWeight: "700", color: C.brand },
  // Quick-amount chips shown horizontally above the keypad on phone, so the
  // numpad gets the full width instead of fighting with a side column.
  quickRowMobile: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
  },
  quickChipMobile: {
    backgroundColor: C.brandTint,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 999,
  },
  quickChipMobileText: { fontSize: 13, fontWeight: "700", color: C.brand },

  quickCol: { width: 168, minWidth: 168, gap: 12 },
  netBox: {
    backgroundColor: C.bg,
    borderRadius: 12,
    padding: 12,
    alignItems: "center",
  },
  netLabel: { fontSize: 11, color: C.ink2, fontWeight: "600" },
  netHint: { fontSize: 9, color: C.ink3, marginTop: 2 },
  netVal: { fontSize: 16, color: C.brand, fontWeight: "700", marginTop: 6 },
  quickBtn: {
    backgroundColor: C.bgSoft,
    borderRadius: 10,
    padding: 14,
    alignItems: "center",
  },
  quickText: { fontSize: 16, fontWeight: "700", color: C.ink },

  // QR pane (Thai QR / KBank)
  qrPane: {
    flex: 1,
    backgroundColor: C.surface,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: C.line,
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
  qrHeaderText: { color: C.surface, fontSize: 16, fontWeight: "700" },
  qrBrandRow: { flexDirection: "row", gap: 10, flexWrap: "wrap", justifyContent: "center" },
  brandPill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    backgroundColor: C.bg,
    borderRadius: 4,
    fontSize: 11,
    color: C.ink2,
    fontWeight: "700",
  },
  qrHint: { fontSize: 11, color: C.ink2, textAlign: "center" },
  qrBy: { fontSize: 11, color: C.ink3 },
  qrBox: {
    width: 160,
    height: 160,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: C.surface,
    borderWidth: 2,
    borderColor: C.brand,
    borderRadius: 10,
  },
  qrAmount: { fontSize: 22, fontWeight: "700", color: C.brand },
  qrInstructBox: {
    backgroundColor: C.bg,
    padding: 10,
    borderRadius: 8,
    flexDirection: "row",
    gap: 6,
    alignItems: "flex-start",
  },
  qrInstructText: { flex: 1, fontSize: 11, color: C.ink2 },

  // Custom pane
  customPane: {
    flex: 1,
    backgroundColor: C.surface,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: C.line,
    padding: 14,
    gap: 12,
  },
  customRef: {
    borderBottomWidth: 1,
    borderBottomColor: C.line,
    paddingVertical: 10,
    fontSize: 14,
    color: C.ink,
    ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}),
  },
  customAmountRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingBottom: 10,
    borderBottomWidth: 1,
    borderBottomColor: C.line,
  },
  customAmountLabel: { fontSize: 14, color: C.ink2 },
  customAmountVal: { fontSize: 16, fontWeight: "700", color: C.ink },
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
    borderColor: C.line,
  },
  customOptionActive: { borderColor: C.brand, backgroundColor: C.brandTint },
  radio: {
    width: 18,
    height: 18,
    borderRadius: 9,
    borderWidth: 2,
    borderColor: C.lineStrong,
    alignItems: "center",
    justifyContent: "center",
  },
  radioActive: { borderColor: C.brand },
  radioInner: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: C.brand,
  },
  customOptionText: { flex: 1, fontSize: 12, color: C.ink, fontWeight: "500" },

  // Credit pane
  creditPane: { padding: 16, gap: 14 },
  creditAmtRow: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    paddingHorizontal: 16, paddingVertical: 14,
    backgroundColor: C.bg, borderRadius: 12,
  },
  creditAmtLabel: { fontSize: 14, color: C.ink2, fontWeight: "600" },
  creditAmtVal: { fontSize: 22, fontWeight: "700", color: C.ink },
  creditInput: {
    height: 46, paddingHorizontal: 14,
    backgroundColor: C.surface, borderWidth: 1, borderColor: C.line,
    borderRadius: 10, fontSize: 14, color: C.ink,
  },
  creditDivider: {
    fontSize: 12, fontWeight: "600", color: C.ink2,
    textAlign: "center", marginVertical: 4,
  },
  creditGrid: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  creditOption: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 12, paddingVertical: 12,
    backgroundColor: C.surface,
    borderWidth: 1, borderColor: C.line, borderRadius: 10,
    width: "48%",
    gap: 4,
  },
  creditOptionActive: { borderColor: C.brand, backgroundColor: C.brandTintSoft },
  creditOptionText: { fontSize: 13, color: C.ink, fontWeight: "600" },
  bankBadge: {
    width: 24, height: 24, borderRadius: 6, marginRight: 6,
    alignItems: "center", justifyContent: "center",
  },
  bankBadgeText: { color: C.surface, fontSize: 12, fontWeight: "700" },
  bankSubText: { fontSize: 10, color: C.ink3, fontWeight: "600" },
  summaryBox: {
    backgroundColor: C.bgSoft,
    padding: 12,
    borderRadius: 10,
    alignItems: "center",
    marginTop: 10,
  },
  summaryVal: {
    fontSize: 18,
    fontWeight: "700",
    color: C.ink,
    marginTop: 4,
  },

  // Payment right panel v2 (matching screenshot)
  netBoxV2: {
    flex: 1,
    backgroundColor: C.surface,
    borderRadius: 16,
    padding: 14,
    gap: 14,
    borderWidth: 1,
    borderColor: C.line,
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
    borderBottomColor: C.bg,
  },
  guestText: { fontSize: 13, color: C.ink2, fontWeight: "600" },
  netRowV2: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  netLabelV2: { fontSize: 13, color: C.ink2, fontWeight: "600" },
  netValV2: { fontSize: 18, color: C.ink, fontWeight: "700" },
  tapEqualRow: {
    flexDirection: "column",
    alignItems: "flex-start",
    gap: 2,
    backgroundColor: C.brandTintSoft,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderWidth: 1,
    borderColor: C.okBorder,
  },
  tapEqualLabel: { fontSize: 12, color: C.brand, fontWeight: "600" },
  tapEqualVal: { fontSize: 18, color: C.danger, fontWeight: "700" },
  payConfirmBtn: {
    backgroundColor: C.brand,
    paddingVertical: 15,
    paddingHorizontal: 14,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    minHeight: 52,
  },
  payConfirmText: { color: C.surface, fontSize: 15, fontWeight: "700" },
  itemCountText: {
    fontSize: 11,
    color: C.ink2Soft,
    textAlign: "center",
    marginTop: 2,
    lineHeight: 16,
  },
  summaryCard: {
    paddingTop: 12,
    borderTopWidth: 1,
    borderTopColor: C.bg,
    gap: 4,
  },
  summaryLabel: { fontSize: 12, color: C.ink2Soft, fontWeight: "600" },
  summaryValue: { fontSize: 20, color: C.ink, fontWeight: "700" },

  // EDC marketing pane
  edcPane: {
    flex: 1,
    backgroundColor: C.surface,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: C.line,
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
    color: C.ink,
    textAlign: "center",
    lineHeight: 24,
  },
  edcBody: {
    fontSize: 13,
    color: C.ink2,
    textAlign: "center",
    lineHeight: 20,
  },
  edcBy: { fontSize: 12, color: C.ink3 },
  edcRegister: {
    backgroundColor: C.brand,
    paddingVertical: 14,
    paddingHorizontal: 40,
    borderRadius: 10,
    marginTop: 8,
  },
  edcRegisterText: { color: C.surface, fontSize: 15, fontWeight: "700" },

  paymentFooter: {
    flexDirection: "row",
    alignItems: "center",
    padding: 16,
    gap: 16,
    borderTopWidth: 1,
    borderTopColor: C.line,
  },
  changeBox: { flex: 1 },
  changeLabel: { fontSize: 11, color: C.ink3 },
  changeVal: { fontSize: 20, fontWeight: "700", color: C.ink },
  confirmBtn: {
    flex: 1,
    backgroundColor: C.brand,
    paddingVertical: 16,
    borderRadius: 12,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
  },
  confirmText: { color: C.surface, fontSize: 16, fontWeight: "700" },

  // Discount modal
  discountModal: {
    width: "92%",
    maxWidth: 520,
    backgroundColor: C.surface,
    borderRadius: 20,
    padding: 4,
    overflow: "hidden",
  },
  toggleRow: {
    flexDirection: "row",
    margin: 16,
    backgroundColor: C.bg,
    borderRadius: 10,
    padding: 4,
  },
  toggleBtn: { flex: 1, paddingVertical: 10, alignItems: "center", borderRadius: 8 },
  toggleBtnActive: { backgroundColor: C.surface },
  toggleText: { fontSize: 13, fontWeight: "600", color: C.ink3 },
  toggleTextActive: { color: C.ink },
  discountInput: {
    marginHorizontal: 16,
    backgroundColor: C.bgSoft,
    padding: 20,
    borderRadius: 12,
    alignItems: "center",
  },
  discountInputText: { fontSize: 32, fontWeight: "700", color: C.brand },
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
    borderColor: C.brand,
    alignItems: "center",
    justifyContent: "center",
  },
  quickPctText: { fontSize: 16, fontWeight: "700", color: C.brand },
  scanBarBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 6,
    backgroundColor: C.bg,
  },
  scanBarText: { fontSize: 10, color: C.ink2, fontWeight: "600" },
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
  discKeyText: { fontSize: 26, fontWeight: "500", color: C.brand },
  doneBtn: {
    backgroundColor: C.brand,
    padding: 16,
    alignItems: "center",
    margin: 16,
    borderRadius: 12,
  },
  doneBtnText: { color: C.surface, fontSize: 16, fontWeight: "700" },

  // Customer modal
  customerModal: {
    width: "92%",
    maxWidth: 560,
    height: "80%",
    backgroundColor: C.surface,
    borderRadius: 20,
    overflow: "hidden",
  },
  cancelText: { color: C.danger, fontSize: 14, fontWeight: "600" },
  searchBox: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    margin: 16,
    backgroundColor: C.bg,
    borderRadius: 10,
    paddingHorizontal: 12,
    height: 40,
  },
  searchInput2: { flex: 1, fontSize: 14, color: C.ink, ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}) },
  custRow: { flexDirection: "row", gap: 12, alignItems: "center", padding: 16 },
  custAvatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    alignItems: "center",
    justifyContent: "center",
  },
  custAvatarText: { color: C.surface, fontWeight: "700", fontSize: 18 },
  custRowName: { fontSize: 14, fontWeight: "600", color: C.ink },
  custRowPhone: { fontSize: 12, color: C.ink2, marginTop: 2 },
  custRowLast: { fontSize: 11, color: C.ink3, marginTop: 2 },
  sep: { height: 1, backgroundColor: C.bg, marginLeft: 76 },
  textInput: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 10,
    padding: 14,
    fontSize: 14,
    color: C.ink,
  },
  saveCustBtn: {
    backgroundColor: C.brand,
    padding: 14,
    borderRadius: 10,
    alignItems: "center",
  },
  saveCustText: { color: C.surface, fontSize: 15, fontWeight: "700" },

  // Order Hub
  orderHub: {
    width: "96%",
    height: "92%",
    backgroundColor: C.surface,
    borderRadius: 20,
    overflow: "hidden",
  },
  deliveryCtrl: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap" },
  deliveryCtrlNarrow: {
    paddingHorizontal: 16,
    paddingTop: 10,
    borderBottomWidth: 1,
    borderBottomColor: C.line,
    paddingBottom: 10,
  },
  delToggle: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 20,
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  delDot: { width: 10, height: 10, borderRadius: 5 },
  delText: { fontSize: 11, color: C.ink2, fontWeight: "600" },
  delMenu: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 20,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  delMenuText: { fontSize: 11, color: C.ink2, fontWeight: "600" },
  hubTabs: {
    flexDirection: "row",
    marginHorizontal: 16,
    marginTop: 14,
    backgroundColor: C.bg,
    borderRadius: 10,
    padding: 4,
  },
  hubTab: { flex: 1, paddingVertical: 10, alignItems: "center", borderRadius: 8 },
  hubTabActive: { backgroundColor: C.surface },
  hubTabText: { fontSize: 13, color: C.ink3, fontWeight: "600" },
  hubTabTextActive: { color: C.ink },
  hubSearch: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    margin: 16,
    backgroundColor: C.bg,
    borderRadius: 10,
    padding: 12,
  },
  hubSearchText: { fontSize: 13, color: C.ink3 },
  kanban: { flex: 1, flexDirection: "row", paddingHorizontal: 12, paddingBottom: 16, gap: 10 },
  kanbanNarrow: { flex: 0, flexDirection: "column" },
  kanCol: { flex: 1, backgroundColor: C.bgSoft, borderRadius: 12, padding: 10 },
  kanColNarrow: { flex: 0, width: "100%" },
  kanHead: { flexDirection: "row", alignItems: "center", gap: 6, marginBottom: 12 },
  kanTitle: { flex: 1, fontSize: 13, fontWeight: "700", color: C.ink },
  kanCount: {
    minWidth: 22,
    height: 22,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 6,
  },
  kanCountText: { color: C.surface, fontSize: 11, fontWeight: "700" },
  orderCard: {
    backgroundColor: C.surface,
    borderRadius: 10,
    padding: 10,
    marginBottom: 8,
    borderWidth: 1,
    borderColor: C.line,
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
    backgroundColor: C.brand,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
  },
  grabText: { color: C.surface, fontSize: 10, fontWeight: "700" },
  orderNum: { fontSize: 14, fontWeight: "700", color: C.ink },
  orderMeta: { flexDirection: "row", gap: 12, marginTop: 8 },
  metaItem: { flexDirection: "row", alignItems: "center", gap: 3 },
  metaText: { fontSize: 11, color: C.ink2 },

  // Parked
  parkedModal: {
    width: "92%",
    maxWidth: 560,
    height: "70%",
    backgroundColor: C.surface,
    borderRadius: 20,
    overflow: "hidden",
  },
  parkBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    padding: 14,
    margin: 16,
    backgroundColor: C.brandTint,
    borderRadius: 12,
    borderWidth: 2,
    borderColor: C.brand,
    borderStyle: "dashed",
  },
  parkBtnText: { color: C.brand, fontSize: 14, fontWeight: "700" },
  parkRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: 14,
    backgroundColor: C.bgSoft,
    borderRadius: 12,
  },
  parkLabel: { fontSize: 14, fontWeight: "700", color: C.ink },
  parkSub: { fontSize: 11, color: C.ink3, marginTop: 2 },
  retrieveBtn: {
    backgroundColor: C.brand,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 8,
  },
  retrieveBtnText: { color: C.surface, fontSize: 12, fontWeight: "700" },

  // Success
  successModal: {
    width: "92%",
    maxWidth: 440,
    backgroundColor: C.surface,
    borderRadius: 24,
    padding: 28,
    alignItems: "center",
  },
  successIcon: {
    width: 80,
    height: 80,
    borderRadius: 40,
    backgroundColor: C.brand,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 16,
  },
  successTitle: { fontSize: 20, fontWeight: "700", color: C.ink },
  successOrder: { fontSize: 14, color: C.ink3, marginTop: 4, marginBottom: 20 },
  successRow: {
    width: "100%",
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 8,
    borderBottomWidth: 1,
    borderBottomColor: C.bg,
  },
  successLabel: { fontSize: 13, color: C.ink2 },
  successVal: { fontSize: 13, color: C.ink, fontWeight: "600" },
  changeRow: { borderBottomWidth: 0, marginTop: 8, paddingTop: 12 },
  changeRowLabel: { fontSize: 14, color: C.ink, fontWeight: "700" },
  changeRowVal: { fontSize: 18, color: C.brand, fontWeight: "700" },
  successBtn: {
    backgroundColor: C.brand,
    paddingVertical: 14,
    paddingHorizontal: 20,
    borderRadius: 12,
    marginTop: 20,
    width: "100%",
    alignItems: "center",
  },
  successBtnText: { color: C.surface, fontSize: 15, fontWeight: "700" },

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
  printPillNeutral: { backgroundColor: C.bg, borderColor: C.line },
  printPillOk: { backgroundColor: C.okTint, borderColor: C.okBorder },
  printPillWarn: { backgroundColor: C.warnTint, borderColor: "#FCD34D" },
  printPillText: { fontSize: 13, color: C.ink2, fontWeight: "600" },
  printPillSub: { fontSize: 11, color: C.warnDark, marginTop: 2 },

  // Selling gate (no open shift)
  shiftGate: {
    ...StyleSheet.absoluteFill,
    backgroundColor: "rgba(244,244,246,0.92)",
    alignItems: "center",
    justifyContent: "center",
    gap: 14,
  },
  shiftGateText: { fontSize: 17, color: C.ink2, fontWeight: "600" },
  shiftGateBtn: {
    backgroundColor: C.brand,
    paddingHorizontal: 40,
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: "center",
  },
  shiftGateBtnText: { color: C.surface, fontSize: 15, fontWeight: "700", letterSpacing: 1 },
  gateModalOverlay: {
    flex: 1,
    backgroundColor: C.scrim,
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  gateModal: { width: "100%", maxWidth: 380, backgroundColor: C.surface, borderRadius: 16, overflow: "hidden" },
  gateModalHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  gateModalTitle: { fontSize: 16, fontWeight: "700", color: C.ink },
  gateModalLabel: { fontSize: 13, color: C.ink2, fontWeight: "500" },
  gateModalInput: {
    borderWidth: 1, borderColor: C.line, borderRadius: 10,
    paddingHorizontal: 14, paddingVertical: 12, fontSize: 18, color: C.ink,
    ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}),
  },

  // Drawer
  drawerOverlay: {
    flex: 1,
    backgroundColor: C.scrim,
    flexDirection: "row",
  },
  drawerPanel: {
    width: 360,
    height: "100%",
    backgroundColor: C.surface,
    padding: 24,
  },
  drawerTitle: { fontSize: 22, fontWeight: "700", color: C.ink },
  drawerSub: { fontSize: 13, color: C.ink3, marginTop: 4, marginBottom: 20 },
  drawerStats: { gap: 10 },
  statCard: {
    backgroundColor: C.bgSoft,
    borderRadius: 12,
    padding: 14,
    borderLeftWidth: 4,
    gap: 4,
  },
  statLabel: { fontSize: 11, color: C.ink3, fontWeight: "600" },
  statValue: { fontSize: 18, color: C.ink, fontWeight: "700" },
  drawerNote: { fontSize: 12, color: C.ink3, marginTop: 24, fontStyle: "italic" },
  drawerClose: {
    ...MARGIN_TOP_AUTO,
    padding: 14,
    backgroundColor: C.bg,
    borderRadius: 12,
    alignItems: "center",
  },
  drawerCloseText: { fontSize: 14, fontWeight: "600", color: C.ink2 },

  // ---------- Easy Pay pane ----------
  easyPayPane: {
    flex: 1,
    backgroundColor: C.brandTintSoft,
    borderRadius: 16,
    padding: 20,
    alignItems: "center",
    gap: 10,
  },
  easyPayThai: {
    fontSize: 22,
    fontWeight: "700",
    color: C.ink,
    textAlign: "center",
  },
  easyPayTitle: {
    fontSize: 30,
    fontWeight: "700",
    color: C.brand,
    textAlign: "center",
  },
  easyPayBadges: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    justifyContent: "center",
  },
  easyPayBadge: {
    backgroundColor: C.brand,
    paddingHorizontal: 12,
    paddingVertical: 5,
    borderRadius: 20,
  },
  easyPayBadgeText: { color: C.surface, fontSize: 12, fontWeight: "600" },
  easyPayCard: {
    flexDirection: "row",
    gap: 16,
    backgroundColor: C.surface,
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
  easyPayBrandText: { color: C.surface, fontSize: 10, fontWeight: "700" },
  easyPayRegister: {
    backgroundColor: C.brand,
    paddingVertical: 14,
    paddingHorizontal: 40,
    borderRadius: 30,
    marginTop: 4,
    width: "70%",
    alignItems: "center",
  },
  easyPayRegisterText: { color: C.surface, fontSize: 16, fontWeight: "700" },

  // ---------- PromptPay pane ----------
  promptPayPane: {
    flex: 1,
    backgroundColor: C.surface,
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
    color: C.surface,
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
    color: C.danger,
    textAlign: "center",
  },
  promptPayHint: {
    fontSize: 13,
    color: C.ink2,
    textAlign: "center",
    lineHeight: 20,
  },
  printQrBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: 1,
    borderColor: C.lineStrong,
    borderRadius: 8,
    paddingVertical: 10,
    paddingHorizontal: 20,
    ...MARGIN_TOP_AUTO,
  },
  printQrText: { fontSize: 14, color: C.ink2, fontWeight: "600" },

  // ---------- QR Kbank pane ----------
  qrKbankPane: {
    flex: 1,
    backgroundColor: C.surface,
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
  kbankBrandText: { color: C.surface, fontSize: 12, fontWeight: "600" },
  kbankSupportText: {
    fontSize: 13,
    color: C.ink2,
    textAlign: "center",
    paddingHorizontal: 12,
    lineHeight: 20,
  },
  kbankByRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  kbankByLabel: { fontSize: 13, color: C.ink2 },
  kbankLogoPill: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 10,
  },
  kbankLogoText: { color: C.surface, fontSize: 12, fontWeight: "700" },
  kbankIllustration: {
    width: 80,
    height: 80,
    alignItems: "center",
    justifyContent: "center",
  },
  kbankRegisterBtn: {
    backgroundColor: C.brand,
    width: "90%",
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: "center",
    ...MARGIN_TOP_AUTO,
  },
  kbankRegisterText: { color: C.surface, fontSize: 15, fontWeight: "700" },

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
    backgroundColor: C.brand,
  },
  edcArrowLabel: {
    backgroundColor: C.surface,
    paddingHorizontal: 6,
    fontSize: 11,
    color: C.brand,
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
    borderBottomColor: C.line,
    paddingVertical: 10,
    paddingHorizontal: 2,
    gap: 8,
  },
  customRefLabel: {
    fontSize: 14,
    color: C.ink2Soft,
    minWidth: 70,
  },
  customRefInput: {
    flex: 1,
    fontSize: 14,
    color: C.ink,
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
    borderBottomColor: C.line,
  },
  beamLogoBox: {
    width: 44,
    height: 44,
    borderRadius: 10,
    backgroundColor: C.brandTint,
    alignItems: "center",
    justifyContent: "center",
  },
  beamTitle: { fontSize: 16, fontWeight: "700", color: C.ink },
  beamSub: { fontSize: 12, color: C.ink2Soft, marginTop: 2 },
  beamIdleBox: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    paddingVertical: 24,
  },
  beamIdleText: { fontSize: 13, color: C.ink3, textAlign: "center" },
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
  beamWaitingText: { fontSize: 13, color: C.ink2 },
  beamAmount: {
    fontSize: 22,
    fontWeight: "700",
    color: C.ink,
    textAlign: "center",
    paddingTop: 8,
    borderTopWidth: 1,
    borderTopColor: C.line,
  },
  beamRetryBtn: {
    backgroundColor: C.danger,
    paddingHorizontal: 24,
    paddingVertical: 10,
    borderRadius: 8,
    marginTop: 8,
  },
  beamRetryText: { color: C.surface, fontWeight: "700", fontSize: 14 },
  beamCancelBtn: {
    borderWidth: 1,
    borderColor: C.lineStrong,
    paddingHorizontal: 20,
    paddingVertical: 8,
    borderRadius: 8,
    marginTop: 4,
  },
  beamCancelText: { color: C.ink2Soft, fontSize: 13 },

  // Omise credit-card fee + VAT breakdown
  feeBreakdown: {
    marginTop: 8,
    paddingTop: 8,
    borderTopWidth: 1,
    borderTopColor: C.line,
    gap: 4,
  },
  feeRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  feeLabel: { fontSize: 13, color: C.ink2 },
  feeVal: { fontSize: 13, color: C.ink2, fontWeight: "600" },
  feeLabelMuted: { fontSize: 12, color: C.ink3, paddingLeft: 10 },
  feeValMuted: { fontSize: 12, color: C.ink3 },
  feeRowTotal: {
    marginTop: 4,
    paddingTop: 6,
    borderTopWidth: 1,
    borderTopColor: C.line,
  },
  feeTotalLabel: { fontSize: 15, fontWeight: "700", color: C.ink },
  feeTotalVal: { fontSize: 16, fontWeight: "800", color: C.brand },
});
