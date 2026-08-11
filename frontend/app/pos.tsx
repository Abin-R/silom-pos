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
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import { useRouter, useFocusEffect } from "expo-router";
import { Ionicons, MaterialCommunityIcons } from "@expo/vector-icons";
import PhoneInput from "../components/PhoneInput";
import { useStarPrinter } from "../lib/useStarPrinter";
import { useSelfOrderPrinting } from "../lib/useSelfOrderPrinting";
import { loadLocalPrinterConfig } from "../lib/localPrinterConfig";
import { listJobs } from "../lib/printerQueue";
import { AppShell, TopBar, WIDE, railWidth, useDense } from "../components/AppShell";
import { apiFetch, clearAuthToken } from "../lib/api";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Sentry from "@sentry/react-native";
import qrcode from "qrcode-generator";
import { C, MONO, R } from "../lib/theme";
import { showAlert } from "../lib/dialog";
import { Btn, Empty, Money, SearchField, Tag } from "../lib/ui";
import { methodLabel } from "../lib/payments";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const AUTH_KEY = "bravepos:auth:v1";
const RAIL_KEY = "bravepos:rail-collapsed:v1";

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
  const { width, height } = useWindowDimensions();
  const [railPref, setRailPref] = useState<boolean | null>(null);
  useEffect(() => {
    AsyncStorage.getItem(RAIL_KEY)
      .then((v) => setRailPref(v === null ? null : v === "1"))
      .catch(() => {});
  }, []);
  // Default to collapsed on a typical tablet, where the 195px rail is exactly
  // what the category column needs.
  const railCollapsed = railPref ?? width < 1280;
  const toggleRail = () => {
    const next = !railCollapsed;
    setRailPref(next);
    AsyncStorage.setItem(RAIL_KEY, next ? "1" : "0").catch(() => {});
  };
  // `isWide` is the four-zone layout: navy rail, categories, grid, cart. It
  // must match the shell's own threshold or the rail and the columns disagree
  // about which layout is on screen.
  const isWide = width >= WIDE;
  const isMid = width >= 600;
  // Real tablets are not the 1536px the design was drawn at — a Galaxy Tab is
  // 1138. Holding the mockup's fixed 262 + 466 there leaves ~170px for the
  // grid and crushes the cards, so the furniture is derived from the viewport
  // and only *reaches* the design's numbers on a screen wide enough for them.
  const railW = railWidth(isWide, railCollapsed);
  const L = useMemo(() => {
    const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));
    if (!isWide) {
      return {
        cart: 0,
        cat: 0,
        showCat: false,
        cols: isMid ? 3 : 2,
        actionBar: 0,
        tight: false,
      };
    }
    const cart = Math.round(clamp(width * 0.32, 320, 466));
    const cat = Math.round(clamp(width * 0.18, 196, 262));
    // The column only earns its width if the grid still has room for two
    // readable cards beside it; otherwise the categories become a strip.
    const free = width - railW - cart - 32;
    const showCat = free - cat - 14 - 44 >= 340;
    const gridW = free - (showCat ? cat + 14 : 0);
    return {
      cart,
      cat,
      showCat,
      cols: clamp(Math.round((gridW - 44) / 175), 2, 4),
      // Short tablets (712px tall) can't spare 88px for the action bar.
      actionBar: height < 800 ? 68 : 88,
      tight: (gridW - 44) / 4 < 150,
    };
  }, [width, height, isWide, isMid, railW]);
  const gridCols = L.cols;

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
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Selling gate: until a shift is open, the product grid / cart are blocked.
  // `null` = still loading (render nothing rather than flash the gate).
  const [shiftOpen, setShiftOpen] = useState<boolean | null>(null);
  const [showOpenShift, setShowOpenShift] = useState(false);
  const [startCash, setStartCash] = useState("0");
  const [openingShift, setOpeningShift] = useState(false);

  // Receipts sitting in the retry queue = prints that silently failed. Polled
  // rather than pushed, because the queue is drained by a background timer in
  // useStarPrinter that this screen doesn't own.
  const [queuedPrints, setQueuedPrints] = useState(0);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const jobs = await listJobs();
        if (!stop) setQueuedPrints(jobs.length);
      } catch {
        // Queue unreadable (web, or first run) — leave the count alone.
      }
    };
    tick();
    const t = setInterval(tick, 15_000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  // Wall clock for the top bar. A minute is plenty — this is not a stopwatch.
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);

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

  // Per-category counts for the category column. Shown beside each row so a
  // cashier knows an empty grid means "nothing in here" rather than "still
  // loading" before they tap.
  const catCounts = useMemo(() => {
    const m: Record<string, number> = { favorite: 0 };
    for (const p of products) {
      if (p.is_favorite) m.favorite += 1;
      m[p.category_id] = (m[p.category_id] || 0) + 1;
    }
    return m;
  }, [products]);

  // Name of the category currently on screen — the grid header states what
  // you are looking at, which matters once the strip became a column.
  const activeCatName = useMemo(() => {
    if (search.trim()) return `Results for “${search.trim()}”`;
    if (activeCat === "favorite") return "Favorites";
    return categories.find((c) => c.id === activeCat)?.name || "Products";
  }, [activeCat, categories, search]);

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
      showAlert(
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

  // A product card is flex:1, so a lone item on the last row stretches to the
  // full grid width. Pad the row out with invisible cards to hold the shape.
  const gridData = useMemo(() => {
    const rem = filteredProducts.length % gridCols;
    if (filteredProducts.length === 0 || rem === 0) return filteredProducts;
    return [
      ...filteredProducts,
      ...Array.from({ length: gridCols - rem }, (_, i) => ({
        id: `__pad_${i}`,
        __pad: true,
      })),
    ] as Product[];
  }, [filteredProducts, gridCols]);

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

  // Navigation is identical on every screen; only what a key *means* differs.
  // Here "shop" is already the current screen, so it just closes the drawer.
  const navProps = {
    staff: staff || "Admin",
    role: role || "",
    branchName: activeBranchName || undefined,
    activeKey: "shop",
    onNavigate: (key: string) => {
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
    },
    onLogout: async () => {
      setSidebarOpen(false);
      // Full logout: backend session + in-memory token + AsyncStorage.
      // Just clearing the in-memory token leaves AUTH_KEY on disk, so
      // index.tsx's /auth/me check succeeds and bounces back to /pos.
      await doLogout();
      router.replace("/");
    },
  };

  // The four things a cashier reaches for that aren't a product. On the tablet
  // they get the action bar under the grid; on phone they ride the top bar as
  // icons, because 88px of tiles would cost a row of products.
  const actions = [
    {
      icon: "person-outline",
      label: "Customer",
      short: "Customer",
      onPress: () => setShowCustomer(true),
      testId: "toolbar-customer",
    },
    {
      icon: "bookmark-outline",
      label: "Hold Sale",
      short: "Hold",
      badge: parkedCount,
      onPress: () => setShowParked(true),
      testId: "toolbar-parked",
    },
    {
      icon: "globe-outline",
      label: "Online Orders",
      short: "Orders",
      badge: orderHubCount,
      onPress: () => setShowOrderHub(true),
      testId: "toolbar-order-hub",
    },
    {
      icon: "albums-outline",
      label: "Cash Drawer",
      short: "Cash",
      onPress: () => navProps.onNavigate("drawer"),
      testId: "toolbar-drawer",
    },
  ];

  const productGrid = (
    <FlatList
      key={`grid-${gridCols}`}
      data={gridData}
      keyExtractor={(i) => i.id}
      numColumns={gridCols}
      contentContainerStyle={styles.gridContent}
      columnWrapperStyle={{ gap: 16 }}
      showsVerticalScrollIndicator={false}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={onRefresh}
          colors={[C.brand]}
          tintColor={C.brand}
        />
      }
      ItemSeparatorComponent={() => <View style={{ height: 16 }} />}
      ListEmptyComponent={
        <Empty
          icon="search-outline"
          title="No products here"
          note={
            search.trim()
              ? "Nothing matches that search. Check the spelling, or clear it to browse by category."
              : "This category has no products yet."
          }
        />
      }
      renderItem={({ item }) =>
        (item as any).__pad ? (
          <View style={{ flex: 1 }} />
        ) : (
          <ProductCard item={item} onPress={() => addToCart(item)} />
        )
      }
    />
  );

  return (
    <AppShell
      nav={navProps}
      drawerOpen={sidebarOpen}
      onDrawerChange={setSidebarOpen}
      railCollapsed={railCollapsed}
      onToggleRail={toggleRail}
      testID="pos-screen"
    >
      <StatusBar style="dark" />

      {/* ============ TOP BAR ============ */}
      <TopBar
        onMenu={() => (isWide ? toggleRail() : setSidebarOpen(true))}
        menuOpen={isWide ? !railCollapsed : sidebarOpen}
        search={
          <SearchField
            rounded
            height={isWide ? 52 : 44}
            value={search}
            onChangeText={setSearch}
            placeholder={
              isWide ? "Search products by name or Thai name…" : "Search products"
            }
            testID="product-search"
          />
        }
        actions={
          isWide ? (
            // The right of the bar carries state the cashier would otherwise
            // only discover at the worst moment: no open shift, or receipts
            // that failed to print. Both are actionable, so both are buttons.
            <>
              {queuedPrints > 0 && (
                <TouchableOpacity
                  style={styles.statusPill}
                  onPress={() =>
                    showAlert(
                      `${queuedPrints} receipt${queuedPrints === 1 ? "" : "s"} waiting to print`,
                      "The printer was unreachable, so these were queued and are retried automatically. Check the printer is on and connected.",
                    )
                  }
                  testID="status-print-queue"
                >
                  <Ionicons name="print-outline" size={16} color={C.warnDark} />
                  <Text style={[styles.statusText, { color: C.warnDark }]}>
                    {`${queuedPrints} queued`}
                  </Text>
                </TouchableOpacity>
              )}

              {shiftOpen === false ? (
                <TouchableOpacity
                  style={[styles.statusPill, { backgroundColor: C.warnTint }]}
                  onPress={() => setShowOpenShift(true)}
                  testID="status-shift"
                >
                  <Ionicons name="lock-closed-outline" size={16} color={C.warnDark} />
                  <Text style={[styles.statusText, { color: C.warnDark }]}>
                    No shift open
                  </Text>
                </TouchableOpacity>
              ) : shiftOpen === true ? (
                <View style={[styles.statusPill, { backgroundColor: C.okTint }]}>
                  <View style={styles.statusDot} />
                  <Text style={[styles.statusText, { color: C.okDark }]}>
                    Shift open
                  </Text>
                </View>
              ) : null}

              <Money style={styles.topClock}>
                {`${String(clock.getHours()).padStart(2, "0")}:${String(clock.getMinutes()).padStart(2, "0")}`}
              </Money>
            </>
          ) : (
            <>
              {actions.map((a) => (
                <ToolbarIcon
                  key={a.testId}
                  icon={a.icon}
                  label={a.label}
                  badge={a.badge}
                  onPress={a.onPress}
                  testId={a.testId}
                  compact
                />
              ))}
            </>
          )
        }
      />

      {/* ============ MAIN LAYOUT ============ */}
      <View style={{ flex: 1, minHeight: 0 }}>
        <View style={styles.saleBody}>
          <View style={styles.saleLeft}>
            <View style={styles.saleCols}>
              {/* Categories: a column on the tablet, where there is room to
                  show the Thai name and a count; a strip on phone. */}
              {L.showCat ? (
                <ScrollView
                  style={[styles.catCol, { width: L.cat }]}
                  contentContainerStyle={{ gap: 2 }}
                  showsVerticalScrollIndicator={false}
                  testID="category-rail"
                >
                  <CatRow
                    label="Favorites"
                    emoji="★"
                    count={catCounts.favorite}
                    active={activeCat === "favorite" && !search.trim()}
                    onPress={() => {
                      setActiveCat("favorite");
                      setSearch("");
                    }}
                    testId="cat-favorite"
                  />
                  {categories
                    .filter((c) => c.name !== "Favorite")
                    .map((c) => (
                      <CatRow
                        key={c.id}
                        label={c.name}
                        sub={c.name_th}
                        color={c.color}
                        count={catCounts[c.id] || 0}
                        active={activeCat === c.id && !search.trim()}
                        onPress={() => {
                          setActiveCat(c.id);
                          setSearch("");
                        }}
                        testId={`cat-${c.id}`}
                      />
                    ))}
                </ScrollView>
              ) : null}

              <View style={styles.prodCol}>
                {L.showCat ? (
                  <View style={styles.prodHead}>
                    <Text style={styles.prodHeadTitle} numberOfLines={1}>
                      {activeCatName}
                    </Text>
                    <View style={{ flex: 1 }} />
                    <Text style={styles.prodHeadCount}>
                      {filteredProducts.length}{" "}
                      {filteredProducts.length === 1 ? "item" : "items"}
                    </Text>
                  </View>
                ) : (
                  <ScrollView
                    horizontal
                    showsHorizontalScrollIndicator={false}
                    style={styles.catStrip}
                    contentContainerStyle={{
                      paddingHorizontal: 12,
                      gap: 8,
                      alignItems: "center",
                    }}
                    testID="category-rail"
                  >
                    <CatChip
                      label="Favorites"
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

                {productGrid}
              </View>
            </View>

            {/* Action bar — tablet only; the phone carries these in the bar. */}
            {isWide && (
              <View style={[styles.actionBar, { height: L.actionBar }]}>
                {actions.map((a) => (
                  <TouchableOpacity
                    key={a.testId}
                    style={styles.act}
                    onPress={a.onPress}
                    activeOpacity={0.8}
                    testID={a.testId}
                  >
                    <Ionicons name={a.icon as any} size={L.tight ? 20 : 22} color={C.ink2} />
                    <Text
                      style={[styles.actText, L.tight && { fontSize: 13.5 }]}
                      numberOfLines={1}
                    >
                      {L.tight ? a.short : a.label}
                    </Text>
                    {!!a.badge && a.badge > 0 && (
                      <View style={styles.actBadge}>
                        <Text style={styles.actBadgeText}>{a.badge}</Text>
                      </View>
                    )}
                  </TouchableOpacity>
                ))}
              </View>
            )}
          </View>

          {/* Order panel — right-hand column on the tablet, per the design;
              on phone it collapses into the sheet behind the cart button. */}
          {isWide && (
            <CartSidebar
              width={L.cart}
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
              <Text style={styles.fabView}>Checkout</Text>
              <Ionicons name="chevron-up" size={18} color={C.surface} />
            </View>
          </TouchableOpacity>
        )}

        {/* Selling gate — blocks the grid/cart until a shift is open. The top
            bar (and its rail → admin) stays reachable above this. */}
        {shiftOpen === false && (
          <View style={styles.shiftGate} testID="shift-gate">
            <View style={styles.shiftGateCard}>
              <View style={styles.shiftGateIcon}>
                <Ionicons name="lock-closed-outline" size={32} color={C.brand} />
              </View>
              <Text style={styles.shiftGateText}>Open shift to continue</Text>
              <Text style={styles.shiftGateNote}>
                Count the drawer first — the figure you enter is what the close
                is measured against.
              </Text>
              <Btn
                label="Open shift"
                variant="blue"
                icon="play-outline"
                height={56}
                style={{ marginTop: 20, alignSelf: "stretch" }}
                onPress={() => setShowOpenShift(true)}
                testID="gate-open-shift"
              />
            </View>
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
      {/* The rail handles its own drawer on phone — see AppShell. */}
      {/* Off-screen receipt rendering target for view-shot capture.
          Only mounts when a print is in flight; invisible to the user. */}
      <ReceiptOverlay />

    </AppShell>
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

// Phone-width category chip. The tablet uses CatRow instead — a horizontal
// strip on a 195px-narrower screen would hide most of the categories.
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
      <Text
        style={[styles.catChipText, active && styles.catChipTextActive]}
        numberOfLines={1}
      >
        {label}
      </Text>
    </TouchableOpacity>
  );
}

// Tablet category row. Carries the Thai name and a count, so choosing a
// category is a decision made before the grid redraws rather than after.
function CatRow({
  label,
  sub,
  emoji,
  color,
  count,
  active,
  onPress,
  testId,
}: {
  label: string;
  sub?: string;
  emoji?: string;
  color?: string;
  count?: number;
  active: boolean;
  onPress: () => void;
  testId: string;
}) {
  return (
    <TouchableOpacity
      style={[styles.catRow, active && styles.catRowActive]}
      onPress={onPress}
      activeOpacity={0.8}
      testID={testId}
    >
      {emoji ? (
        <Text style={[styles.catEmoji, active && { color: C.brand }]}>
          {emoji}
        </Text>
      ) : (
        <View
          style={[
            styles.catDot,
            { backgroundColor: color || C.lineStrong },
          ]}
        />
      )}
      <View style={{ flex: 1, minWidth: 0 }}>
        <Text
          style={[styles.catRowText, active && styles.catRowTextActive]}
          numberOfLines={1}
        >
          {label}
        </Text>
        {!!sub && (
          <Text style={styles.catRowSub} numberOfLines={1}>
            {sub}
          </Text>
        )}
      </View>
      {count !== undefined && (
        <Money style={[styles.catCount, active && { color: C.brand }]}>
          {count}
        </Money>
      )}
    </TouchableOpacity>
  );
}

// Product tile. Image block on top, names below, price and an explicit add
// button in the footer — the whole card is still tappable, but the blue
// button tells a new cashier where to aim.
function ProductCard({
  item,
  onPress,
}: {
  item: Product;
  onPress: () => void;
}) {
  const img = item.image_base64 || item.image_url;
  return (
    <TouchableOpacity
      style={styles.pcard}
      onPress={onPress}
      activeOpacity={0.85}
      testID={`product-${item.id}`}
    >
      <View style={styles.pimg}>
        {img ? (
          <Image source={{ uri: img }} style={styles.pimgPhoto} />
        ) : (
          <Ionicons name="cafe-outline" size={38} color={C.ink3} />
        )}
      </View>
      {item.is_favorite && (
        <View style={styles.pbadge}>
          <Tag tone="low" icon="star">Favorite</Tag>
        </View>
      )}
      <Text style={styles.pname} numberOfLines={2}>
        {item.name}
      </Text>
      {!!item.name_th && (
        <Text style={styles.pnameTh} numberOfLines={1}>
          {item.name_th}
        </Text>
      )}
      <View style={styles.pfoot}>
        <Money style={styles.pprice}>{THB(item.price)}</Money>
        <View style={styles.padd}>
          <Ionicons name="add" size={20} color={C.surface} />
        </View>
      </View>
    </TouchableOpacity>
  );
}

// ---------- Cart Sidebar (shared between the tablet column + phone sheet) ----------
function CartSidebar({
  width,
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
  /** Fixed column width on tablet; the phone sheet ignores it. */
  width?: number;
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
  // Prices already include VAT (see the shop's tax setup), so the tax line is
  // the portion carved out of the total, not something added on top of it.
  const vat = total > 0 ? (total * 7) / 107 : 0;

  return (
    <View
      style={[
        styles.cart,
        !!width && { width },
        embedded && styles.cartEmbedded,
      ]}
      testID="cart-sidebar"
    >
      {!embedded && (
        <View style={styles.cartHead}>
          <Text style={styles.cartTitle}>
            Cart <Text style={styles.cartCountText}>({cartCount})</Text>
          </Text>
          <View style={{ flex: 1 }} />
          {cart.length > 0 && (
            <TouchableOpacity
              style={styles.clearBtn}
              onPress={onClear}
              testID="clear-cart"
            >
              <Ionicons name="trash-outline" size={18} color={C.danger} />
              <Text style={styles.clearText}>Clear</Text>
            </TouchableOpacity>
          )}
        </View>
      )}

      {!!customer && (
        <View style={styles.custChip}>
          <View style={[styles.custDot, { backgroundColor: customer.color }]}>
            <Text style={styles.custInitial}>
              {customer.name?.[0]?.toUpperCase()}
            </Text>
          </View>
          <Text style={styles.custName} numberOfLines={1}>
            {customer.name}
          </Text>
          <TouchableOpacity onPress={onRemoveCustomer} testID="remove-customer" hitSlop={8}>
            <Ionicons name="close" size={16} color={C.ink3} />
          </TouchableOpacity>
        </View>
      )}

      <FlatList
        data={cart}
        keyExtractor={(i) => i.product_id}
        style={{ flex: 1 }}
        contentContainerStyle={cart.length === 0 ? { flex: 1 } : undefined}
        showsVerticalScrollIndicator={false}
        ListEmptyComponent={
          <Empty
            icon="cart-outline"
            title="Cart is empty"
            note="Tap a product to start the order."
          />
        }
        renderItem={({ item }) => (
          <View style={styles.crow} testID={`cart-item-${item.product_id}`}>
            <TouchableOpacity
              style={styles.crowInfo}
              onPress={() => onEdit(item)}
              testID={`cart-item-edit-${item.product_id}`}
            >
              <Text style={styles.crowName} numberOfLines={2}>
                {item.name}
              </Text>
              <Money style={styles.crowUnit}>{`${THB(item.price)} / ea`}</Money>
              {!!item.discount && item.discount > 0 && (
                <Money style={styles.crowDisc}>
                  {`Discount −${THB(item.discount)}`}
                </Money>
              )}
            </TouchableOpacity>

            <View style={styles.qty}>
              <TouchableOpacity
                style={styles.qtyBtn}
                onPress={() => onDec(item.product_id)}
                testID={`qty-dec-${item.product_id}`}
              >
                <Ionicons name="remove" size={17} color={C.ink2} />
              </TouchableOpacity>
              <Money style={styles.qtyText}>{item.qty}</Money>
              <TouchableOpacity
                style={styles.qtyBtn}
                onPress={() => onInc(item.product_id)}
                testID={`qty-inc-${item.product_id}`}
              >
                <Ionicons name="add" size={17} color={C.ink2} />
              </TouchableOpacity>
            </View>

            <Money style={styles.crowLine}>
              {THB(item.price * item.qty - (item.discount || 0))}
            </Money>

            <TouchableOpacity
              onPress={() => onRemove(item.product_id)}
              style={styles.crowRm}
              testID={`remove-${item.product_id}`}
              hitSlop={6}
            >
              <Ionicons name="close" size={17} color={C.ink3} />
            </TouchableOpacity>
          </View>
        )}
      />

      <View style={styles.totals}>
        <View style={styles.tr}>
          <Text style={styles.trLabel}>Subtotal</Text>
          <Money style={styles.trValue}>{THB(subtotal)}</Money>
        </View>
        {discountAmount > 0 && (
          <View style={styles.tr}>
            <Text style={styles.trLabel}>Discount</Text>
            <Money style={[styles.trValue, { color: C.ok }]}>
              {`−${THB(discountAmount)}`}
            </Money>
          </View>
        )}
        <View style={styles.tr}>
          <Text style={styles.trLabel}>VAT 7% (included)</Text>
          <Money style={styles.trValue}>{THB(vat)}</Money>
        </View>
        <View style={styles.dash} />
        <View style={styles.trBig}>
          <Text style={styles.trBigLabel}>Total</Text>
          <Money style={styles.trBigValue} numberOfLines={1}>
            <Text testID="cart-total">{THB(total)}</Text>
          </Money>
        </View>
      </View>

      <TouchableOpacity
        style={[styles.checkout, cart.length === 0 && styles.checkoutOff]}
        disabled={cart.length === 0}
        onPress={onPay}
        activeOpacity={0.85}
        testID="pay-btn"
      >
        <Ionicons name="lock-closed-outline" size={20} color={C.surface} />
        <Text style={styles.checkoutText}>Checkout</Text>
        <View style={{ flex: 1 }} />
        <Money style={styles.checkoutAmt}>{THB(total)}</Money>
      </TouchableOpacity>
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
  // The keypad is opt-in. Quick-tender chips cover almost every cash sale, so
  // showing twelve keys by default puts arithmetic in front of a task that
  // rarely needs it.
  const [showPad, setShowPad] = useState(false);

  useEffect(() => {
    if (visible) {
      setAmount("");
      setMethod("Cash");
      setShowPad(false);
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
  // A 713px-tall tablet was clipping "Change to give" behind the footer, so a
  // plain cash sale needed a scroll to read its own answer.
  const dense = useDense();

  const onKey = (k: string) => {
    if (k === "clear") setAmount("");
    else if (k === "back") setAmount((a) => a.slice(0, -1));
    else if (k === ".") {
      if (!amount.includes(".")) setAmount((a) => (a || "0") + ".");
    } else setAmount((a) => (a === "0" ? k : a + k));
  };


  // Quick-tender chips — what a guest actually hands over: the next round
  // twenty, the next hundred, then the notes above that. A normal cash sale
  // becomes two taps and no arithmetic.
  const tenders = useMemo(() => {
    const out: number[] = [];
    for (const step of [20, 100, 500, 1000]) {
      const v = Math.ceil(total / step) * step;
      if (v > 0 && !out.includes(v)) out.push(v);
    }
    return out.sort((a, b) => a - b).slice(0, 4);
  }, [total]);

  const change = Math.max(0, paid - total);

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

  // Icon tint per method — cash green, QR blue, card purple. The tints are
  // the same ones the Orders list uses for a paid row, so "how was this paid"
  // reads the same colour on both screens.
  const methodTint: Record<string, { bg: string; fg: string }> = {
    [PAYMENT_METHODS.CASH]: { bg: C.okTint, fg: C.okDark },
    [PAYMENT_METHODS.BEAM]: { bg: C.brandTintSoft, fg: C.brand },
    [PAYMENT_METHODS.PROMPTPAY]: { bg: C.brandTintSoft, fg: C.brand },
    [PAYMENT_METHODS.QR_KBANK]: { bg: C.brandTintSoft, fg: C.brand },
    [PAYMENT_METHODS.CARD_LINK]: { bg: C.accentTint, fg: C.accentDark },
    [PAYMENT_METHODS.BEAM_CARD]: { bg: C.accentTint, fg: C.accentDark },
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        <View
          style={[styles.payModal, isNarrow && styles.payModalNarrow]}
          testID="payment-modal"
        >
          {/* ── Header ── */}
          <View style={[styles.payHead, dense && { height: 60 }]}>
            <Text style={styles.payHeadTitle}>Checkout</Text>
            {!isNarrow && (
              // Tag defaults to flex-start so it never stretches in a column;
              // in this centred row it has to opt back in.
              <Tag tone="info" mono style={{ alignSelf: "center" }}>
                {`${itemsCount} item${itemsCount !== 1 ? "s" : ""} · ${cartCount} pcs`}
              </Tag>
            )}
            <View style={{ flex: 1 }} />
            <TouchableOpacity
              style={styles.xbtn}
              onPress={onClose}
              testID="close-payment"
            >
              <Ionicons name="close" size={18} color={C.ink2Soft} />
            </TouchableOpacity>
          </View>

          <View style={[styles.paySplit, isNarrow && { flexDirection: "column" }]}>
            {/* ── Method column ── */}
            <ScrollView
              style={isNarrow ? styles.payLeftNarrow : styles.payLeftWrap}
              contentContainerStyle={
                isNarrow ? styles.payLeftNarrowInner : styles.payLeft
              }
              horizontal={isNarrow}
              showsHorizontalScrollIndicator={false}
              showsVerticalScrollIndicator={false}
            >
              {methods.map((m) => {
                const on = method === m.key;
                const tint = methodTint[m.key] || { bg: C.neutralTint, fg: C.ink2Soft };
                return (
                  <TouchableOpacity
                    key={m.key}
                    style={[
                      styles.pm,
                      on && styles.pmOn,
                      isNarrow && styles.pmNarrow,
                    ]}
                    onPress={() => setMethod(m.key)}
                    activeOpacity={0.8}
                    testID={`pay-method-${m.key}`}
                  >
                    <View style={[styles.pmIcon, { backgroundColor: tint.bg }]}>
                      <Ionicons name={m.icon} size={20} color={tint.fg} />
                    </View>
                    <Text
                      style={[styles.pmText, on && styles.pmTextOn]}
                      numberOfLines={1}
                    >
                      {methodLabel(m.key)}
                    </Text>
                  </TouchableOpacity>
                );
              })}

              {!isNarrow && (
                <View style={styles.payNote}>
                  <Text style={styles.payNoteText}>
                    Splitting the bill? Take one method now and charge the
                    remainder after.
                  </Text>
                </View>
              )}
            </ScrollView>

            {/* ── Detail column ── */}
            <View style={styles.payRight}>
              <View style={[styles.due, dense && { paddingTop: 14, paddingBottom: 12 }]}>
                <Text style={styles.dueLabel}>AMOUNT DUE</Text>
                <Money style={[styles.dueVal, dense && { fontSize: 30 }]} numberOfLines={1}>
                  {THB(total)}
                </Money>
              </View>

              <ScrollView
                style={{ flex: 1 }}
                contentContainerStyle={[styles.payPane, dense && { padding: 18, paddingTop: 14 }]}
                showsVerticalScrollIndicator={false}
              >
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
                    <Text style={styles.beamTitle}>PromptPay QR</Text>
                    <Text style={styles.beamSub}>Scan with any Thai banking app · via Beam</Text>
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
                    <Text style={styles.beamTitle}>Card · Omise</Text>
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
                    <Text style={styles.beamTitle}>Card · Beam</Text>
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
              <View style={styles.cashPane} testID="cash-pane">
                  <Text style={styles.paneLbl}>CASH RECEIVED</Text>
                  <View style={styles.quickGrid}>
                    {tenders.map((t) => {
                      const on = amount !== "" && parseFloat(amount) === t;
                      return (
                        <TouchableOpacity
                          key={t}
                          style={[styles.qk, dense && { height: 50 }, on && styles.qkOn]}
                          onPress={() => setAmount(String(t))}
                          testID={`tender-${t}`}
                        >
                          <Money style={[styles.qkText, on && styles.qkTextOn]}>
                            {`฿${t.toLocaleString("en-US")}`}
                          </Money>
                        </TouchableOpacity>
                      );
                    })}
                  </View>
                  <View style={[styles.quickGrid, { marginTop: 10 }]}>
                    <TouchableOpacity
                      style={[
                        styles.qk,
                        { flexBasis: "48%" },
                        dense && { height: 50 },
                        amount !== "" && parseFloat(amount) === total && styles.qkOn,
                      ]}
                      onPress={() => setAmount(String(total))}
                      testID="tender-exact"
                    >
                      <Money
                        style={[
                          styles.qkText,
                          amount !== "" && parseFloat(amount) === total && styles.qkTextOn,
                        ]}
                      >
                        {`Exact ${THB(total)}`}
                      </Money>
                    </TouchableOpacity>
                    <TouchableOpacity
                      style={[styles.qk, { flexBasis: "48%" }, dense && { height: 50 }, showPad && styles.qkOn]}
                      onPress={() => setShowPad((v) => !v)}
                      testID="tender-other"
                    >
                      <Text
                        style={[styles.qkText, showPad && styles.qkTextOn, { fontWeight: "600" }]}
                      >
                        Other amount
                      </Text>
                    </TouchableOpacity>
                  </View>

                  {/* Keypad — only when the chips don't cover what was handed
                      over, so the common case stays two taps. */}
                  {showPad && (
                    <View style={styles.padWrap}>
                      <View style={styles.padDisplay}>
                        <Text style={styles.padDisplayLbl}>THB</Text>
                        <Money style={styles.padDisplayVal} testID="amount-display">
                          {amount || "0"}
                        </Money>
                      </View>
                      <View style={styles.padGrid}>
                        {["7", "8", "9", "4", "5", "6", "1", "2", "3", "0", ".", "back"].map(
                          (k) => (
                            <TouchableOpacity
                              key={k}
                              style={styles.padBtn}
                              onPress={() => onKey(k)}
                              testID={`pad-${k}`}
                            >
                              {k === "back" ? (
                                <Ionicons
                                  name="backspace-outline"
                                  size={22}
                                  color={C.ink2}
                                />
                              ) : (
                                <Money style={styles.padText}>{k}</Money>
                              )}
                            </TouchableOpacity>
                          ),
                        )}
                      </View>
                      <TouchableOpacity
                        style={styles.padClear}
                        onPress={() => onKey("clear")}
                        testID="pad-clear"
                      >
                        <Text style={styles.padClearText}>Clear</Text>
                      </TouchableOpacity>
                    </View>
                  )}

                </View>
            )}
              </ScrollView>

              {/* Change is the answer to a cash sale, so it is pinned outside
                  the scroll area — it was being clipped behind the footer on a
                  713px screen, which made the cashier scroll to read it. */}
              {method === PAYMENT_METHODS.CASH && (
                <View style={styles.tenderPinned}>
                  <View style={[styles.tender, dense && { marginTop: 12, paddingVertical: 12, gap: 8 }]}>
                    <View style={styles.tenderRow}>
                      <Text style={styles.tenderLbl}>Received</Text>
                      <Money style={styles.tenderVal}>
                        {amount ? THB(parseFloat(amount) || 0) : "—"}
                      </Money>
                    </View>
                    <View style={styles.tenderRow}>
                      <Text style={styles.tenderLbl}>Bill total</Text>
                      <Money style={styles.tenderValSoft}>{THB(total)}</Money>
                    </View>
                    <View style={[styles.tenderRow, styles.tenderChange]}>
                      <Text style={styles.tenderChangeLbl}>Change to give</Text>
                      <Money style={[styles.tenderChangeVal, dense && { fontSize: 26 }]} numberOfLines={1}>
                        {THB(change)}
                      </Money>
                    </View>
                  </View>
                </View>
              )}

              {/* ── Footer ── */}
              <View style={[styles.payFoot, dense && { padding: 14 }]}>
                <Btn
                  label="Back"
                  icon="arrow-back"
                  height={dense ? 52 : 60}
                  onPress={onClose}
                  style={{ flexGrow: 0, flexShrink: 0, width: isNarrow ? 120 : 170 }}
                />
                <Btn
                  label={confirmLabel}
                  variant="blue"
                  height={dense ? 52 : 60}
                  disabled={!canConfirm}
                  onPress={handleConfirmPayment}
                  style={{ flex: 1 }}
                  textStyle={{ fontSize: 17 }}
                  testID="confirm-payment-right"
                />
              </View>
            </View>
          </View>
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
      showAlert("Couldn't save customer", e?.message || "Please try again.");
      return;
    }
    // Guard against a success response missing the required field — never select
    // an object the cart can't render (it reads customer.name[0]).
    if (!c || !c.name) {
      showAlert("Couldn't save customer", "Unexpected response from server.");
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
  // The cashier already knows it worked. What they need in that second is the
  // number to count back into a hand — so change is the headline, and the word
  // "successful" is demoted to the tick.
  const hasChange = !!data && data.change > 0.005;

  return (
    <Modal visible={!!data} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.overlay}>
        {data && (
          <View style={styles.successModal} testID="success-modal">
            <View style={styles.successIcon}>
              <Ionicons name="checkmark" size={38} color={C.ok} />
            </View>

            <Text style={styles.successTitle}>
              {hasChange ? `Give ${THB(data.change)} change` : "Paid in full"}
            </Text>
            <Text style={styles.successOrder}>
              {`${data.order_number} · ${methodLabel(data.method)}`}
            </Text>

            <View style={styles.successTotal}>
              <Text style={styles.successTotalLbl}>BILL TOTAL</Text>
              <Money style={styles.successTotalVal} numberOfLines={1}>
                {THB(data.total)}
              </Money>
            </View>

            {/* Received/change stay available for the cashier who wants to
                check the arithmetic, just not at headline size. */}
            <View style={styles.successRow}>
              <Text style={styles.successLabel}>Received</Text>
              <Money style={styles.successVal}>{THB(data.paid)}</Money>
            </View>
            <View style={styles.successRow}>
              <Text style={styles.successLabel}>Change</Text>
              <Money style={styles.successVal}>{THB(data.change)}</Money>
            </View>

            <PrintStatusPill status={printStatus} />

            <Btn
              label="New sale"
              icon="add"
              variant="blue"
              height={64}
              onPress={onClose}
              style={{ alignSelf: "stretch", marginTop: 20 }}
              textStyle={{ fontSize: 17 }}
              testID="success-done"
            />
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

// ============ STYLES ============

/** Workaround for React Native Web typing gap — `marginTop: "auto"` is valid CSS but not typed. */
const MARGIN_TOP_AUTO = { marginTop: "auto" as any };

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },

  // ── Sale floor ─────────────────────────────────────────────────────────
  // Four zones on the tablet: rail (in the shell), category column, product
  // grid, cart. The left group and the cart are siblings so the cart keeps a
  // fixed width while the grid absorbs whatever is left.
  saleBody: { flex: 1, minHeight: 0, flexDirection: "row" },
  saleLeft: { flex: 1, minWidth: 0, padding: 16, gap: 14 },
  saleCols: { flex: 1, minHeight: 0, flexDirection: "row", gap: 14 },

  catCol: {
    width: 262,
    flexGrow: 0,
    flexShrink: 0,
    backgroundColor: C.surface,
    borderRadius: R.card,
    padding: 12,
  },
  catRow: {
    minHeight: 56,
    borderRadius: 11,
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 16,
    paddingVertical: 8,
  },
  catRowActive: { backgroundColor: C.brandTintSoft },
  catEmoji: { fontSize: 20, width: 24, textAlign: "center", color: C.ink2 },
  catDot: { width: 12, height: 12, borderRadius: 6, marginHorizontal: 6 },
  catRowText: { fontSize: 15, fontWeight: "600", color: C.ink2 },
  catRowTextActive: { color: C.brand, fontWeight: "700" },
  catRowSub: { fontSize: 12, color: C.ink3, marginTop: 2 },
  catCount: { fontSize: 12, color: C.ink3 },

  prodCol: {
    flex: 1,
    minWidth: 0,
    backgroundColor: C.surface,
    borderRadius: R.card,
    overflow: "hidden",
  },
  prodHead: {
    height: 64,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 22,
    gap: 12,
  },
  prodHeadTitle: {
    fontSize: 19,
    fontWeight: "700",
    color: C.ink,
    letterSpacing: -0.42,
    flexShrink: 1,
  },
  prodHeadCount: { fontSize: 13.5, color: C.ink3 },
  gridContent: { paddingHorizontal: 22, paddingTop: 4, paddingBottom: 22 },

  // Product tile
  pcard: {
    flex: 1,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 13,
    padding: 14,
    backgroundColor: C.surface,
  },
  pimg: {
    height: 118,
    borderRadius: 10,
    backgroundColor: C.sunk,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 14,
    overflow: "hidden",
  },
  pimgPhoto: { width: "100%", height: "100%" },
  pbadge: { position: "absolute", top: 22, left: 22 },
  pname: {
    fontSize: 15.5,
    fontWeight: "600",
    color: C.ink,
    letterSpacing: -0.23,
    lineHeight: 19,
  },
  pnameTh: { fontSize: 12.5, color: C.ink3, marginTop: 3 },
  pfoot: {
    marginTop: 10,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  pprice: { fontSize: 15.5, fontWeight: "700", color: C.ink2 },
  padd: {
    width: 38,
    height: 38,
    borderRadius: 10,
    backgroundColor: C.brand,
    alignItems: "center",
    justifyContent: "center",
  },

  // Action bar under the grid
  actionBar: { height: 88, flexDirection: "row", gap: 14 },
  act: {
    flex: 1,
    backgroundColor: C.surface,
    borderRadius: R.card,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
  },
  actText: {
    fontSize: 15,
    fontWeight: "700",
    color: C.ink2,
    letterSpacing: -0.24,
  },
  actBadge: {
    minWidth: 22,
    height: 22,
    borderRadius: 11,
    paddingHorizontal: 6,
    backgroundColor: C.brand,
    alignItems: "center",
    justifyContent: "center",
  },
  actBadgeText: { color: C.surface, fontSize: 12, fontWeight: "700" },

  // ── Cart ───────────────────────────────────────────────────────────────
  cart: {
    width: 466,
    flexGrow: 0,
    flexShrink: 0,
    backgroundColor: C.surface,
    borderLeftWidth: 1,
    borderLeftColor: C.line2,
  },
  cartEmbedded: { width: "100%", flex: 1, borderLeftWidth: 0 },
  cartHead: {
    height: 80,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 24,
    gap: 10,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  cartTitle: {
    fontSize: 22,
    fontWeight: "800",
    color: C.ink,
    letterSpacing: -0.55,
  },
  cartCountText: { color: C.ink2Soft, fontWeight: "700" },
  clearBtn: { flexDirection: "row", alignItems: "center", gap: 8 },
  clearText: { fontSize: 15, fontWeight: "700", color: C.danger },

  crow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 24,
    paddingVertical: 16,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  crowInfo: { flex: 1, minWidth: 0 },
  crowName: {
    fontSize: 15.5,
    fontWeight: "600",
    color: C.ink,
    letterSpacing: -0.23,
  },
  crowUnit: { fontSize: 13, color: C.ink2Soft, marginTop: 4 },
  crowDisc: { fontSize: 12.5, color: C.ok, marginTop: 3 },
  qty: {
    flexDirection: "row",
    alignItems: "center",
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 9,
    overflow: "hidden",
  },
  qtyBtn: {
    width: 32,
    height: 34,
    alignItems: "center",
    justifyContent: "center",
  },
  qtyText: {
    width: 44,
    height: 34,
    textAlign: "center",
    lineHeight: 34,
    fontSize: 14,
    fontWeight: "700",
    color: C.ink,
    borderLeftWidth: 1,
    borderRightWidth: 1,
    borderColor: C.line,
  },
  crowLine: {
    width: 84,
    textAlign: "right",
    fontSize: 15.5,
    fontWeight: "700",
    color: C.ink,
  },
  crowRm: { width: 22, alignItems: "center" },

  totals: {
    paddingHorizontal: 24,
    paddingTop: 20,
    borderTopWidth: 1,
    borderTopColor: C.line2,
  },
  tr: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingVertical: 6,
  },
  trLabel: { fontSize: 15, color: C.ink2Soft },
  trValue: { fontSize: 15, fontWeight: "600", color: C.ink2 },
  dash: {
    borderTopWidth: 1,
    borderTopColor: C.line,
    borderStyle: "dashed",
    marginVertical: 14,
  },
  trBig: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "baseline",
    paddingBottom: 4,
  },
  trBigLabel: {
    fontSize: 20,
    fontWeight: "800",
    color: C.ink,
    letterSpacing: -0.5,
  },
  trBigValue: {
    fontSize: 30,
    fontWeight: "800",
    color: C.brand,
    letterSpacing: -1.05,
  },
  checkout: {
    margin: 20,
    marginTop: 16,
    height: 72,
    borderRadius: R.card,
    backgroundColor: C.brand,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 26,
    gap: 16,
  },
  checkoutOff: { backgroundColor: C.lineStrong },
  checkoutText: {
    fontSize: 19,
    fontWeight: "700",
    color: C.surface,
    letterSpacing: -0.38,
  },
  checkoutAmt: { fontSize: 21, fontWeight: "800", color: C.surface },

  // ── Top-bar status cluster ─────────────────────────────────────────────
  statusPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    height: 36,
    paddingHorizontal: 14,
    borderRadius: R.pill,
    backgroundColor: C.warnTint,
  },
  statusText: { fontSize: 13.5, fontWeight: "700" },
  statusDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: C.ok,
  },
  topClock: {
    fontSize: 20,
    fontWeight: "600",
    color: C.ink2,
    letterSpacing: -0.5,
  },

  // ── Checkout modal ─────────────────────────────────────────────────────
  payModal: {
    width: "94%",
    maxWidth: 1000,
    height: "90%",
    maxHeight: 800,
    backgroundColor: C.surface,
    borderRadius: R.modal,
    overflow: "hidden",
  },
  payModalNarrow: { width: "100%", height: "100%", maxHeight: "100%", borderRadius: 0 },
  payHead: {
    height: 76,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 26,
    gap: 14,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  payHeadTitle: {
    fontSize: 21,
    fontWeight: "800",
    color: C.ink,
    letterSpacing: -0.53,
  },
  xbtn: {
    width: 40,
    height: 40,
    borderRadius: 11,
    borderWidth: 1,
    borderColor: C.line,
    alignItems: "center",
    justifyContent: "center",
  },
  paySplit: { flex: 1, minHeight: 0, flexDirection: "row" },

  payLeftWrap: {
    width: 274,
    flexGrow: 0,
    flexShrink: 0,
    borderRightWidth: 1,
    borderRightColor: C.line2,
  },
  payLeft: { padding: 18, gap: 10, flexGrow: 1 },
  payLeftNarrow: {
    maxHeight: 88,
    flexGrow: 0,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  payLeftNarrowInner: { padding: 14, gap: 10, alignItems: "center" },
  pm: {
    height: 68,
    borderRadius: 13,
    borderWidth: 1,
    borderColor: C.line,
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 16,
  },
  pmNarrow: { height: 60, paddingHorizontal: 14 },
  pmOn: {
    borderWidth: 2,
    borderColor: C.brand,
    backgroundColor: C.brandTintSoft,
    paddingHorizontal: 15,
  },
  pmIcon: {
    width: 40,
    height: 40,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
  },
  pmText: { fontSize: 16, fontWeight: "700", color: C.ink2 },
  pmTextOn: { color: C.brand },
  payNote: {
    marginTop: "auto",
    padding: 16,
    borderRadius: 13,
    backgroundColor: C.sunk,
  },
  payNoteText: { fontSize: 13.5, color: C.ink2Soft, lineHeight: 21 },

  payRight: { flex: 1, minWidth: 0 },
  due: {
    flexDirection: "row",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: 12,
    marginHorizontal: 28,
    paddingTop: 22,
    paddingBottom: 18,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  dueLabel: {
    ...MONO,
    fontSize: 10,
    letterSpacing: 1.3,
    color: C.ink2Soft,
  },
  dueVal: {
    fontSize: 38,
    fontWeight: "800",
    color: C.brand,
    letterSpacing: -1.4,
    flexShrink: 1,
  },
  payPane: { padding: 24, paddingTop: 18, flexGrow: 1 },

  cashPane: { flex: 1 },
  paneLbl: {
    ...MONO,
    fontSize: 10,
    letterSpacing: 1.3,
    color: C.ink2Soft,
    marginBottom: 12,
  },
  quickGrid: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  qk: {
    flexGrow: 1,
    flexBasis: "22%",
    height: 60,
    borderRadius: R.control,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
    alignItems: "center",
    justifyContent: "center",
  },
  qkOn: { backgroundColor: C.brand, borderColor: C.brand },
  qkText: { fontSize: 16, fontWeight: "700", color: C.ink2 },
  qkTextOn: { color: C.surface },

  padWrap: { marginTop: 16, gap: 10 },
  padDisplay: {
    flexDirection: "row",
    alignItems: "baseline",
    justifyContent: "flex-end",
    gap: 10,
    paddingHorizontal: 18,
    paddingVertical: 14,
    borderRadius: R.control,
    backgroundColor: C.sunk,
  },
  padDisplayLbl: { fontSize: 13, fontWeight: "700", color: C.ink3 },
  padDisplayVal: { fontSize: 28, fontWeight: "700", color: C.ink, letterSpacing: -0.9 },
  padClear: {
    height: 44,
    borderRadius: R.control,
    borderWidth: 1,
    borderColor: C.line,
    alignItems: "center",
    justifyContent: "center",
  },
  padClearText: { fontSize: 14, fontWeight: "700", color: C.ink2Soft },

  tender: {
    marginTop: 18,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 13,
    backgroundColor: C.sunk,
    paddingHorizontal: 20,
    paddingVertical: 18,
    gap: 12,
  },
  tenderRow: {
    flexDirection: "row",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: 12,
  },
  tenderLbl: { fontSize: 15.5, color: C.ink2Soft },
  tenderVal: { fontSize: 18, fontWeight: "700", color: C.ink },
  tenderValSoft: { fontSize: 15.5, color: C.ink2 },
  tenderChange: {
    borderTopWidth: 1,
    borderTopColor: C.line,
    paddingTop: 14,
  },
  tenderChangeLbl: { fontSize: 16, fontWeight: "700", color: C.ok },
  tenderChangeVal: {
    fontSize: 32,
    fontWeight: "800",
    color: C.ok,
    letterSpacing: -1.1,
    flexShrink: 1,
  },

  tenderPinned: {
    paddingHorizontal: 24,
    paddingBottom: 4,
  },
  payFoot: {
    flexDirection: "row",
    gap: 12,
    padding: 20,
    borderTopWidth: 1,
    borderTopColor: C.line2,
  },

  // ── Shift gate ─────────────────────────────────────────────────────────
  shiftGateCard: {
    width: "100%",
    maxWidth: 420,
    backgroundColor: C.surface,
    borderRadius: R.modal,
    padding: 32,
    alignItems: "center",
  },
  shiftGateIcon: {
    width: 64,
    height: 64,
    borderRadius: 32,
    backgroundColor: C.brandTintSoft,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 20,
  },
  shiftGateNote: {
    fontSize: 14.5,
    color: C.ink2Soft,
    textAlign: "center",
    lineHeight: 21,
    marginTop: 8,
  },

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
    maxHeight: 62,
    paddingVertical: 12,
    flexGrow: 0,
  },
  catChip: {
    height: 38,
    paddingHorizontal: 16,
    borderRadius: 10,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.line,
    alignItems: "center",
    justifyContent: "center",
    minWidth: 72,
  },
  catChipActive: { backgroundColor: C.brand, borderColor: C.brand },
  catChipText: { fontSize: 14, fontWeight: "600", color: C.ink2 },
  catChipTextActive: { color: C.surface, fontWeight: "700" },

  // FAB cart (mobile)
  fabCart: {
    position: "absolute",
    left: 12,
    right: 12,
    bottom: 12,
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: C.brand,
    borderRadius: 14,
    paddingHorizontal: 18,
    paddingVertical: 14,
    gap: 14,
    shadowColor: "#0B2050",
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.28,
    shadowRadius: 16,
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
  fabTotalLabel: { color: "rgba(255,255,255,0.75)", fontSize: 11, fontWeight: "600" },
  fabTotal: { ...MONO, color: C.surface, fontSize: 19, fontWeight: "800" },
  fabRight: { flexDirection: "row", alignItems: "center", gap: 4 },
  fabView: { color: C.surface, fontSize: 14, fontWeight: "700" },

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
    paddingHorizontal: 24,
    paddingBottom: 14,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  cartSheetTitle: { fontSize: 20, fontWeight: "800", color: C.ink, letterSpacing: -0.5 },
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
    gap: 10,
    backgroundColor: C.brandTintSoft,
    marginHorizontal: 24,
    marginTop: 14,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 11,
  },
  custDot: {
    width: 28,
    height: 28,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  custInitial: { color: C.surface, fontWeight: "700", fontSize: 12 },
  custName: { flex: 1, fontSize: 14, fontWeight: "600", color: C.ink },

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
  // (The old cart-row styles lived here; the cart now uses the crow/qty/
  //  totals set defined at the top of this sheet.)

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
    // Four across — 0-9, a decimal point and backspace fit one clean block.
    flexGrow: 1,
    flexBasis: "22%",
    height: 56,
    backgroundColor: C.sunk,
    borderRadius: R.control,
    alignItems: "center",
    justifyContent: "center",
  },
  padText: { fontSize: 22, fontWeight: "600", color: C.ink },

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
    maxWidth: 520,
    backgroundColor: C.surface,
    borderRadius: R.modal,
    padding: 36,
  },
  // Green means money that actually landed — it appears here and nowhere
  // earlier in the flow, so the tick is unambiguous.
  successIcon: {
    width: 72,
    height: 72,
    borderRadius: 36,
    backgroundColor: C.okTint,
    alignItems: "center",
    justifyContent: "center",
  },
  successTitle: {
    fontSize: 30,
    fontWeight: "800",
    color: C.ink,
    letterSpacing: -1,
    marginTop: 24,
  },
  successOrder: { fontSize: 15.5, color: C.ink2Soft, marginTop: 8 },
  successTotal: {
    flexDirection: "row",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: 12,
    marginVertical: 24,
    paddingVertical: 20,
    borderTopWidth: 1,
    borderTopColor: C.line2,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  successTotalLbl: {
    ...MONO,
    fontSize: 10,
    letterSpacing: 1.3,
    color: C.ink2Soft,
  },
  successTotalVal: {
    fontSize: 34,
    fontWeight: "800",
    color: C.brand,
    letterSpacing: -1.2,
    flexShrink: 1,
  },
  successRow: {
    width: "100%",
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 7,
  },
  successLabel: { fontSize: 14.5, color: C.ink2Soft },
  successVal: { fontSize: 14.5, color: C.ink, fontWeight: "600" },

  // Print status pill shown inside SuccessModal — tells the cashier
  // whether the local printer actually printed the receipt or whether
  // it got queued for retry because the printer was offline.
  printPill: {
    width: "100%",
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
    paddingVertical: 14,
    paddingHorizontal: 17,
    borderRadius: R.control,
    marginTop: 16,
  },
  printPillNeutral: { backgroundColor: C.sunk },
  printPillOk: { backgroundColor: C.okTint },
  printPillWarn: { backgroundColor: C.warnTint },
  printPillText: { fontSize: 14.5, color: C.ink2, fontWeight: "700" },
  printPillSub: { fontSize: 11, color: C.warnDark, marginTop: 2 },

  // Selling gate (no open shift)
  shiftGate: {
    ...StyleSheet.absoluteFill,
    backgroundColor: "rgba(245,247,250,0.94)",
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  shiftGateText: {
    fontSize: 21,
    color: C.ink,
    fontWeight: "800",
    letterSpacing: -0.5,
    textAlign: "center",
  },
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
