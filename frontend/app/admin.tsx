import { useEffect, useMemo, useState, useCallback, useRef } from "react";
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
  useWindowDimensions,
  Platform,
  Switch,
  Alert,
  Linking,
  Share,
} from "react-native";
import qrcode from "qrcode-generator";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { useRouter, useLocalSearchParams } from "expo-router";
import { Ionicons, MaterialCommunityIcons } from "@expo/vector-icons";
import * as ImagePicker from "expo-image-picker";
import * as ImageManipulator from "expo-image-manipulator";
import PhoneInput from "../components/PhoneInput";
import {
  discoverPrinters as starDiscover,
  testPrint as starTestPrint,
  type DiscoveredPrinter,
  type PrinterConfig,
  type ReceiptOrder,
} from "../lib/starPrinter";
import { useStarPrinter } from "../lib/useStarPrinter";
import { useShiftSummaryPrint } from "../lib/useShiftSummaryPrint";
import { usePrinterStatus } from "../lib/usePrinterStatus";
import { SidebarDrawer } from "../components/SidebarDrawer";
import {
  loadLocalPrinterConfig,
  saveLocalPrinterConfig,
} from "../lib/localPrinterConfig";
import * as printerQueue from "../lib/printerQueue";
import { apiFetch, clearAuthToken } from "../lib/api";
import AsyncStorage from "@react-native-async-storage/async-storage";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
// Server-rendered backoffice (Django) lives under /backoffice/ on the same host.
const BACKOFFICE_URL = `${process.env.EXPO_PUBLIC_BACKEND_URL}/backoffice/`;
// The customer self-ordering site is served at the host root: /order/<branchId>/.
const SELF_ORDER_BASE = `${process.env.EXPO_PUBLIC_BACKEND_URL}/order`;
const AUTH_KEY = "bravepos:auth:v1";

// Render a string as a QR-code PNG data URI for <Image>. Mirrors pos.tsx's
// helper (error-correction "M", auto type/size).
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

async function doLogout(): Promise<void> {
  try {
    await apiFetch("/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch {}
  clearAuthToken();
  try { await AsyncStorage.removeItem(AUTH_KEY); } catch {}
}
const THB = (n: number) => `฿${(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// Mask prefix used by the backend to redact stored Beam API keys (••••<last4>).
// Kept in sync with backend/server.py BEAM_API_KEY_MASK_PREFIX.
const BEAM_API_KEY_MASK_PREFIX = "••••";

type Section = "transactions" | "reports" | "inventory" | "customers" | "products" | "drawer" | "settings";

type Category = { id: string; name: string; name_th?: string; color: string; source?: string; order: number };
type Product = {
  id: string; name: string; name_th?: string; price: number; cost: number;
  category_id: string; image_url: string; image_base64?: string; is_favorite: boolean;
  stock: number; tax_type: string; product_type: string; barcode?: string;
};
type StockDocItem = {
  product_id?: string | null; barcode: string; product_name: string;
  qty: number; price: number; discount: number; total: number;
};
type StockDoc = {
  id: string; type: "in" | "out" | "adjust" | "check"; document_no: string;
  document_name: string; adjust_type: string; ref_no: string;
  vendor: string; receiver: string; note: string;
  subtotal: number; discount: number; tax: number; total: number;
  created_by: string; created_at: string; items: StockDocItem[];
};
type ChannelRow = {
  channel: string; source: string; count: number;
  before_gp: number; gp: number; after_gp: number; has_gp: boolean;
};
type Customer = { id: string; name: string; phone?: string; last_visit?: string; color: string };
type CustomerStats = {
  success_total: number;
  bill_count: number;
  avg_bill: number;
  outstanding_total: number;
  outstanding_count: number;
  top_products: { product_id: string; name: string; total: number; qty: number }[];
  top_categories: { name: string; total: number }[];
};
type Order = {
  id: string; order_number: string; items: any[]; total: number;
  status: string; source: string; created_time: string; created_at: string;
  payment_method?: string; delivery_provider?: string; delivery_status?: string;
  staff?: string; voided_by?: string; voided_at?: string | null;
  subtotal?: number; paid_amount?: number; change?: number;
  discount_amount?: number; branch_name?: string;
};
type Dashboard = {
  total_sales: number; cost: number; profit: number; gp_percent: number;
  tx_count: number; avg_bill: number;
  timeline: { label: string; value: number }[];
  top_products: { product_id: string; name: string; total: number; qty: number }[];
  top_categories: { name: string; total: number }[];
};
type Settings = {
  shop_name: string; business_type: string; tax_id?: string;
  pos_id: string; branch: string; pos_number: string;
  open_time: string; close_time: string;
  tax_percent: number; tax_mode: string;
  service_charge_enabled: boolean; service_charge_percent: number;
  beam_merchant_id?: string; beam_api_key?: string; beam_sandbox?: boolean;
  beam_card_fee_percent?: number;
  omise_public_key?: string; omise_secret_key?: string; omise_fee_percent?: number;
  printer_enabled?: boolean;
  printer_transport?: "disabled" | "file" | "network";
  printer_address?: string | null;
  printer_paper_width?: number;
};
type PrinterStatus = {
  connected: boolean;
  status: "connected" | "offline" | "disabled";
  enabled: boolean;
  transport: string;
  address?: string | null;
  paper_width: number;
  error?: string;
};

export default function Admin() {
  const router = useRouter();
  // Auth state comes from AsyncStorage (the session), not URL params — those
  // would otherwise let a manual URL edit show a stale or wrong role/branch.
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
  // When the Shop screen's hamburger pushes us here, it passes
  // `sidebar=open` so the user sees the full menu immediately instead of
  // landing on Reports.  Only meaningful on narrow screens (the wide
  // layout shows the sidebar permanently on the left).
  const navParams = useLocalSearchParams<{ sidebar?: string; section?: string }>();
  // Initial section comes from the ?section= query param (set by the
  // Shop sidebar when a user picks Reports/Inventory/Settings/etc.).
  // Whitelisted to known Section keys so a malformed URL can't put us
  // in an unrenderable state — default Reports if not specified.
  const VALID_SECTIONS: Section[] = [
    "transactions", "reports", "inventory", "customers",
    "products", "drawer", "settings",
  ];
  const initialSection = (VALID_SECTIONS.includes(navParams.section as Section)
    ? (navParams.section as Section)
    : "reports") as Section;
  const [section, setSection] = useState<Section>(initialSection);
  // Sidebar=open is now unused (Shop opens its own drawer instead of
  // navigating with the flag) but we keep the param handling for any
  // links that still pass it.
  const [sidebarOpen, setSidebarOpen] = useState(
    navParams.sidebar === "open" && width < 720,
  );

  // Receipt-printing context for the Admin screen.  The same hook also
  // runs on /pos — both instances share the on-disk print queue, so a
  // failed print from POS gets auto-retried even if the cashier is
  // sitting on Admin/Transactions when the printer comes back online.
  // reprint() is exposed to Transactions for the per-row Reprint button.
  const { reprint: reprintReceipt, ReceiptOverlay: PrinterOverlay } = useStarPrinter();

  const allItems: { key: Section | "shop"; label: string; icon: any; adminOnly?: boolean }[] = [
    { key: "shop", label: "Shop", icon: "home-outline" },
    { key: "transactions", label: "Transactions", icon: "swap-horizontal-outline" },
    { key: "reports", label: "Reports", icon: "pie-chart-outline" },
    { key: "inventory", label: "Inventory", icon: "cube-outline" },
    { key: "customers", label: "Customers", icon: "people-outline" },
    { key: "products", label: "Products", icon: "gift-outline" },
    { key: "drawer", label: "Drawer", icon: "calculator-outline" },
    { key: "settings", label: "Settings", icon: "settings-outline" },
  ];
  const items = allItems.filter((it) => !it.adminOnly || isAdmin);

  const navigate = (k: Section | "shop") => {
    setSidebarOpen(false);
    if (k === "shop") {
      // Prefer going back through the nav stack so we restore the Shop
      // screen the user came from (preserves any state).  Only fall back
      // to replace if there's no stack to pop (e.g. admin was opened via
      // deep link or as the initial route after login).
      if (router.canGoBack()) {
        router.back();
      } else {
        router.replace({
          pathname: "/pos",
          params: {
            staff: staff || "Admin",
            role: role || "",
            branch_id: activeBranchId,
            branch_name: activeBranchName,
          },
        });
      }
    } else {
      setSection(k);
    }
  };

  const insets = useSafeAreaInsets();

  // Render-function version of the sidebar so we can pass `extraStyle`
  // (inset padding) when it's rendered inside the modal on narrow screens.
  // Sharing the same JSX between desktop + modal previously required a
  // SafeAreaView wrapper that broke height inheritance and hid the items.
  const renderSidebar = (extraStyle?: any) => (
    <View style={[styles.sidebar, extraStyle]} testID="admin-sidebar">
      <View style={styles.avatarBox}>
        <View style={styles.avatarCircle}>
          <Ionicons name="person" size={32} color="#475569" />
        </View>
        <Text style={styles.avatarText}>{staff || "Admin"}</Text>
        {!!activeBranchName && (
          <View style={styles.sideBranchChip} testID="admin-branch-chip">
            <Ionicons name="storefront-outline" size={12} color="#00B14F" />
            <Text style={styles.sideBranchChipText} numberOfLines={1}>{activeBranchName}</Text>
          </View>
        )}
      </View>
      {/* Scrollable so the lower items (Settings / Branches) and the logout
          footer never get clipped when the sidebar is taller than the screen
          (landscape, short devices, etc.). */}
      <ScrollView style={{ flex: 1 }} contentContainerStyle={{ paddingBottom: 4 }}>
        {items.map((it) => (
          <TouchableOpacity
            key={it.key}
            style={[styles.sideItem, section === it.key && styles.sideItemActive]}
            onPress={() => navigate(it.key)}
            testID={`side-${it.key}`}
          >
            <Ionicons
              name={it.icon}
              size={20}
              color={section === it.key ? "#00B14F" : "#475569"}
            />
            <Text
              style={[styles.sideLabel, section === it.key && styles.sideLabelActive]}
            >
              {it.label}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>
      <TouchableOpacity
        style={styles.logoutSide}
        onPress={async () => { await doLogout(); router.replace("/"); }}
        testID="admin-logout"
      >
        <Ionicons name="log-out-outline" size={18} color="#EF4444" />
        <Text style={styles.logoutSideText}>Log out</Text>
      </TouchableOpacity>
      <View style={styles.sideFooter}>
        <Ionicons name="refresh-circle-outline" size={14} color="#94A3B8" />
        <Text style={styles.sideFooterDate}>
          {new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })}
          {" "}
          {new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}
        </Text>
      </View>
      <Text style={styles.versionText}>
        Version {require("expo-constants").default.expoConfig?.version || "?"}
      </Text>
    </View>
  );

  // Backwards-compat alias so JSX below can stay readable.
  const Sidebar = renderSidebar();

  if (!authLoaded) {
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
      <View style={[styles.rootRow, !isWide && { flexDirection: "column" }]}>
        {isWide ? (
          Sidebar
        ) : (
          <>
            <View style={styles.mobileTop}>
              <TouchableOpacity onPress={() => setSidebarOpen(true)} testID="open-sidebar">
                <Ionicons name="menu" size={26} color="#0F172A" />
              </TouchableOpacity>
              <Text style={styles.mobileTitle}>
                {items.find((i) => i.key === section)?.label}
              </Text>
              <TouchableOpacity onPress={async () => { await doLogout(); router.replace("/"); }}>
                <Ionicons name="log-out-outline" size={22} color="#EF4444" />
              </TouchableOpacity>
            </View>
            {/* Use the SHARED SidebarDrawer so the menu looks and behaves
                identically to the one Shop opens.  Replaces the previous
                inline Modal that wrapped renderSidebar. */}
            <SidebarDrawer
              visible={sidebarOpen}
              onClose={() => setSidebarOpen(false)}
              staff={staff || "Admin"}
              role={role || ""}
              branchName={activeBranchName || undefined}
              activeKey={section}
              onNavigate={(key) => {
                setSidebarOpen(false);
                navigate(key as Section | "shop");
              }}
              onLogout={async () => {
                setSidebarOpen(false);
                await doLogout();
                router.replace("/");
              }}
            />
            {/* Old inline modal (replaced by SidebarDrawer above) — kept
                below as a no-op to preserve the rest of the JSX structure. */}
            <Modal
              visible={false}
              animationType="slide"
              transparent
              onRequestClose={() => setSidebarOpen(false)}
            >
              <View style={styles.mobileSidebarOverlay}>
                {renderSidebar({
                  paddingTop: 20 + insets.top,
                  paddingBottom: 20 + insets.bottom,
                })}
                <TouchableOpacity
                  style={{ flex: 1 }}
                  onPress={() => setSidebarOpen(false)}
                />
              </View>
            </Modal>
          </>
        )}
        <View style={styles.content}>
          {section === "reports" && <Reports isWide={isWide} />}
          {section === "transactions" && <Transactions isWide={isWide} reprint={reprintReceipt} staff={staff || "Admin"} />}
          {section === "inventory" && <Inventory isWide={isWide} />}
          {section === "customers" && <Customers isWide={isWide} />}
          {section === "products" && <Products isWide={isWide} isAdmin={isAdmin} />}
          {section === "drawer" && <Drawer isWide={isWide} staff={staff || "Admin"} />}
          {section === "settings" && (
            <SettingsView
              isWide={isWide}
              branchId={activeBranchId}
              branchName={activeBranchName}
            />
          )}
        </View>
      </View>
      {/* Off-screen receipt render target — view-shot captures this. */}
      <PrinterOverlay />
    </SafeAreaView>
  );
}

// =================== REPORTS / DASHBOARD ===================
function Reports({ isWide }: { isWide: boolean }) {
  const [period, setPeriod] = useState("month");
  const [range, setRange] = useState<DateRange>({ start: "", end: "" });
  const [showRange, setShowRange] = useState(false);
  const [data, setData] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [showChannels, setShowChannels] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch(`${API}/dashboard?${periodQuery(period, range)}`);
      setData(await res.json());
    } catch {}
    setLoading(false);
  }, [period, range]);
  useEffect(() => { load(); }, [load]);

  const maxBar = Math.max(1, ...(data?.timeline || []).map((t) => t.value));
  const topProdTotal = (data?.top_products || []).reduce((s, p) => s + p.total, 0) || 1;
  const topCatTotal = (data?.top_categories || []).reduce((s, c) => s + c.total, 0) || 1;
  const palette = ["#00B14F", "#EF4444", "#F59E0B", "#3B82F6", "#8B5CF6"];

  return (
    <ScrollView style={{ flex: 1 }} contentContainerStyle={{ padding: 16 }} testID="reports-section">
      <View style={styles.reportsHeader}>
        <Text style={styles.h1}>Sales Dashboard</Text>
        <TouchableOpacity
          style={styles.reportsBtn}
          onPress={() => setShowChannels(true)}
          testID="open-channel-report"
        >
          <Ionicons name="bar-chart-outline" size={16} color="#00B14F" />
          <Text style={styles.reportsBtnText}>Reports</Text>
        </TouchableOpacity>
      </View>

      <ChannelReportModal
        visible={showChannels}
        period={period}
        range={range}
        onClose={() => setShowChannels(false)}
      />

      <DateRangeModal
        visible={showRange}
        initial={range}
        onClose={() => setShowRange(false)}
        onApply={(r) => { setRange(r); setPeriod("custom"); setShowRange(false); }}
      />

      <View style={styles.periodRow}>
        {PERIODS.map((p) => (
          <TouchableOpacity
            key={p.k}
            style={[styles.periodBtn, period === p.k && styles.periodBtnActive]}
            onPress={() => (p.k === "custom" ? setShowRange(true) : setPeriod(p.k))}
            testID={`period-${p.k}`}
          >
            <Text style={[styles.periodText, period === p.k && styles.periodTextActive]}>
              {p.k === "custom" && period === "custom" && range.start ? rangeLabel(range) : p.l}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {loading || !data ? (
        <ActivityIndicator color="#00B14F" style={{ marginTop: 40 }} />
      ) : (
        <>
          <View style={styles.kpiRow}>
            <KPI label="Sales" value={THB(data.total_sales)} color="#00B14F" icon="cash-outline" />
            <KPI label="Profit" value={THB(data.profit)} color="#3B82F6" icon="trending-up" />
            <KPI label="Transactions" value={String(data.tx_count ?? 0)} color="#F59E0B" icon="receipt-outline" />
            <KPI label="Avg/bill" value={THB(data.avg_bill)} color="#8B5CF6" icon="stats-chart" />
          </View>

          <View style={styles.gpRow}>
            <GPStat label="Before GP" value={THB(data.total_sales)} />
            <GPStat label="Cost" value={THB(data.cost)} />
            <GPStat label="GP %" value={`${(data.gp_percent ?? 0).toFixed(1)}%`} accent />
            <GPStat label="Profit" value={THB(data.profit)} accent />
          </View>

          <View style={styles.chartsRow}>
            <View style={styles.chartCard} testID="sales-chart">
              <Text style={styles.chartTitle}>Sales Trend</Text>
              <View style={styles.chart}>
                {(data.timeline ?? []).length === 0 ? (
                  <Text style={styles.emptyChart}>No data for this period</Text>
                ) : (
                  (data.timeline ?? []).map((t, i) => (
                    <View key={i} style={styles.barCol}>
                      <View
                        style={[
                          styles.bar,
                          { height: Math.max(4, (t.value / maxBar) * 140) },
                        ]}
                      />
                      <Text style={styles.barLabel}>{t.label.slice(5)}</Text>
                    </View>
                  ))
                )}
              </View>
            </View>

            <View style={styles.chartCard} testID="top-products">
              <Text style={styles.chartTitle}>Top 5 Products</Text>
              {(data.top_products ?? []).length === 0 ? (
                <Text style={styles.emptyChart}>No sales yet</Text>
              ) : (
                (data.top_products ?? []).map((p, i) => (
                  <View key={p.product_id} style={styles.rankRow}>
                    <View style={[styles.rankDot, { backgroundColor: palette[i] }]} />
                    <Text style={styles.rankName} numberOfLines={1}>{p.name}</Text>
                    <View style={styles.rankBarBg}>
                      <View
                        style={[styles.rankBar, {
                          width: `${(p.total / topProdTotal) * 100}%`,
                          backgroundColor: palette[i],
                        }]}
                      />
                    </View>
                    <Text style={styles.rankVal}>{THB(p.total)}</Text>
                  </View>
                ))
              )}
            </View>

            <View style={styles.chartCard} testID="top-categories">
              <Text style={styles.chartTitle}>Top 5 Categories</Text>
              {(data.top_categories ?? []).length === 0 ? (
                <Text style={styles.emptyChart}>No sales yet</Text>
              ) : (
                (data.top_categories ?? []).map((c, i) => (
                  <View key={c.name} style={styles.rankRow}>
                    <View style={[styles.rankDot, { backgroundColor: palette[i] }]} />
                    <Text style={styles.rankName} numberOfLines={1}>{c.name}</Text>
                    <View style={styles.rankBarBg}>
                      <View
                        style={[styles.rankBar, {
                          width: `${(c.total / topCatTotal) * 100}%`,
                          backgroundColor: palette[i],
                        }]}
                      />
                    </View>
                    <Text style={styles.rankVal}>{THB(c.total)}</Text>
                  </View>
                ))
              )}
            </View>
          </View>
        </>
      )}

      <TouchableOpacity
        style={styles.backofficeBtn}
        onPress={() => Linking.openURL(BACKOFFICE_URL)}
        testID="open-backoffice"
      >
        <Ionicons name="desktop-outline" size={18} color="#00B14F" />
        <Text style={styles.backofficeBtnText}>Back office</Text>
      </TouchableOpacity>
    </ScrollView>
  );
}

function KPI({ label, value, color, icon }: { label: string; value: string; color: string; icon: any }) {
  return (
    <View style={[styles.kpiCard, { borderTopColor: color }]}>
      <View style={styles.kpiHead}>
        <Ionicons name={icon} size={18} color={color} />
        <Text style={styles.kpiLabel}>{label}</Text>
      </View>
      <Text style={styles.kpiValue}>{value}</Text>
    </View>
  );
}

function GPStat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <View style={[styles.gpStat, accent && { backgroundColor: "#E5F7ED" }]}>
      <Text style={styles.gpLabel}>{label}</Text>
      <Text style={[styles.gpValue, accent && { color: "#00B14F" }]}>{value}</Text>
    </View>
  );
}

// ── Shared period filter helpers (dashboard + channel report) ──
type DateRange = { start: string; end: string };
const PERIODS = [
  { k: "today", l: "Today" },
  { k: "week", l: "This week" },
  { k: "month", l: "This month" },
  { k: "year", l: "This Year" },
  { k: "custom", l: "Custom" },
] as const;
const PERIOD_LABELS: Record<string, string> = {
  today: "Today", week: "This week", month: "This month", year: "This Year", custom: "Custom",
};

function periodQuery(period: string, range: DateRange): string {
  if (period === "custom" && range.start) {
    return `period=custom&start=${range.start}&end=${range.end || range.start}`;
  }
  return `period=${period}`;
}
function rangeLabel(range: DateRange): string {
  if (!range.start) return "Custom";
  return range.end && range.end !== range.start ? `${range.start} → ${range.end}` : range.start;
}
const fmtISO = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

// In-app month calendar with range highlighting (no native picker dependency).
function Calendar({ start, end, onPick }: { start: string; end: string; onPick: (iso: string) => void }) {
  const base = start ? new Date(start + "T00:00:00") : new Date();
  const [view, setView] = useState(new Date(base.getFullYear(), base.getMonth(), 1));
  const year = view.getFullYear();
  const month = view.getMonth();
  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const cells: (number | null)[] = [
    ...Array(firstDow).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  while (cells.length % 7 !== 0) cells.push(null);
  const monthLabel = view.toLocaleDateString("en-US", { month: "long", year: "numeric" });
  const todayISO = fmtISO(new Date());
  const shift = (delta: number) => setView(new Date(year, month + delta, 1));

  return (
    <View>
      <View style={styles.calHeader}>
        <TouchableOpacity onPress={() => shift(-1)} style={styles.calNavBtn} testID="cal-prev">
          <Ionicons name="chevron-back" size={20} color="#0F172A" />
        </TouchableOpacity>
        <Text style={styles.calMonth}>{monthLabel}</Text>
        <TouchableOpacity onPress={() => shift(1)} style={styles.calNavBtn} testID="cal-next">
          <Ionicons name="chevron-forward" size={20} color="#0F172A" />
        </TouchableOpacity>
      </View>
      <View style={styles.calWeekRow}>
        {WEEKDAYS.map((w) => <Text key={w} style={styles.calWeekday}>{w}</Text>)}
      </View>
      <View style={styles.calGrid}>
        {cells.map((d, i) => {
          if (d === null) return <View key={i} style={styles.calCell} />;
          const iso = fmtISO(new Date(year, month, d));
          const isStart = iso === start;
          const isEnd = iso === end;
          const isEdge = isStart || isEnd;
          const inRange = !!start && !!end && iso > start && iso < end;
          return (
            <TouchableOpacity
              key={i}
              style={[styles.calCell, inRange && styles.calCellInRange, isEdge && styles.calCellSel]}
              onPress={() => onPick(iso)}
              testID={`cal-day-${iso}`}
            >
              <Text style={[
                styles.calCellText,
                iso === todayISO && !isEdge && styles.calCellToday,
                isEdge && styles.calCellTextSel,
              ]}>{d}</Text>
            </TouchableOpacity>
          );
        })}
      </View>
    </View>
  );
}

// Custom date-range picker — tap a start day, then an end day (optional).
function DateRangeModal({
  visible, initial, onClose, onApply,
}: { visible: boolean; initial: DateRange; onClose: () => void; onApply: (r: DateRange) => void }) {
  const [start, setStart] = useState(initial.start);
  const [end, setEnd] = useState(initial.end);
  useEffect(() => { if (visible) { setStart(initial.start); setEnd(initial.end); } }, [visible, initial]);

  const pick = (iso: string) => {
    if (!start || (start && end)) { setStart(iso); setEnd(""); }   // begin a fresh range
    else if (iso < start) { setStart(iso); }                        // earlier than start → new start
    else { setEnd(iso); }                                           // later → close the range
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.modalOverlay}>
        <View style={styles.rangeCard} testID="date-range-modal">
          <View style={styles.modalHead}>
            <TouchableOpacity onPress={onClose}><Ionicons name="close" size={22} color="#475569" /></TouchableOpacity>
            <Text style={styles.modalTitle}>Custom range</Text>
            <View style={{ width: 22 }} />
          </View>
          <View style={{ padding: 16, gap: 12 }}>
            <View style={styles.rangeSummary}>
              <View style={styles.rangeSummaryCol}>
                <Text style={styles.docFieldLabel}>Start</Text>
                <Text style={styles.rangeSummaryVal}>{start || "—"}</Text>
              </View>
              <Ionicons name="arrow-forward" size={16} color="#94A3B8" />
              <View style={styles.rangeSummaryCol}>
                <Text style={styles.docFieldLabel}>End</Text>
                <Text style={styles.rangeSummaryVal}>{end || (start ? "Same day" : "—")}</Text>
              </View>
            </View>
            <Calendar start={start} end={end} onPick={pick} />
            <TouchableOpacity
              style={[styles.primaryBtn, !start && { opacity: 0.5 }]}
              disabled={!start}
              onPress={() => onApply({ start, end })}
              testID="range-apply"
            >
              <Text style={styles.primaryBtnText}>Apply</Text>
            </TouchableOpacity>
          </View>
        </View>
      </View>
    </Modal>
  );
}

// Sales channel report — orders grouped by source (image 2).
function ChannelReportModal({
  visible, period, range, onClose,
}: { visible: boolean; period: string; range: DateRange; onClose: () => void }) {
  const [rows, setRows] = useState<ChannelRow[] | null>(null);
  const [totals, setTotals] = useState({ before: 0, gp: 0, after: 0, count: 0 });
  const [curPeriod, setCurPeriod] = useState(period);
  const [curRange, setCurRange] = useState<DateRange>(range);
  const [showRange, setShowRange] = useState(false);

  // Adopt the dashboard's current selection each time the report is opened.
  useEffect(() => { if (visible) { setCurPeriod(period); setCurRange(range); } }, [visible, period, range]);

  useEffect(() => {
    if (!visible) return;
    setRows(null);
    (async () => {
      try {
        const res = await apiFetch(`${API}/dashboard/channels?${periodQuery(curPeriod, curRange)}`);
        const d = await res.json();
        setRows(d.channels || []);
        setTotals({
          before: d.total_before_gp || 0, gp: d.total_gp || 0,
          after: d.total_after_gp || 0, count: d.total_count || 0,
        });
      } catch { setRows([]); }
    })();
  }, [visible, curPeriod, curRange]);

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <SafeAreaView style={styles.docScreen} testID="channel-report">
        <View style={styles.docTopBar}>
          <TouchableOpacity style={styles.docBackBtn} onPress={onClose}>
            <Ionicons name="chevron-back" size={22} color="#0F172A" />
            <Text style={styles.docBackText}>Back</Text>
          </TouchableOpacity>
          <Text style={styles.docTopTitle}>Sales channel report</Text>
          <View style={{ width: 70 }} />
        </View>

        {/* Working period filter */}
        <View style={styles.chPeriodRow}>
          {PERIODS.map((p) => (
            <TouchableOpacity
              key={p.k}
              style={[styles.periodBtn, curPeriod === p.k && styles.periodBtnActive]}
              onPress={() => (p.k === "custom" ? setShowRange(true) : setCurPeriod(p.k))}
              testID={`ch-period-${p.k}`}
            >
              <Text style={[styles.periodText, curPeriod === p.k && styles.periodTextActive]}>
                {p.k === "custom" && curPeriod === "custom" && curRange.start ? rangeLabel(curRange) : p.l}
              </Text>
            </TouchableOpacity>
          ))}
        </View>

        <DateRangeModal
          visible={showRange}
          initial={curRange}
          onClose={() => setShowRange(false)}
          onApply={(r) => { setCurRange(r); setCurPeriod("custom"); setShowRange(false); }}
        />

        <View style={styles.chTableHead}>
          <Text style={[styles.chHeadCell, { width: 30 }]}>#</Text>
          <Text style={[styles.chHeadCell, { flex: 1, textAlign: "left" }]}>Channel name</Text>
          <Text style={[styles.chHeadCell, { width: 70 }]}>Count</Text>
          <Text style={[styles.chHeadCell, { width: 110 }]}>Before GP</Text>
          <Text style={[styles.chHeadCell, { width: 90 }]}>GP</Text>
          <Text style={[styles.chHeadCell, { width: 110 }]}>After GP</Text>
        </View>

        {rows === null ? (
          <ActivityIndicator color="#00B14F" style={{ marginTop: 40 }} />
        ) : rows.length === 0 ? (
          <View style={styles.emptyBox}><Text style={styles.emptyText}>No sales</Text></View>
        ) : (
          <ScrollView>
            {rows.map((r, i) => (
              <View key={r.source + i} style={styles.chRow}>
                <Text style={[styles.chCell, { width: 30 }]}>{i + 1}</Text>
                <View style={{ flex: 1, flexDirection: "row", alignItems: "center", gap: 8 }}>
                  <View style={styles.chIcon}>
                    <Ionicons name="storefront" size={14} color="#00B14F" />
                  </View>
                  <Text style={styles.chName} numberOfLines={1}>{r.channel}</Text>
                  {!r.has_gp && (
                    <View style={styles.noGpBadge}><Text style={styles.noGpText}>No GP</Text></View>
                  )}
                </View>
                <Text style={[styles.chCell, { width: 70 }]}>{r.count}</Text>
                <Text style={[styles.chCell, { width: 110 }]}>{r.before_gp.toFixed(2)}</Text>
                <Text style={[styles.chCell, { width: 90 }]}>{r.gp.toFixed(2)}</Text>
                <Text style={[styles.chCell, { width: 110 }]}>{r.after_gp.toFixed(2)}</Text>
              </View>
            ))}
            <View style={[styles.chRow, { backgroundColor: "#F8FAFC" }]}>
              <Text style={[styles.chCell, { width: 30 }]} />
              <Text style={[styles.chCell, { flex: 1, textAlign: "left", fontWeight: "700" }]}>Total</Text>
              <Text style={[styles.chCell, { width: 70, fontWeight: "700" }]}>{totals.count}</Text>
              <Text style={[styles.chCell, { width: 110, fontWeight: "700" }]}>{totals.before.toFixed(2)}</Text>
              <Text style={[styles.chCell, { width: 90, fontWeight: "700" }]}>{totals.gp.toFixed(2)}</Text>
              <Text style={[styles.chCell, { width: 110, fontWeight: "700" }]}>{totals.after.toFixed(2)}</Text>
            </View>
          </ScrollView>
        )}
      </SafeAreaView>
    </Modal>
  );
}

// =================== TRANSACTIONS ===================
type ReprintFn = (
  order: ReceiptOrder,
  shop: any,
) => Promise<{ ok: true } | { ok: false; error: string }>;

type DateFilter = "today" | "yesterday" | "week" | "all";
type ProductRef = { image: string; barcode: string };

function Transactions({ isWide, reprint, staff }: { isWide: boolean; reprint: ReprintFn; staff: string }) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [selected, setSelected] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [showDetail, setShowDetail] = useState(false);
  const [query, setQuery] = useState("");
  const [dateFilter, setDateFilter] = useState<DateFilter>("all");
  // Order items only snapshot name/price/qty, so we join to the live
  // product catalogue (by product_id) to show the thumbnail + barcode on
  // each receipt line — matching the reference Sale Transactions screen.
  const [productMap, setProductMap] = useState<Record<string, ProductRef>>({});
  const [taxPercent, setTaxPercent] = useState(7);

  // Merge a server-updated order (e.g. after a void) back into the list
  // and the open detail pane so the "Voided" state shows without a reload.
  const handleVoided = useCallback((updated: Order) => {
    setOrders((prev) => prev.map((o) => (o.id === updated.id ? { ...o, ...updated } : o)));
    setSelected((cur) => (cur && cur.id === updated.id ? { ...cur, ...updated } : cur));
  }, []);

  useEffect(() => {
    (async () => {
      const [res, prodRes, setRes] = await Promise.all([
        apiFetch(`${API}/orders`),
        apiFetch(`${API}/products`).catch(() => null),
        apiFetch(`${API}/settings`).catch(() => null),
      ]);
      const o: Order[] = await res.json();
      setOrders(o);
      if (o[0] && isWide) setSelected(o[0]);
      if (prodRes?.ok) {
        const prods: any[] = await prodRes.json();
        const map: Record<string, ProductRef> = {};
        for (const p of prods) {
          map[p.id] = { image: p.image_base64 || p.image_url || "", barcode: p.barcode || p.sku || "" };
        }
        setProductMap(map);
      }
      if (setRes?.ok) {
        const s = await setRes.json();
        if (s?.tax_percent != null) setTaxPercent(Number(s.tax_percent) || 7);
      }
      setLoading(false);
    })();
  }, [isWide]);

  // Filter by order number (case-insensitive substring) and the chosen
  // date bucket.  Buckets are computed from the local-day boundary of the
  // *current* device so "Today" matches what the cashier sees on the wall
  // clock, not UTC.
  const filteredOrders = useMemo(() => {
    const q = query.trim().toLowerCase();
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const dayMs = 24 * 60 * 60 * 1000;

    return orders.filter((o) => {
      if (q && !o.order_number.toLowerCase().includes(q)) return false;
      if (dateFilter !== "all") {
        const t = new Date(o.created_at).getTime();
        if (dateFilter === "today" && t < startOfToday) return false;
        if (dateFilter === "yesterday" && (t < startOfToday - dayMs || t >= startOfToday)) return false;
        if (dateFilter === "week" && t < startOfToday - 6 * dayMs) return false;
      }
      return true;
    });
  }, [orders, query, dateFilter]);

  // If the currently-selected order falls out of the filter, drop the
  // selection so the right-hand detail pane doesn't show a row the user
  // can no longer see in the list.
  useEffect(() => {
    if (selected && !filteredOrders.some((o) => o.id === selected.id)) {
      setSelected(isWide ? (filteredOrders[0] ?? null) : null);
    }
  }, [filteredOrders, selected, isWide]);

  if (loading) return <ActivityIndicator color="#00B14F" style={{ marginTop: 40 }} />;

  // Mobile drill-down: show list OR detail
  if (!isWide && showDetail && selected) {
    return (
      <View style={{ flex: 1 }}>
        <TouchableOpacity style={styles.backRow} onPress={() => setShowDetail(false)}>
          <Ionicons name="chevron-back" size={22} color="#00B14F" />
          <Text style={styles.backText}>Back to transactions</Text>
        </TouchableOpacity>
        <TransactionDetail
          order={selected}
          reprint={reprint}
          staff={staff}
          onVoided={handleVoided}
          productMap={productMap}
          taxPercent={taxPercent}
        />
      </View>
    );
  }

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="transactions-section">
      <View style={[styles.txList, !isWide && styles.fullCol]}>
        <Text style={styles.sectionHeader}>Sale Transactions</Text>
        <View style={styles.searchBoxRow}>
          <Ionicons name="search" size={16} color="#94A3B8" />
          <TextInput
            placeholder="Search order #"
            style={styles.searchBoxInput}
            value={query}
            onChangeText={setQuery}
            placeholderTextColor="#94A3B8"
            autoCapitalize="characters"
            testID="tx-search"
          />
          {!!query && (
            <TouchableOpacity onPress={() => setQuery("")} testID="tx-search-clear">
              <Ionicons name="close-circle" size={16} color="#94A3B8" />
            </TouchableOpacity>
          )}
        </View>
        <View style={styles.txDateChips}>
          {([
            { key: "today", label: "Today" },
            { key: "yesterday", label: "Yesterday" },
            { key: "week", label: "Last 7 days" },
            { key: "all", label: "All" },
          ] as { key: DateFilter; label: string }[]).map((opt) => {
            const active = dateFilter === opt.key;
            return (
              <TouchableOpacity
                key={opt.key}
                style={[styles.txDateChip, active && styles.txDateChipActive]}
                onPress={() => setDateFilter(opt.key)}
                testID={`tx-date-${opt.key}`}
              >
                <Text
                  style={[styles.txDateChipText, active && { color: "#FFF" }]}
                  numberOfLines={1}
                >
                  {opt.label}
                </Text>
              </TouchableOpacity>
            );
          })}
        </View>
        {filteredOrders.length === 0 ? (
          <View style={styles.txEmpty}>
            <Text style={styles.emptyText}>No matching transactions</Text>
          </View>
        ) : (
          <FlatList
            data={filteredOrders}
            keyExtractor={(i) => i.id}
            ItemSeparatorComponent={() => <View style={styles.divider} />}
            renderItem={({ item }) => {
              const voided = item.status === "cancel";
              return (
              <TouchableOpacity
                style={[
                  styles.txRow,
                  selected?.id === item.id && isWide && styles.txRowActive,
                ]}
                onPress={() => { setSelected(item); if (!isWide) setShowDetail(true); }}
                testID={`tx-${item.order_number}`}
              >
                <Ionicons name="folder-outline" size={18} color={voided ? "#DC2626" : "#94A3B8"} />
                <View style={{ flex: 1 }}>
                  <Text style={[styles.txNum, voided && styles.txVoided]}>{item.order_number}</Text>
                  <Text style={styles.txTime}>
                    {item.created_time}
                    {voided && <Text style={styles.txVoided}>   Voided</Text>}
                  </Text>
                </View>
                <Text style={[styles.txAmount, voided && styles.txVoided]}>{THB(item.total)}</Text>
              </TouchableOpacity>
              );
            }}
          />
        )}
      </View>
      {isWide && (
        <View style={styles.txDetail}>
          {!selected ? (
            <View style={styles.emptyBox}>
              <Text style={styles.emptyText}>Please select bill</Text>
            </View>
          ) : (
            <TransactionDetail
              order={selected}
              reprint={reprint}
              staff={staff}
              onVoided={handleVoided}
              productMap={productMap}
              taxPercent={taxPercent}
            />
          )}
        </View>
      )}
    </View>
  );
}

const RECEIPT_MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// "09 June 2569 15:18:47" — English month, Thai Buddhist year (+543), like
// the reference receipt header.
function formatThaiDateTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${pad(d.getDate())} ${RECEIPT_MONTHS[d.getMonth()]} ${d.getFullYear() + 543} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

function TransactionDetail({
  order,
  reprint,
  staff,
  onVoided,
  productMap,
  taxPercent,
}: {
  order: Order;
  reprint: ReprintFn;
  staff: string;
  onVoided: (updated: Order) => void;
  productMap: Record<string, ProductRef>;
  taxPercent: number;
}) {
  const [reprintBusy, setReprintBusy] = useState(false);
  const [voidBusy, setVoidBusy] = useState(false);

  const isVoided = order.status === "cancel";

  // Money breakdown.  Prices are VAT-inclusive (Thai retail), so ex-tax and
  // tax are derived from the gross total at the configured rate.
  const subtotal = Number(order.subtotal ?? order.total) || 0;
  const gross = Number(order.total) || 0;
  const rate = (Number(taxPercent) || 0) / 100;
  const exTax = rate > 0 ? gross / (1 + rate) : gross;
  const tax = gross - exTax;
  const paid = Number(order.paid_amount ?? order.total) || 0;
  const change = Number(order.change) || 0;

  // Snapshot this order into the printer's ReceiptOrder shape.  `voided`
  // stamps the VOIDED banner on the printed copy (and carries the name of
  // whoever voided it) so the cancelled-bill reprint is unmistakable.
  const toReceiptOrder = (voided: boolean, voidedBy?: string): ReceiptOrder => ({
    order_number: order.order_number,
    items: order.items.map((it: any) => ({ name: it.name, qty: it.qty, price: it.price })),
    subtotal: Number(order.subtotal) || 0,
    discount_amount: Number(order.discount_amount) || 0,
    total: order.total,
    payment_method: order.payment_method || undefined,
    paid_amount: paid,
    change,
    created_at_local: new Date(order.created_at).toLocaleString("en-GB"),
    staff: order.staff || "",
    voided,
    voided_by: voided ? voidedBy || order.voided_by || staff : undefined,
  });

  // Centralised so both Re-Print and the void auto-print surface their
  // result the same way — a failed print used to be swallowed silently.
  const sendPrint = async (receipt: ReceiptOrder): Promise<boolean> => {
    const shopRes = await apiFetch(`${API}/settings`);
    const shop = shopRes.ok ? await shopRes.json() : {};
    // Settings.branch is a shop-wide string default ("Main"); the
    // order itself carries the actual branch it was rung up at.
    // Override so a reprint shows the correct location.
    if (order.branch_name) shop.branch = order.branch_name;
    const r = await reprint(receipt, shop);
    if (!r.ok) {
      Alert.alert(
        "Print failed",
        `${r.error}\n\nCheck the printer under Settings → Local Printer (it must be enabled and connected on this device).`,
      );
    }
    return r.ok;
  };

  const onReprint = async () => {
    setReprintBusy(true);
    try {
      await sendPrint(toReceiptOrder(isVoided));
    } catch (e: any) {
      Alert.alert("Print failed", e?.message || String(e));
    } finally {
      setReprintBusy(false);
    }
  };

  // Void = mark the bill cancelled on the server, then auto-print a void
  // copy (matches the reference POS, where confirming a void immediately
  // prints).  The list/detail update optimistically via onVoided().
  const doVoid = async () => {
    setVoidBusy(true);
    try {
      const res = await apiFetch(`${API}/orders/${order.id}/status`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "cancel" }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      const updated: Order = await res.json();
      onVoided(updated);
      // Auto-print the void receipt (fires the "Printing…" overlay).
      await sendPrint(toReceiptOrder(true, updated.voided_by));
    } catch (e: any) {
      Alert.alert("Void failed", e?.message || String(e));
    } finally {
      setVoidBusy(false);
    }
  };

  const onVoid = () => {
    Alert.alert(
      "Void bill",
      "Are you sure you want to void this bill? Can't undo this action.",
      [
        { text: "Close", style: "cancel" },
        { text: "Confirm", style: "destructive", onPress: doVoid },
      ],
    );
  };

  return (
    <View style={styles.txDetailWrap}>
      <ScrollView style={{ flex: 1 }} contentContainerStyle={styles.txDetailScroll}>
        {/* ── Header: order # + grand total ── */}
        <View style={styles.tdHeadRow}>
          <Text style={[styles.tdOrderNo, isVoided && styles.txVoided]}>{order.order_number}</Text>
          <Text style={[styles.tdGrand, isVoided && styles.txVoided]}>{THB(order.total)}</Text>
        </View>
        <Text style={styles.tdMeta}>{formatThaiDateTime(order.created_at)}</Text>
        <Text style={styles.tdMeta}>Cashier: {order.staff || "—"}</Text>
        {isVoided && (
          <Text style={styles.voidedBy}>Voided by: {order.voided_by || "—"}</Text>
        )}

        {/* ── Description ── */}
        <SectionLabel text="Description" />
        {order.items.map((it: any, i: number) => {
          const ref = it.product_id ? productMap[it.product_id] : undefined;
          const img = ref?.image;
          return (
            <View key={i} style={styles.tdItemRow}>
              {img ? (
                <Image source={{ uri: img }} style={styles.tdItemImg} />
              ) : (
                <View style={[styles.tdItemImg, styles.tdItemImgEmpty]}>
                  <Ionicons name="image-outline" size={18} color="#CBD5E1" />
                </View>
              )}
              <View style={styles.tdItemMid}>
                <Text style={styles.tdItemName}>{it.name}</Text>
                <Text style={styles.tdItemSub}>
                  {ref?.barcode ? `${ref.barcode}   ` : ""}
                  {THB(it.price)} x {it.qty}
                </Text>
              </View>
              <Text style={styles.tdItemTotal}>{THB((Number(it.price) || 0) * (Number(it.qty) || 0))}</Text>
            </View>
          );
        })}

        {/* ── Totals ── */}
        <View style={styles.tdTotalsBlock}>
          <TdLine label="Subtotal" value={THB(subtotal)} />
          <TdLine label="Subtotal (ex-Tax)" value={THB(exTax)} />
          <TdLine label={`Tax ${taxPercent} %`} value={THB(tax)} />
        </View>

        {/* ── Payment ── */}
        <SectionLabel text="Payment" />
        <TdLine label={order.payment_method || "Cash"} value={THB(paid)} />
        <TdLine label="Change" value={THB(change)} bold />

        {/* ── Sales channels ── */}
        <SectionLabel text="Sales channels" />
        <View style={styles.tdChannelRow}>
          <View style={styles.tdChannelBadge}>
            <Ionicons name="storefront" size={14} color="#00B14F" />
            <Text style={styles.tdChannelText}>{channelLabel(order.source)}</Text>
          </View>
        </View>
      </ScrollView>

      {/* ── Fixed action bar ── */}
      <View style={styles.tdActionBar}>
        <TouchableOpacity
          style={[styles.tdCancelBtn, (isVoided || voidBusy) && styles.tdCancelBtnDisabled]}
          onPress={onVoid}
          disabled={isVoided || voidBusy}
          testID={`void-${order.order_number}`}
        >
          <Text style={styles.tdActionText}>{voidBusy ? "Voiding…" : "Cancel Bill"}</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[styles.tdReprintBtn, reprintBusy && { opacity: 0.6 }]}
          onPress={onReprint}
          disabled={reprintBusy}
          testID={`reprint-${order.order_number}`}
        >
          <Text style={styles.tdActionText}>{reprintBusy ? "Printing…" : "Re-Print"}</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

// Centered section divider label: ───── Label ─────
function SectionLabel({ text }: { text: string }) {
  return (
    <View style={styles.tdSectionRow}>
      <View style={styles.tdSectionLine} />
      <Text style={styles.tdSectionText}>{text}</Text>
      <View style={styles.tdSectionLine} />
    </View>
  );
}

// One right-aligned label/value line in the totals + payment blocks.
function TdLine({ label, value, bold }: { label: string; value: string; bold?: boolean }) {
  return (
    <View style={styles.tdLineRow}>
      <Text style={[styles.tdLineLabel, bold && styles.tdLineBold]}>{label}</Text>
      <Text style={[styles.tdLineValue, bold && styles.tdLineBold]}>{value}</Text>
    </View>
  );
}

// Order.source → customer-facing sales-channel label.
function channelLabel(source: string): string {
  switch (source) {
    case "delivery": return "Delivery";
    case "kiosk": return "Kiosk";
    case "table":
    case "other":
    default: return "Store";
  }
}

// =================== INVENTORY ===================
const INV_TABS = [
  { k: "movement", l: "Stock Movement" },
  { k: "in", l: "Stock-In" },
  { k: "out", l: "Stock-Out" },
  { k: "adjust", l: "Adjust Stock" },
  { k: "check", l: "Check Stock" },
] as const;
type InvTab = (typeof INV_TABS)[number]["k"];

function Inventory({ isWide }: { isWide: boolean }) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("");
  const [tab, setTab] = useState<InvTab>("movement");
  const [stockModal, setStockModal] = useState<Product | null>(null);
  const [sortBy, setSortBy] = useState<"custom" | "name" | "inventory">("custom");
  const [search, setSearch] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    const [c, p] = await Promise.all([
      apiFetch(`${API}/categories`).then((r) => r.json()),
      apiFetch(`${API}/products`).then((r) => r.json()),
    ]);
    setCategories(c);
    setProducts(p);
    if (!activeCat && c.length) setActiveCat(c[1]?.id || c[0].id);
  };
  useEffect(() => { load(); }, []);

  const onRefresh = async () => {
    setRefreshing(true);
    try { await load(); } finally { setRefreshing(false); }
  };

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = products.filter((p) => p.category_id === activeCat);
    if (q) list = list.filter((p) => p.name.toLowerCase().includes(q) || (p.barcode || "").toLowerCase().includes(q));
    if (sortBy === "name") return [...list].sort((a, b) => a.name.localeCompare(b.name));
    if (sortBy === "inventory") return [...list].sort((a, b) => a.stock - b.stock);
    return list;
  }, [products, activeCat, sortBy, search]);
  const curCat = categories.find((c) => c.id === activeCat);

  const doMovement = async (type: "in" | "out" | "adjust", qty: number) => {
    if (!stockModal) return;
    await apiFetch(`${API}/stock-movements`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_id: stockModal.id, type, qty, note: `Admin ${type}` }),
    });
    setStockModal(null);
    load();
  };

  const isDocTab = tab !== "movement";

  return (
    <View style={{ flex: 1 }} testID="inventory-section">
      {/* Top tab bar (image 3/4) */}
      <View style={styles.invTabBar}>
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={{ alignItems: "center", paddingHorizontal: 12, gap: 6 }}
        >
          {INV_TABS.map((t) => (
            <TouchableOpacity
              key={t.k}
              style={[styles.invTopTab, tab === t.k && styles.invTopTabActive]}
              onPress={() => setTab(t.k)}
              testID={`inv-tab-${t.k}`}
            >
              <Text style={[styles.invTopTabText, tab === t.k && styles.invTopTabTextActive]}>
                {t.l}
              </Text>
            </TouchableOpacity>
          ))}
          <Ionicons name="chevron-forward" size={18} color="#94A3B8" style={{ marginLeft: 4 }} />
        </ScrollView>
      </View>

      {isDocTab ? (
        <StockDocuments type={tab as DocType} products={products} categories={categories} onChanged={load} />
      ) : (
        <View style={[styles.twoCol, !isWide && styles.stackedCol, { flex: 1 }]}>
          {isWide ? (
            <View style={styles.leftNav}>
              <View style={styles.invSearchRow}>
                <Ionicons name="search" size={16} color="#94A3B8" />
                <TextInput
                  style={styles.invSearchInput}
                  placeholder="Search"
                  placeholderTextColor="#94A3B8"
                  value={search}
                  onChangeText={setSearch}
                />
              </View>
              <ScrollView>
                {categories.map((c) => (
                  <TouchableOpacity
                    key={c.id}
                    style={[styles.leftNavRow, activeCat === c.id && styles.leftNavRowActive]}
                    onPress={() => setActiveCat(c.id)}
                    testID={`inv-cat-${c.id}`}
                  >
                    <Text style={styles.leftNavText} numberOfLines={1}>{c.name}</Text>
                    <Ionicons name="chevron-forward" size={14} color="#94A3B8" />
                  </TouchableOpacity>
                ))}
              </ScrollView>
            </View>
          ) : (
            <View style={styles.narrowCatBar}>
              <ScrollView
                horizontal
                showsHorizontalScrollIndicator={false}
                contentContainerStyle={{ paddingHorizontal: 12, paddingVertical: 8, gap: 8 }}
              >
                {categories.map((c) => {
                  const active = activeCat === c.id;
                  return (
                    <TouchableOpacity
                      key={c.id}
                      style={[styles.catChip, active && { backgroundColor: c.color, borderColor: c.color }]}
                      onPress={() => setActiveCat(c.id)}
                      testID={`inv-cat-${c.id}`}
                    >
                      <Text style={[styles.catChipText, active && { color: "#FFF" }]}>{c.name}</Text>
                    </TouchableOpacity>
                  );
                })}
              </ScrollView>
            </View>
          )}
          <View style={{ flex: 1 }}>
            <Text style={styles.sectionHeader}>
              {curCat?.name} ({filtered.length})
            </Text>
            <View style={styles.sortRow}>
              <Text style={styles.sortLabel}>Sort</Text>
              {(["custom", "name", "inventory"] as const).map((s) => (
                <TouchableOpacity
                  key={s}
                  style={[styles.sortTab, sortBy === s && styles.sortTabActive]}
                  onPress={() => setSortBy(s)}
                  testID={`inv-sort-${s}`}
                >
                  <Text style={[styles.sortTabText, sortBy === s && styles.sortTabTextActive]}>
                    {s === "custom" ? "Custom" : s === "name" ? "Name" : "Inventory"}
                  </Text>
                </TouchableOpacity>
              ))}
            </View>
            <FlatList
              data={filtered}
              keyExtractor={(i) => i.id}
              contentContainerStyle={{ padding: 14 }}
              refreshControl={
                <RefreshControl refreshing={refreshing} onRefresh={onRefresh} colors={["#00B14F"]} tintColor="#00B14F" />
              }
              renderItem={({ item }) => (
                <TouchableOpacity
                  style={styles.invRow}
                  onPress={() => setStockModal(item)}
                  testID={`inv-prod-${item.id}`}
                >
                  <Image source={{ uri: item.image_base64 || item.image_url }} style={styles.invImg} />
                  <View style={{ flex: 1 }}>
                    <Text style={styles.invName} numberOfLines={2}>{item.name}</Text>
                    <Text style={styles.invPrice}>{THB(item.price)}</Text>
                  </View>
                  <View style={styles.stockBox}>
                    {item.product_type === "BOM" ? (
                      <Text style={styles.nonStockText}>Non-stock product</Text>
                    ) : (
                      <>
                        <Text style={[styles.stockNum, item.stock <= 0 && { color: "#EF4444" }]}>
                          {item.stock}
                        </Text>
                        <Text style={[styles.stockStatus, item.stock <= 0 && { color: "#EF4444" }]}>
                          {item.stock <= 0 ? "Out of stock" : "In stock"}
                        </Text>
                      </>
                    )}
                  </View>
                  <Ionicons name="chevron-forward" size={16} color="#CBD5E1" />
                </TouchableOpacity>
              )}
              ListEmptyComponent={
                <View style={styles.emptyBox}>
                  <Text style={styles.emptyText}>No products</Text>
                </View>
              }
            />
          </View>
        </View>
      )}

      <StockMovementModal
        product={stockModal}
        defaultType={tab === "adjust" ? "adjust" : "in"}
        onClose={() => setStockModal(null)}
        onSave={doMovement}
      />
    </View>
  );
}

// ───── Stock document flows (Stock-In / Stock-Out / Adjust / Check) ─────
type DocType = "in" | "out" | "adjust" | "check";

const DOC_CONFIG: Record<DocType, {
  mode: "purchase" | "reconcile"; // purchase = in/out (qty·price); reconcile = adjust/check (count)
  title: string;            // "Create Stock-In Document"
  partyLabel?: string;      // Vendor / Receiver
  refLabel?: string;        // Purchasing Document Ref. / Ref Doc No.
  refCol?: string;          // list column header for the ref/name
  hasParty: boolean;        // show vendor/receiver select
  hasPrice: boolean;        // show price/discount/total columns
  hasName: boolean;         // adjust/check use a Document Name
  hasAdjustType: boolean;   // adjust shows A+/A- toggle
  hasAvgCost: boolean;      // stock-in only
  addBarLabel: string;      // green bar: "Items" / "Search Products"
  reasonLabel?: string;     // reconcile: "Reason"
  reconcileCols?: { before: string; input: string; result: string };
  mutates?: boolean;        // reconcile: adjust mutates stock, check does not
}> = {
  in: {
    mode: "purchase",
    title: "Create Stock-In Document", partyLabel: "Vendor",
    refLabel: "Purchasing Document Ref.", refCol: "Purchasing Document Ref.",
    hasParty: true, hasPrice: true, hasName: false, hasAdjustType: false, hasAvgCost: true,
    addBarLabel: "Items",
  },
  out: {
    mode: "purchase",
    title: "Create Stock-Out Document", partyLabel: "Receiver",
    refLabel: "Ref Doc No.", refCol: "Ref Doc No.",
    hasParty: true, hasPrice: true, hasName: false, hasAdjustType: false, hasAvgCost: false,
    addBarLabel: "Items",
  },
  adjust: {
    mode: "reconcile",
    title: "Create adjust stock document", refCol: "Document Name",
    hasParty: false, hasPrice: false, hasName: true, hasAdjustType: true, hasAvgCost: false,
    addBarLabel: "Search Products", reasonLabel: "Reason", mutates: true,
    reconcileCols: { before: "Before Adjust", input: "Qty Reconcile", result: "Update" },
  },
  check: {
    mode: "reconcile",
    title: "Create check stock document", refCol: "Document Name",
    hasParty: false, hasPrice: false, hasName: true, hasAdjustType: false, hasAvgCost: false,
    addBarLabel: "Search Products", reasonLabel: "Note", mutates: false,
    reconcileCols: { before: "Before Count", input: "Counted Qty", result: "Difference" },
  },
};

function thaiDate(d: Date): string {
  const months = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];
  return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear() + 543}`;
}

// Document list for a given type (images 4 / adjust / check).
function StockDocuments({
  type, products, categories, onChanged,
}: {
  type: DocType; products: Product[]; categories: Category[]; onChanged: () => void;
}) {
  const cfg = DOC_CONFIG[type];
  const [docs, setDocs] = useState<StockDoc[] | null>(null);
  const [creating, setCreating] = useState(false);

  const today = useMemo(() => new Date(), []);
  const weekAgo = useMemo(() => new Date(Date.now() - 7 * 86400000), []);
  const rangeLabel = `${thaiDate(weekAgo)} - ${thaiDate(today)}`;

  const load = useCallback(async () => {
    setDocs(null);
    try {
      const res = await apiFetch(`${API}/stock-documents?type=${type}`);
      setDocs(await res.json());
    } catch { setDocs([]); }
  }, [type]);
  useEffect(() => { load(); }, [load]);

  const total = (docs || []).reduce((s, d) => s + (d.total || 0), 0);

  return (
    <View style={{ flex: 1 }} testID={`stockdoc-${type}`}>
      <View style={styles.docListBar}>
        <View style={styles.docDateRange}>
          <Ionicons name="chevron-back" size={16} color="#CBD5E1" />
          <Text style={styles.docDateRangeText}>{rangeLabel}</Text>
          <Ionicons name="chevron-forward" size={16} color="#CBD5E1" />
        </View>
        {cfg.hasPrice && (
          <Text style={styles.docListTotal}>Total <Text style={{ fontWeight: "700", color: "#0F172A" }}>{total.toFixed(2)}</Text></Text>
        )}
        <TouchableOpacity style={styles.createDocBtn} onPress={() => setCreating(true)} testID="create-document">
          <Ionicons name="add" size={16} color="#00B14F" />
          <Text style={styles.createDocBtnText}>Create Document</Text>
        </TouchableOpacity>
      </View>

      {/* column headers */}
      <View style={styles.docColHead}>
        <Text style={[styles.docColCell, { width: 150 }]}>Date</Text>
        <Text style={[styles.docColCell, { width: 150 }]}>Document No.</Text>
        <Text style={[styles.docColCell, { flex: 1 }]}>{cfg.refCol}</Text>
        {cfg.hasPrice && <Text style={[styles.docColCell, { width: 90, textAlign: "right" }]}>Total</Text>}
        {cfg.hasAdjustType && <Text style={[styles.docColCell, { width: 110 }]}>Document Type</Text>}
        <Text style={[styles.docColCell, { width: 100, textAlign: "right" }]}>Created by</Text>
      </View>

      {docs === null ? (
        <ActivityIndicator color="#00B14F" style={{ marginTop: 40 }} />
      ) : docs.length === 0 ? (
        <View style={styles.emptyBox}><Text style={styles.emptyText}>No document</Text></View>
      ) : (
        <FlatList
          data={docs}
          keyExtractor={(d) => d.id}
          renderItem={({ item }) => {
            const dt = new Date(item.created_at);
            const refText = type === "in" || type === "out"
              ? item.ref_no
              : (item.document_name || item.note || "");
            return (
              <View style={styles.docRow} testID={`doc-${item.id}`}>
                <Text style={[styles.docCell, { width: 150 }]}>{thaiDate(dt)} {dt.toTimeString().slice(0, 5)}</Text>
                <Text style={[styles.docCell, { width: 150, color: "#0F172A" }]}>{item.document_no}</Text>
                <Text style={[styles.docCell, { flex: 1 }]} numberOfLines={1}>{refText}</Text>
                {cfg.hasPrice && <Text style={[styles.docCell, { width: 90, textAlign: "right" }]}>{(item.total || 0).toFixed(2)}</Text>}
                {cfg.hasAdjustType && <Text style={[styles.docCell, { width: 110 }]}>{item.adjust_type || ""}</Text>}
                <Text style={[styles.docCell, { width: 100, textAlign: "right" }]}>{item.created_by || ""}</Text>
              </View>
            );
          }}
        />
      )}

      <CreateStockDocModal
        visible={creating}
        type={type}
        products={products}
        categories={categories}
        onClose={() => setCreating(false)}
        onSaved={() => { setCreating(false); load(); onChanged(); }}
      />
    </View>
  );
}

type DraftField = "qty" | "price" | "discount" | "reconcile";
type DraftLine = {
  product_id: string; barcode: string; product_name: string;
  qty: string; price: string; discount: string;
  before: string; reconcile: string;
};

// Create-document form (image 5) + Select Products popup (images 6/7).
// Two layouts: "purchase" (in/out — qty·price) and "reconcile" (adjust/check
// — Before / counted / delta), driven by DOC_CONFIG[type].mode.
function CreateStockDocModal({
  visible, type, products, categories, onClose, onSaved,
}: {
  visible: boolean; type: DocType; products: Product[]; categories: Category[];
  onClose: () => void; onSaved: () => void;
}) {
  const cfg = DOC_CONFIG[type];
  const reconcile = cfg.mode === "reconcile";
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [ref, setRef] = useState("");
  const [docName, setDocName] = useState("");
  const [party, setParty] = useState("");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [taxIncluded, setTaxIncluded] = useState(false);
  const [avgCost, setAvgCost] = useState(false);
  const [picker, setPicker] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [keypad, setKeypad] = useState<{ idx: number; field: DraftField } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (visible) {
      setLines([]); setRef(""); setDocName(""); setParty(""); setNote(""); setReason("");
      setTaxIncluded(false); setAvgCost(false);
    }
  }, [visible]);

  const lineTotal = (l: DraftLine) =>
    Math.max(0, (parseFloat(l.qty) || 0) * (parseFloat(l.price) || 0) - (parseFloat(l.discount) || 0));
  const updateDelta = (l: DraftLine) => (parseFloat(l.reconcile) || 0) - (parseFloat(l.before) || 0);
  const subtotal = lines.reduce((s, l) => s + lineTotal(l), 0);
  const discountSum = lines.reduce((s, l) => s + (parseFloat(l.discount) || 0), 0);
  const tax = taxIncluded ? subtotal * 0.07 : 0;

  const addProducts = (picked: Product[]) => {
    setLines((prev) => {
      const existing = new Set(prev.map((l) => l.product_id));
      const fresh = picked
        .filter((p) => !existing.has(p.id))
        .map((p) => ({
          product_id: p.id, barcode: p.barcode || "", product_name: p.name,
          qty: "0", price: String(p.cost || 0), discount: "0",
          before: String(p.stock ?? 0), reconcile: "0",
        }));
      return [...prev, ...fresh];
    });
    setPicker(false);
  };

  // Import the product lines of previously-saved documents (reconcile flow).
  const importDocs = (docs: StockDoc[]) => {
    const picked = docs.flatMap((d) => d.items).map((it) => it.product_id).filter(Boolean) as string[];
    const toAdd = products.filter((p) => picked.includes(p.id));
    addProducts(toAdd);
    setImportOpen(false);
  };

  const setField = (idx: number, field: DraftField, val: string) =>
    setLines((prev) => prev.map((l, i) => (i === idx ? { ...l, [field]: val } : l)));

  const removeLine = (idx: number) => setLines((prev) => prev.filter((_, i) => i !== idx));

  const save = async () => {
    if (!lines.length || saving) return;
    setSaving(true);
    const body: any = { type };
    if (reconcile) {
      body.document_name = reason || docName;
      body.note = reason || note;
      body.items = lines.map((l) => ({
        product_id: l.product_id, barcode: l.barcode, product_name: l.product_name,
        qty: updateDelta(l),
        before_qty: parseFloat(l.before) || 0,
        reconcile_qty: parseFloat(l.reconcile) || 0,
      }));
    } else {
      body.ref_no = ref;
      body.note = note;
      body.tax_included = taxIncluded;
      body.avg_cost = avgCost;
      body.subtotal = subtotal; body.discount = discountSum; body.tax = tax; body.total = subtotal;
      body.items = lines.map((l) => ({
        product_id: l.product_id, barcode: l.barcode, product_name: l.product_name,
        qty: parseFloat(l.qty) || 0, price: parseFloat(l.price) || 0,
        discount: parseFloat(l.discount) || 0, total: lineTotal(l),
      }));
      if (type === "in") body.vendor = party;
      if (type === "out") body.receiver = party;
    }
    try {
      await apiFetch(`${API}/stock-documents`, { method: "POST", body: JSON.stringify(body) });
      onSaved();
    } catch {}
    setSaving(false);
  };

  const confirmSave = () => {
    if (!lines.length) return;
    if (Platform.OS === "web") { save(); return; }
    Alert.alert("Confirm Save Document", "Are you sure you want to save document.", [
      { text: "Cancel", style: "cancel" },
      { text: "Save", style: "destructive", onPress: save },
    ]);
  };

  const rc = cfg.reconcileCols;

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <SafeAreaView style={styles.docScreen}>
        <View style={styles.docTopBar}>
          <TouchableOpacity style={styles.docBackBtn} onPress={onClose}>
            <Ionicons name="chevron-back" size={22} color="#0F172A" />
            <Text style={styles.docBackText}>Back</Text>
          </TouchableOpacity>
          <Text style={styles.docTopTitle}>{cfg.title}</Text>
          <TouchableOpacity onPress={confirmSave} disabled={!lines.length}>
            <Text style={[styles.docSaveText, !lines.length && { color: "#CBD5E1" }]}>Save</Text>
          </TouchableOpacity>
        </View>

        <ScrollView keyboardShouldPersistTaps="handled">
          {/* ── header fields ── */}
          {reconcile ? (
            <View style={styles.docForm}>
              <View style={styles.docFormRow}>
                <TouchableOpacity style={[styles.docField, styles.importBtn]} onPress={() => setImportOpen(true)} testID="import-documents">
                  <Text style={styles.importBtnText}>Import Documents</Text>
                  <Ionicons name="chevron-forward" size={16} color="#94A3B8" />
                </TouchableOpacity>
                <View style={[styles.docField, { flex: 2 }]}>
                  <TextInput
                    style={styles.docInput}
                    value={reason}
                    onChangeText={setReason}
                    placeholder={cfg.reasonLabel}
                    placeholderTextColor="#94A3B8"
                    testID="reconcile-reason"
                  />
                </View>
              </View>
            </View>
          ) : (
            <View style={styles.docForm}>
              <View style={styles.docFormRow}>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>{type === "in" ? "Bill Date Ref." : "Date Ref."}</Text>
                  <Text style={styles.docFieldDate}>{thaiDate(new Date())}</Text>
                </View>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>{cfg.refLabel}</Text>
                  <TextInput style={styles.docInput} value={ref} onChangeText={setRef} placeholder="" />
                </View>
              </View>
              <View style={styles.docFormRow}>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>{cfg.partyLabel}</Text>
                  <TextInput style={styles.docInput} value={party} onChangeText={setParty} placeholder="" />
                </View>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>Note</Text>
                  <TextInput style={styles.docInput} value={note} onChangeText={setNote} placeholder="" />
                </View>
              </View>
            </View>
          )}

          {/* ── items table header ── */}
          <View style={styles.itemsHead}>
            <Text style={[styles.itemsHeadCell, { width: 30 }]}>#</Text>
            <Text style={[styles.itemsHeadCell, { width: 130 }]}>Barcode</Text>
            <Text style={[styles.itemsHeadCell, { flex: 1, textAlign: "left" }]}>Product Name</Text>
            {reconcile ? (
              <>
                <Text style={[styles.itemsHeadCell, { width: 90, textAlign: "right" }]}>{rc?.before}</Text>
                <Text style={[styles.itemsHeadCell, { width: 90 }]}>{rc?.input}</Text>
                <Text style={[styles.itemsHeadCell, { width: 80, textAlign: "right" }]}>{rc?.result}</Text>
              </>
            ) : (
              <>
                <Text style={[styles.itemsHeadCell, { width: 70 }]}>Quantity</Text>
                <Text style={[styles.itemsHeadCell, { width: 80 }]}>Price/Unit</Text>
                <Text style={[styles.itemsHeadCell, { width: 70 }]}>Discount</Text>
                <Text style={[styles.itemsHeadCell, { width: 80 }]}>Total</Text>
              </>
            )}
            <View style={{ width: 28 }} />
          </View>

          {lines.map((l, i) => {
            const d = updateDelta(l);
            return (
              <View key={l.product_id} style={styles.itemRow}>
                <Text style={[styles.itemCell, { width: 30 }]}>{i + 1}</Text>
                <Text style={[styles.itemCell, { width: 130, fontSize: 11 }]} numberOfLines={1}>{l.barcode}</Text>
                <Text style={[styles.itemCell, { flex: 1, textAlign: "left" }]} numberOfLines={1}>{l.product_name}</Text>
                {reconcile ? (
                  <>
                    <Text style={[styles.itemCellRO, { width: 90 }]}>{parseFloat(l.before) || 0}</Text>
                    <TouchableOpacity style={[styles.itemInput, { width: 90 }]} onPress={() => setKeypad({ idx: i, field: "reconcile" })} testID={`reconcile-${i}`}>
                      <Text style={styles.itemInputText}>{parseFloat(l.reconcile) || 0}</Text>
                    </TouchableOpacity>
                    <Text style={[styles.itemCellRO, { width: 80, textAlign: "right", color: d > 0 ? "#00B14F" : d < 0 ? "#EF4444" : "#64748B" }]}>
                      {d > 0 ? `+${d}` : `${d}`}
                    </Text>
                  </>
                ) : (
                  <>
                    <TouchableOpacity style={[styles.itemInput, { width: 70 }]} onPress={() => setKeypad({ idx: i, field: "qty" })}>
                      <Text style={styles.itemInputText}>{l.qty}</Text>
                    </TouchableOpacity>
                    <TouchableOpacity style={[styles.itemInput, { width: 80 }]} onPress={() => setKeypad({ idx: i, field: "price" })}>
                      <Text style={styles.itemInputText}>{(parseFloat(l.price) || 0).toFixed(2)}</Text>
                    </TouchableOpacity>
                    <TouchableOpacity style={[styles.itemInput, { width: 70 }]} onPress={() => setKeypad({ idx: i, field: "discount" })}>
                      <Text style={styles.itemInputText}>{(parseFloat(l.discount) || 0).toFixed(2)}</Text>
                    </TouchableOpacity>
                    <Text style={[styles.itemCell, { width: 80, textAlign: "right" }]}>{lineTotal(l).toFixed(2)}</Text>
                  </>
                )}
                <TouchableOpacity style={{ width: 28, alignItems: "center" }} onPress={() => removeLine(i)}>
                  <Ionicons name="close-circle" size={18} color="#EF4444" />
                </TouchableOpacity>
              </View>
            );
          })}

          <TouchableOpacity style={styles.itemsAddBar} onPress={() => setPicker(true)} testID="add-items">
            <Text style={styles.itemsAddBarText}>{cfg.addBarLabel}</Text>
          </TouchableOpacity>
        </ScrollView>

        {/* ── footer totals (purchase only) ── */}
        {!reconcile && (
          <View style={styles.docFooter}>
            {cfg.hasAvgCost && (
              <View style={styles.footToggle}>
                <Text style={styles.footToggleLabel}>AVG Cost Calculate</Text>
                <Switch value={avgCost} onValueChange={setAvgCost} trackColor={{ true: "#00B14F" }} />
              </View>
            )}
            <View style={styles.footToggle}>
              <Text style={styles.footToggleLabel}>Tax Included</Text>
              <Switch value={taxIncluded} onValueChange={setTaxIncluded} trackColor={{ true: "#00B14F" }} />
            </View>
            <View style={styles.footStat}><Text style={styles.footStatLabel}>Total</Text><Text style={styles.footStatVal}>{subtotal.toFixed(2)}</Text></View>
            <View style={styles.footStat}><Text style={styles.footStatLabel}>Discount</Text><Text style={styles.footStatVal}>{discountSum.toFixed(2)}</Text></View>
            <View style={styles.footStat}><Text style={styles.footStatLabel}>Tax 7%</Text><Text style={styles.footStatVal}>{tax.toFixed(2)}</Text></View>
          </View>
        )}

        <ProductPickerModal
          visible={picker}
          products={products}
          categories={categories}
          existing={lines.map((l) => l.product_id)}
          onClose={() => setPicker(false)}
          onDone={addProducts}
        />
        <SelectDocumentsModal
          visible={importOpen}
          type={type}
          onClose={() => setImportOpen(false)}
          onLoad={importDocs}
        />
        <AmountKeypad
          visible={!!keypad}
          initial={keypad ? lines[keypad.idx]?.[keypad.field] ?? "0" : "0"}
          onCancel={() => setKeypad(null)}
          onDone={(v) => { if (keypad) setField(keypad.idx, keypad.field, v); setKeypad(null); }}
        />
      </SafeAreaView>
    </Modal>
  );
}

// Select Products popup (images 6 / 7).
function ProductPickerModal({
  visible, products, categories, existing, onClose, onDone,
}: {
  visible: boolean; products: Product[]; categories: Category[];
  existing: string[]; onClose: () => void; onDone: (picked: Product[]) => void;
}) {
  const [search, setSearch] = useState("");
  const [catId, setCatId] = useState<string>("");
  const [sort, setSort] = useState<"custom" | "name">("custom");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [catOpen, setCatOpen] = useState(false);

  useEffect(() => { if (visible) { setSearch(""); setCatId(""); setSelected(new Set()); } }, [visible]);

  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    let l = products.filter((p) => p.product_type !== "BOM");
    if (catId) l = l.filter((p) => p.category_id === catId);
    if (q) l = l.filter((p) => p.name.toLowerCase().includes(q) || (p.barcode || "").toLowerCase().includes(q));
    if (sort === "name") l = [...l].sort((a, b) => a.name.localeCompare(b.name));
    return l;
  }, [products, catId, search, sort]);

  const toggle = (id: string) =>
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });

  const curCatName = catId ? (categories.find((c) => c.id === catId)?.name || "All Categories") : "All Categories";

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.pickerOverlay}>
        <View style={styles.pickerCard} testID="product-picker">
          <View style={styles.pickerHead}>
            <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color="#EF4444" /></TouchableOpacity>
            <Text style={styles.pickerTitle}>Select Products</Text>
            <TouchableOpacity onPress={() => onDone(products.filter((p) => selected.has(p.id)))}>
              <Text style={styles.pickerDone}>Done</Text>
            </TouchableOpacity>
          </View>

          <TouchableOpacity style={styles.pickerCatRow} onPress={() => setCatOpen((o) => !o)}>
            <Text style={styles.pickerCatText}>{curCatName}</Text>
            <Ionicons name="chevron-forward" size={16} color="#94A3B8" />
          </TouchableOpacity>
          {catOpen && (
            <View style={styles.pickerCatList}>
              <ScrollView style={{ maxHeight: 160 }}>
                <TouchableOpacity style={styles.pickerCatItem} onPress={() => { setCatId(""); setCatOpen(false); }}>
                  <Text style={styles.pickerCatItemText}>All Categories</Text>
                </TouchableOpacity>
                {categories.map((c) => (
                  <TouchableOpacity key={c.id} style={styles.pickerCatItem} onPress={() => { setCatId(c.id); setCatOpen(false); }}>
                    <Text style={styles.pickerCatItemText}>{c.name}</Text>
                  </TouchableOpacity>
                ))}
              </ScrollView>
            </View>
          )}

          <View style={styles.pickerSearchRow}>
            <Ionicons name="search" size={16} color="#94A3B8" />
            <TextInput style={styles.pickerSearchInput} placeholder="Search" placeholderTextColor="#94A3B8" value={search} onChangeText={setSearch} />
            <Ionicons name="barcode-outline" size={20} color="#00B14F" />
          </View>

          <View style={styles.pickerSortRow}>
            <Text style={styles.sortLabel}>Sort</Text>
            {(["custom", "name"] as const).map((s) => (
              <TouchableOpacity key={s} style={[styles.sortTab, sort === s && styles.sortTabActive]} onPress={() => setSort(s)}>
                <Text style={[styles.sortTabText, sort === s && styles.sortTabTextActive]}>{s === "custom" ? "Custom" : "Name"}</Text>
              </TouchableOpacity>
            ))}
          </View>

          <FlatList
            data={list}
            keyExtractor={(p) => p.id}
            style={{ flex: 1 }}
            renderItem={({ item }) => {
              const checked = selected.has(item.id);
              const already = existing.includes(item.id);
              return (
                <TouchableOpacity
                  style={styles.pickerRow}
                  disabled={already}
                  onPress={() => toggle(item.id)}
                  testID={`pick-${item.id}`}
                >
                  <Ionicons
                    name={already || checked ? "radio-button-on" : "radio-button-off"}
                    size={20}
                    color={already ? "#CBD5E1" : checked ? "#00B14F" : "#CBD5E1"}
                  />
                  <Image source={{ uri: item.image_base64 || item.image_url }} style={styles.pickerImg} />
                  <View style={{ flex: 1 }}>
                    <Text style={[styles.pickerName, already && { color: "#CBD5E1" }]} numberOfLines={1}>{item.name}</Text>
                    <Text style={styles.pickerBarcode}>{item.barcode}</Text>
                  </View>
                </TouchableOpacity>
              );
            }}
            ListEmptyComponent={<View style={styles.emptyBox}><Text style={styles.emptyText}>No products</Text></View>}
          />
        </View>
      </View>
    </Modal>
  );
}

// Custom numeric keypad popup (the "Amount" pad).
function AmountKeypad({
  visible, initial, onCancel, onDone,
}: { visible: boolean; initial: string; onCancel: () => void; onDone: (v: string) => void }) {
  const [val, setVal] = useState(initial);
  useEffect(() => { if (visible) setVal(initial === "0" ? "" : initial); }, [visible, initial]);

  const press = (k: string) => {
    if (k === "del") { setVal((v) => v.slice(0, -1)); return; }
    if (k === ".") { setVal((v) => (v.includes(".") ? v : (v || "0") + ".")); return; }
    setVal((v) => (v === "0" ? k : v + k));
  };
  const keys = ["7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", "del"];

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onCancel}>
      <TouchableOpacity style={styles.keypadOverlay} activeOpacity={1} onPress={onCancel}>
        <TouchableOpacity activeOpacity={1} style={styles.keypadCard}>
          <Text style={styles.keypadTitle}>Amount</Text>
          <Text style={styles.keypadValue}>{val || "0"}</Text>
          <View style={styles.keypadGrid}>
            {keys.map((k) => (
              <TouchableOpacity key={k} style={styles.keypadKey} onPress={() => press(k)}>
                {k === "del"
                  ? <Ionicons name="backspace-outline" size={22} color="#0F172A" />
                  : <Text style={styles.keypadKeyText}>{k}</Text>}
              </TouchableOpacity>
            ))}
          </View>
          <TouchableOpacity style={styles.keypadDone} onPress={() => onDone(val || "0")}>
            <Text style={styles.keypadDoneText}>Done</Text>
          </TouchableOpacity>
        </TouchableOpacity>
      </TouchableOpacity>
    </Modal>
  );
}

// Import Documents → Select Documents popup (image 4). Lists previously-saved
// documents of the same type; "Load Documents" pulls their product lines in.
function SelectDocumentsModal({
  visible, type, onClose, onLoad,
}: { visible: boolean; type: DocType; onClose: () => void; onLoad: (docs: StockDoc[]) => void }) {
  const [docs, setDocs] = useState<StockDoc[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [cal, setCal] = useState<null | "from" | "to">(null);

  useEffect(() => {
    if (!visible) return;
    setSelected(new Set()); setFrom(""); setTo(""); setDocs(null);
    (async () => {
      try {
        const res = await apiFetch(`${API}/stock-documents?type=${type}`);
        setDocs(await res.json());
      } catch { setDocs([]); }
    })();
  }, [visible, type]);

  const filtered = (docs || []).filter((d) => {
    const day = (d.created_at || "").slice(0, 10);
    if (from && day < from) return false;
    if (to && day > to) return false;
    return true;
  });

  const toggle = (id: string) =>
    setSelected((prev) => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.pickerOverlay}>
        <View style={styles.pickerCard} testID="select-documents">
          <View style={styles.pickerHead}>
            <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color="#EF4444" /></TouchableOpacity>
            <Text style={styles.pickerTitle}>Select Documents</Text>
            <TouchableOpacity
              style={styles.loadDocsBtn}
              onPress={() => onLoad((docs || []).filter((d) => selected.has(d.id)))}
            >
              <Text style={styles.loadDocsText}>Load Documents</Text>
            </TouchableOpacity>
          </View>

          <View style={styles.selDateRow}>
            <TouchableOpacity style={styles.selDateField} onPress={() => setCal("from")}>
              <Text style={styles.docFieldLabel}>From Date</Text>
              <Text style={styles.selDateVal}>{from || "Select date"}</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.selDateField} onPress={() => setCal("to")}>
              <Text style={styles.docFieldLabel}>To Date</Text>
              <Text style={styles.selDateVal}>{to || "Select date"}</Text>
            </TouchableOpacity>
          </View>

          {cal && (
            <View style={styles.selCalPop}>
              <Calendar
                start={cal === "from" ? from : to}
                end=""
                onPick={(iso) => { if (cal === "from") setFrom(iso); else setTo(iso); setCal(null); }}
              />
            </View>
          )}

          <View style={styles.docColHead}>
            <Text style={[styles.docColCell, { width: 150 }]}>Date</Text>
            <Text style={[styles.docColCell, { flex: 1 }]}>Document Name</Text>
            <View style={{ width: 28 }} />
          </View>

          {docs === null ? (
            <ActivityIndicator color="#00B14F" style={{ marginTop: 30 }} />
          ) : filtered.length === 0 ? (
            <View style={styles.emptyBox}><Text style={styles.emptyText}>No items</Text></View>
          ) : (
            <FlatList
              data={filtered}
              keyExtractor={(d) => d.id}
              style={{ flex: 1 }}
              renderItem={({ item }) => {
                const dt = new Date(item.created_at);
                const checked = selected.has(item.id);
                return (
                  <TouchableOpacity style={styles.docRow} onPress={() => toggle(item.id)}>
                    <Text style={[styles.docCell, { width: 150 }]}>{thaiDate(dt)}</Text>
                    <Text style={[styles.docCell, { flex: 1 }]} numberOfLines={1}>
                      {item.document_name || item.note || item.document_no}
                    </Text>
                    <Ionicons
                      name={checked ? "checkbox" : "square-outline"}
                      size={20}
                      color={checked ? "#00B14F" : "#CBD5E1"}
                      style={{ width: 28 }}
                    />
                  </TouchableOpacity>
                );
              }}
            />
          )}
        </View>
      </View>
    </Modal>
  );
}

function StockMovementModal({
  product, defaultType, onClose, onSave,
}: {
  product: Product | null;
  defaultType: "in" | "out" | "adjust";
  onClose: () => void;
  onSave: (t: "in" | "out" | "adjust", qty: number) => void;
}) {
  const [type, setType] = useState(defaultType);
  const [qty, setQty] = useState("");
  useEffect(() => {
    if (product) { setType(defaultType); setQty(""); }
  }, [product, defaultType]);

  return (
    <Modal visible={!!product} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.modalOverlay}>
        {product && (
          <View style={styles.smallModal} testID="stock-modal">
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>Stock · {product.name.slice(0, 24)}</Text>
              <View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <View style={{ flexDirection: "row", gap: 8 }}>
                {(["in", "out", "adjust"] as const).map((t) => (
                  <TouchableOpacity
                    key={t}
                    style={[styles.typeBtn, type === t && styles.typeBtnActive]}
                    onPress={() => setType(t)}
                    testID={`stock-type-${t}`}
                  >
                    <Text style={[styles.typeBtnText, type === t && { color: "#FFF" }]}>
                      {t === "in" ? "Stock-In" : t === "out" ? "Stock-Out" : "Adjust"}
                    </Text>
                  </TouchableOpacity>
                ))}
              </View>
              <Text style={styles.formLabel}>Current stock: {product.stock}</Text>
              <TextInput
                placeholder={type === "adjust" ? "New stock value" : "Quantity"}
                keyboardType="number-pad"
                style={styles.formInput}
                value={qty}
                onChangeText={setQty}
                testID="stock-qty"
              />
              <TouchableOpacity
                style={[styles.primaryBtn, !qty && { opacity: 0.5 }]}
                disabled={!qty}
                onPress={() => onSave(type, parseInt(qty || "0"))}
                testID="stock-save"
              >
                <Text style={styles.primaryBtnText}>Save Movement</Text>
              </TouchableOpacity>
            </View>
          </View>
        )}
      </View>
    </Modal>
  );
}

// =================== CUSTOMERS ===================
function Customers({ isWide }: { isWide: boolean }) {
  const [list, setList] = useState<Customer[]>([]);
  const [sel, setSel] = useState<Customer | null>(null);
  const [stats, setStats] = useState<CustomerStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const statsRequestId = useRef<string | null>(null);
  const [q, setQ] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [phoneValid, setPhoneValid] = useState(true);

  const load = async () => {
    const r = await apiFetch(`${API}/customers`);
    const d: Customer[] = await r.json();
    setList(d);
    if (d[0] && !sel) setSel(d[0]);
  };
  useEffect(() => { load(); }, []);

  const loadStats = async (customerId: string) => {
    // Track which customer this request is for so stale responses are discarded.
    statsRequestId.current = customerId;
    setStatsLoading(true);
    setStats(null);
    try {
      const r = await apiFetch(`${API}/customers/${customerId}/stats`);
      if (r.ok && statsRequestId.current === customerId) {
        setStats(await r.json());
      }
    } finally {
      if (statsRequestId.current === customerId) setStatsLoading(false);
    }
  };

  useEffect(() => {
    if (sel) loadStats(sel.id);
    else { statsRequestId.current = null; setStats(null); }
  }, [sel?.id]);

  const filtered = list.filter(
    (c) => !q || c.name.toLowerCase().includes(q.toLowerCase()) || c.phone?.includes(q)
  );

  const save = async () => {
    if (!name.trim()) return;
    let c: any = null;
    try {
      const r = await apiFetch(`${API}/customers`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, phone: phone || null }),
      });
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        throw new Error(detail || `Server error (${r.status})`);
      }
      c = await r.json();
    } catch (e: any) {
      Alert.alert("Couldn't save customer", e?.message || "Please try again.");
      return;
    }
    if (!c || !c.name) {
      Alert.alert("Couldn't save customer", "Unexpected response from server.");
      return;
    }
    setList((l) => [c, ...l]);
    setSel(c); setName(""); setPhone(""); setAddOpen(false);
  };

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="customers-section">
      <View style={[styles.leftNav, !isWide && styles.leftNavStacked]}>
        <View style={styles.custHeader}>
          <Text style={styles.sectionHeader}>Customers</Text>
          <TouchableOpacity onPress={() => setAddOpen(true)} testID="add-customer-admin">
            <Text style={styles.addLink}>Add</Text>
          </TouchableOpacity>
        </View>
        <View style={styles.searchBoxRow}>
          <Ionicons name="search" size={16} color="#94A3B8" />
          <TextInput
            placeholder="Search"
            style={styles.searchBoxInput}
            value={q}
            onChangeText={setQ}
            placeholderTextColor="#94A3B8"
            testID="cust-admin-search"
          />
        </View>
        <FlatList
          data={filtered}
          keyExtractor={(i) => i.id}
          renderItem={({ item }) => (
            <TouchableOpacity
              style={[styles.custAdminRow, sel?.id === item.id && styles.custAdminActive]}
              onPress={() => setSel(item)}
              testID={`cust-admin-${item.id}`}
            >
              <View style={[styles.custAv, { backgroundColor: item.color }]}>
                <Text style={styles.custAvText}>
                  {item.name[0]?.toUpperCase() || "?"}
                </Text>
              </View>
              <View style={{ flex: 1 }}>
                <Text style={styles.custAdminName} numberOfLines={1}>{item.name}</Text>
                {item.phone && <Text style={styles.custAdminPhone}>{item.phone}</Text>}
              </View>
            </TouchableOpacity>
          )}
        />
      </View>
      <View style={{ flex: 1, padding: 20 }}>
        {!sel ? (
          <View style={styles.emptyBox}><Text style={styles.emptyText}>Select a customer</Text></View>
        ) : (
          <>
            <View style={styles.custProfile}>
              <View style={[styles.custAvLarge, { backgroundColor: sel.color }]}>
                <Text style={styles.custAvLargeText}>
                  {sel.name[0]?.toUpperCase() || "?"}
                </Text>
              </View>
              <Text style={styles.custProfileName}>{sel.name}</Text>
              <Text style={styles.custPoints}>
                <Text style={{ fontSize: 22, fontWeight: "700" }}>0</Text>{" "}
                <Text style={{ color: "#94A3B8" }}>Point</Text>
              </Text>
            </View>
            <View style={styles.statsRow}>
              <View style={styles.statCell}>
                <Text style={{ color: "#00B14F", fontWeight: "600", fontSize: 12 }}>Success</Text>
                {statsLoading ? (
                  <ActivityIndicator size="small" color="#00B14F" />
                ) : (
                  <>
                    <Text style={{ fontSize: 24, fontWeight: "700" }}>{THB(stats?.success_total ?? 0)}</Text>
                    <Text style={styles.statSub}>{stats?.bill_count ?? 0} Bills</Text>
                  </>
                )}
              </View>
              <View style={styles.statCell}>
                <Text style={{ color: "#F59E0B", fontWeight: "600", fontSize: 12 }}>Avg/bill</Text>
                {statsLoading ? (
                  <ActivityIndicator size="small" color="#F59E0B" />
                ) : (
                  <Text style={{ fontSize: 24, fontWeight: "700" }}>{THB(stats?.avg_bill ?? 0)}</Text>
                )}
              </View>
              <View style={styles.statCell}>
                <Text style={{ color: "#EF4444", fontWeight: "600", fontSize: 12 }}>Outstanding</Text>
                {statsLoading ? (
                  <ActivityIndicator size="small" color="#EF4444" />
                ) : (
                  <>
                    <Text style={{ fontSize: 24, fontWeight: "700" }}>{THB(stats?.outstanding_total ?? 0)}</Text>
                    <Text style={styles.statSub}>{stats?.outstanding_count ?? 0} Bills</Text>
                  </>
                )}
              </View>
            </View>
            <View style={{ flexDirection: "row", gap: 12, marginTop: 20 }}>
              <View style={styles.topBox}>
                <Text style={styles.chartTitle}>Top Products</Text>
                {statsLoading ? (
                  <ActivityIndicator size="small" color="#94A3B8" style={{ marginTop: 12 }} />
                ) : stats?.top_products?.length ? (
                  stats.top_products.map((p, i) => (
                    <View key={p.product_id} style={styles.topRow}>
                      <Text style={styles.topRank}>#{i + 1}</Text>
                      <Text style={styles.topName} numberOfLines={1}>{p.name}</Text>
                      <Text style={styles.topValue}>{THB(p.total)}</Text>
                    </View>
                  ))
                ) : (
                  <View style={styles.emptyBox}><Text style={styles.emptyText}>No items</Text></View>
                )}
              </View>
              <View style={styles.topBox}>
                <Text style={styles.chartTitle}>Top Categories</Text>
                {statsLoading ? (
                  <ActivityIndicator size="small" color="#94A3B8" style={{ marginTop: 12 }} />
                ) : stats?.top_categories?.length ? (
                  stats.top_categories.map((c, i) => (
                    <View key={c.name} style={styles.topRow}>
                      <Text style={styles.topRank}>#{i + 1}</Text>
                      <Text style={styles.topName} numberOfLines={1}>{c.name}</Text>
                      <Text style={styles.topValue}>{THB(c.total)}</Text>
                    </View>
                  ))
                ) : (
                  <View style={styles.emptyBox}><Text style={styles.emptyText}>No items</Text></View>
                )}
              </View>
            </View>
          </>
        )}
      </View>

      <Modal visible={addOpen} transparent animationType="fade">
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setAddOpen(false)}>
                <Ionicons name="close" size={24} color="#475569" />
              </TouchableOpacity>
              <Text style={styles.modalTitle}>New Customer</Text>
              <View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <TextInput placeholder="Name" style={styles.formInput} value={name} onChangeText={setName} testID="admin-cust-name" />
              <PhoneInput
                value={phone}
                onChange={(e164, valid) => { setPhone(e164); setPhoneValid(valid); }}
                placeholder="Phone (optional)"
                defaultCountryCode="TH"
                testID="admin-cust-phone"
              />
              <TouchableOpacity
                style={[styles.primaryBtn, (!name.trim() || !phoneValid) && { opacity: 0.4 }]}
                onPress={save}
                disabled={!name.trim() || !phoneValid}
                testID="admin-cust-save"
              >
                <Text style={styles.primaryBtnText}>Save</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}


// =================== PRODUCTS ===================
function Products({ isWide, isAdmin }: { isWide: boolean; isAdmin: boolean }) {
  const [cats, setCats] = useState<Category[]>([]);
  const [prods, setProds] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("");
  const [edit, setEdit] = useState<Product | "new" | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"custom" | "name">("custom");
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    const [c, p] = await Promise.all([
      apiFetch(`${API}/categories`).then((r) => r.json()),
      apiFetch(`${API}/products`).then((r) => r.json()),
    ]);
    setCats(c); setProds(p);
    if (!activeCat && c.length) setActiveCat(c[0].id);
  };
  useEffect(() => { load(); }, []);

  const onRefresh = async () => {
    setRefreshing(true);
    try { await load(); } finally { setRefreshing(false); }
  };

  const filtered = useMemo(() => {
    let list = prods;
    if (q) list = list.filter((p) => p.name.toLowerCase().includes(q.toLowerCase()));
    else if (activeCat) {
      const cat = cats.find((c) => c.id === activeCat);
      if (cat?.name === "Favorite") list = list.filter((p) => p.is_favorite);
      else list = list.filter((p) => p.category_id === activeCat);
    }
    if (sort === "name") {
      list = [...list].sort((a, b) => a.name.localeCompare(b.name));
    }
    return list;
  }, [prods, cats, activeCat, q, sort]);

  const curCat = cats.find((c) => c.id === activeCat);

  const toggleFav = async (p: Product) => {
    await apiFetch(`${API}/products/${p.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_favorite: !p.is_favorite }),
    });
    load();
  };

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="products-section">
      {/* On narrow, swap the vertical category rail for a compact horizontal
          chip strip + search above the products grid.  Otherwise the rail's
          ~600px height would push the FlatList off-screen entirely. */}
      {isWide ? (
        <View style={styles.leftNav}>
          <View style={styles.custHeader}>
            <Text style={styles.sectionHeader}>Products</Text>
          </View>
          <View style={styles.searchBoxRow}>
            <Ionicons name="search" size={16} color="#94A3B8" />
            <TextInput
              placeholder="Search Products"
              style={styles.searchBoxInput}
              value={q}
              onChangeText={setQ}
              placeholderTextColor="#94A3B8"
              testID="admin-prod-search"
            />
          </View>
          <ScrollView>
            <Text style={styles.allCatsLabel}>All Categories</Text>
            {cats.map((c) => {
              const active = activeCat === c.id;
              const hasItems = prods.some((p) => c.name === "Favorite" ? p.is_favorite : p.category_id === c.id);
              return (
                <TouchableOpacity
                  key={c.id}
                  style={[styles.catMgmtRow, active && styles.leftNavRowActive]}
                  onPress={() => { setActiveCat(c.id); setQ(""); }}
                  testID={`admin-cat-${c.id}`}
                >
                  <Ionicons
                    name={hasItems ? "checkmark-circle" : "ellipse-outline"}
                    size={18}
                    color={hasItems ? c.color : "#CBD5E1"}
                  />
                  <View style={{ flex: 1 }}>
                    <Text style={styles.catMgmtName}>{c.name}</Text>
                    {c.source && <Text style={styles.catMgmtSource}>Source: {c.source}</Text>}
                  </View>
                  <Ionicons name="chevron-forward" size={14} color="#94A3B8" />
                </TouchableOpacity>
              );
            })}
          </ScrollView>
        </View>
      ) : (
        <View style={styles.narrowCatBar}>
          <View style={styles.searchBoxRow}>
            <Ionicons name="search" size={16} color="#94A3B8" />
            <TextInput
              placeholder="Search Products"
              style={styles.searchBoxInput}
              value={q}
              onChangeText={setQ}
              placeholderTextColor="#94A3B8"
              testID="admin-prod-search"
            />
          </View>
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={{ paddingHorizontal: 12, paddingVertical: 8, gap: 8 }}
          >
            {cats.map((c) => {
              const active = activeCat === c.id;
              return (
                <TouchableOpacity
                  key={c.id}
                  style={[styles.catChip, active && { backgroundColor: c.color, borderColor: c.color }]}
                  onPress={() => { setActiveCat(c.id); setQ(""); }}
                  testID={`admin-cat-${c.id}`}
                >
                  <Text style={[styles.catChipText, active && { color: "#FFF" }]}>{c.name}</Text>
                </TouchableOpacity>
              );
            })}
          </ScrollView>
        </View>
      )}
      <View style={{ flex: 1 }}>
        <View style={styles.prodHeader}>
          <Text style={styles.sectionHeader}>
            {q ? `Search` : curCat?.name}({filtered.length})
          </Text>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 14 }}>
            <Text style={{ fontSize: 12, color: "#94A3B8" }}>Sort</Text>
            <View style={styles.sortGroup}>
              <TouchableOpacity
                style={[styles.sortBtn, sort === "custom" && styles.sortBtnActive]}
                onPress={() => setSort("custom")}
                testID="sort-custom"
              >
                <Text style={[styles.sortText, sort === "custom" && { color: "#00B14F" }]}>
                  Custom
                </Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.sortBtn, sort === "name" && styles.sortBtnActive]}
                onPress={() => setSort("name")}
                testID="sort-name"
              >
                <Text style={[styles.sortText, sort === "name" && { color: "#00B14F" }]}>
                  Name
                </Text>
              </TouchableOpacity>
            </View>
            {isAdmin && (
              <>
                <TouchableOpacity testID="edit-products">
                  <Text style={styles.linkText}>Edit</Text>
                </TouchableOpacity>
                <TouchableOpacity onPress={() => setEdit("new")} testID="add-product">
                  <Text style={styles.linkTextBold}>Add Product</Text>
                </TouchableOpacity>
              </>
            )}
          </View>
        </View>
        <FlatList
          data={filtered}
          keyExtractor={(i) => i.id}
          contentContainerStyle={{ padding: 14 }}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} colors={["#00B14F"]} tintColor="#00B14F" />
          }
          renderItem={({ item }) => (
            <View style={styles.prodMgmtRow} testID={`prod-${item.id}`}>
              <Image source={{ uri: item.image_base64 || item.image_url }} style={styles.invImg} />
              <View style={{ flex: 1 }}>
                <Text style={styles.invName} numberOfLines={2}>{item.name}</Text>
                <View style={{ flexDirection: "row", gap: 10, marginTop: 4, alignItems: "center" }}>
                  <Text style={styles.prodPriceLabel}>
                    <Text style={{ fontWeight: "700", color: item.price === 0 ? "#EF4444" : "#0F172A" }}>
                      {THB(item.price)}
                    </Text>
                  </Text>
                  {item.price === 0 && (
                    <View style={styles.warnPill}>
                      <Text style={styles.warnPillText}>!</Text>
                    </View>
                  )}
                  <Text style={styles.prodPriceLabel}>Cost {THB(item.cost)}</Text>
                  {item.cost === 0 && item.price !== 0 && (
                    <View style={styles.warnPill}>
                      <Text style={styles.warnPillText}>!</Text>
                    </View>
                  )}
                </View>
              </View>
              <TouchableOpacity
                onPress={() => toggleFav(item)}
                disabled={!isAdmin}
                testID={`fav-${item.id}`}
              >
                <Ionicons
                  name={item.is_favorite ? "heart" : "heart-outline"}
                  size={22}
                  color={item.is_favorite ? "#00B14F" : "#CBD5E1"}
                />
              </TouchableOpacity>
              <View style={styles.prodTags}>
                <Text style={styles.tag}>TAX: {item.tax_type}</Text>
                <Text style={[styles.tag, item.product_type === "BOM" && { color: "#00B14F", fontWeight: "700" }]}>
                  Type: {item.product_type}
                </Text>
              </View>
              {isAdmin && (
                <TouchableOpacity style={styles.editBtn} onPress={() => setEdit(item)} testID={`edit-${item.id}`}>
                  <Ionicons name="create-outline" size={18} color="#475569" />
                </TouchableOpacity>
              )}
            </View>
          )}
        />
      </View>

      <ProductEditModal
        product={edit}
        categories={cats.filter((c) => c.name !== "Favorite")}
        defaultCat={activeCat}
        onClose={() => setEdit(null)}
        onSaved={() => { setEdit(null); load(); }}
      />
    </View>
  );
}

function ProductEditModal({
  product, categories, defaultCat, onClose, onSaved,
}: {
  product: Product | "new" | null;
  categories: Category[];
  defaultCat: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const [cost, setCost] = useState("");
  const [stock, setStock] = useState("");
  const [catId, setCatId] = useState("");
  const [img, setImg] = useState("");
  const [imgBase64, setImgBase64] = useState<string>("");   // data URI if picked from device
  const [pickingImage, setPickingImage] = useState(false);
  const [fav, setFav] = useState(false);
  const isNew = product === "new";

  useEffect(() => {
    if (product && product !== "new") {
      setName(product.name); setPrice(String(product.price));
      setCost(String(product.cost)); setStock(String(product.stock));
      setCatId(product.category_id); setImg(product.image_url);
      setImgBase64(product.image_base64 || "");
      setFav(product.is_favorite);
    } else if (product === "new") {
      setName(""); setPrice(""); setCost("0"); setStock("0");
      setCatId(defaultCat); setImg(""); setImgBase64(""); setFav(false);
    }
  }, [product, defaultCat]);

  // Pick from device → resize to 800px max → JPEG 70% → base64.
  // The resize cap keeps the row under ~80 KB so the Postgres TEXT column
  // doesn't bloat with multi-MB photos.
  const pickImage = async () => {
    setPickingImage(true);
    try {
      const perm = await ImagePicker.requestMediaLibraryPermissionsAsync();
      if (!perm.granted) {
        setPickingImage(false);
        return;
      }
      const result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ImagePicker.MediaTypeOptions.Images,
        allowsEditing: true,
        aspect: [1, 1],
        quality: 1,
      });
      if (result.canceled || !result.assets?.[0]) {
        setPickingImage(false);
        return;
      }
      const original = result.assets[0];
      const manipulated = await ImageManipulator.manipulateAsync(
        original.uri,
        [{ resize: { width: 800 } }],
        { compress: 0.7, format: ImageManipulator.SaveFormat.JPEG, base64: true },
      );
      if (manipulated.base64) {
        setImgBase64(`data:image/jpeg;base64,${manipulated.base64}`);
        setImg(""); // clear the URL field so the new image wins
      }
    } catch (e) {
      console.warn("Image pick failed", e);
    } finally {
      setPickingImage(false);
    }
  };

  const clearImage = () => {
    setImg("");
    setImgBase64("");
  };

  const save = async () => {
    if (!name.trim() || !price || !catId) return;
    const body: Record<string, any> = {
      name, price: parseFloat(price), cost: parseFloat(cost || "0"),
      stock: parseInt(stock || "0"), category_id: catId,
      image_url: img || "",
      image_base64: imgBase64 || "",
      is_favorite: fav, tax_type: "V", product_type: "P",
    };
    // If neither image source is set, fall back to the default placeholder so
    // the product list grid still shows *something* instead of a broken icon.
    if (!body.image_url && !body.image_base64) {
      body.image_url = "https://images.pexels.com/photos/36500580/pexels-photo-36500580.jpeg?w=400";
    }
    if (isNew) {
      await apiFetch(`${API}/products`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    } else if (product) {
      await apiFetch(`${API}/products/${(product as Product).id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    }
    onSaved();
  };

  const del = async () => {
    if (!product || product === "new") return;
    await apiFetch(`${API}/products/${(product as Product).id}`, { method: "DELETE" });
    onSaved();
  };

  return (
    <Modal visible={!!product} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.modalOverlay}>
        {product && (
          <View style={styles.editModal} testID="product-edit-modal">
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>{isNew ? "Add Product" : "Edit Product"}</Text>
              <View style={{ width: 24 }} />
            </View>
            <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>Name</Text>
              <TextInput style={styles.formInput} value={name} onChangeText={setName} placeholder="Product name" testID="prod-name" />
              <View style={{ flexDirection: "row", gap: 12 }}>
                <View style={{ flex: 1, gap: 6 }}>
                  <Text style={styles.formLabel}>Price (THB)</Text>
                  <TextInput style={styles.formInput} value={price} onChangeText={setPrice} keyboardType="decimal-pad" placeholder="0.00" testID="prod-price" />
                </View>
                <View style={{ flex: 1, gap: 6 }}>
                  <Text style={styles.formLabel}>Cost (THB)</Text>
                  <TextInput style={styles.formInput} value={cost} onChangeText={setCost} keyboardType="decimal-pad" placeholder="0.00" testID="prod-cost" />
                </View>
              </View>
              <Text style={styles.formLabel}>Stock</Text>
              <TextInput style={styles.formInput} value={stock} onChangeText={setStock} keyboardType="number-pad" placeholder="0" testID="prod-stock" />
              <Text style={styles.formLabel}>Image</Text>
              <View style={{ flexDirection: "row", alignItems: "center", gap: 12 }}>
                <View style={styles.imgThumb}>
                  {imgBase64 || img ? (
                    <Image source={{ uri: imgBase64 || img }} style={styles.imgThumbImage} />
                  ) : (
                    <Ionicons name="image-outline" size={36} color="#94A3B8" />
                  )}
                </View>
                <View style={{ flex: 1, gap: 8 }}>
                  <TouchableOpacity
                    style={styles.imgPickBtn}
                    onPress={pickImage}
                    disabled={pickingImage}
                    testID="prod-img-pick"
                  >
                    <Ionicons name="camera-outline" size={18} color="#0F172A" />
                    <Text style={styles.imgPickBtnText}>
                      {pickingImage ? "Loading…" : (imgBase64 || img ? "Change Image" : "Choose Image")}
                    </Text>
                  </TouchableOpacity>
                  {(imgBase64 || img) && (
                    <TouchableOpacity onPress={clearImage} testID="prod-img-clear">
                      <Text style={styles.imgClearText}>Remove image</Text>
                    </TouchableOpacity>
                  )}
                </View>
              </View>
              <Text style={styles.formLabel}>Category</Text>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
                {categories.map((c) => (
                  <TouchableOpacity
                    key={c.id}
                    style={[styles.catPick, catId === c.id && styles.catPickActive]}
                    onPress={() => setCatId(c.id)}
                  >
                    <Text style={[styles.catPickText, catId === c.id && { color: "#FFF" }]}>
                      {c.name}
                    </Text>
                  </TouchableOpacity>
                ))}
              </View>
              <TouchableOpacity
                style={styles.favToggle}
                onPress={() => setFav((f) => !f)}
                testID="prod-fav-toggle"
              >
                <Ionicons
                  name={fav ? "heart" : "heart-outline"}
                  size={22}
                  color={fav ? "#00B14F" : "#94A3B8"}
                />
                <Text style={{ color: fav ? "#00B14F" : "#475569", fontWeight: "600" }}>
                  {fav ? "Favorite" : "Mark as favorite"}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.primaryBtn} onPress={save} testID="prod-save">
                <Text style={styles.primaryBtnText}>{isNew ? "Create" : "Save Changes"}</Text>
              </TouchableOpacity>
              {!isNew && (
                <TouchableOpacity style={styles.dangerBtn} onPress={del} testID="prod-delete">
                  <Ionicons name="trash-outline" size={16} color="#EF4444" />
                  <Text style={{ color: "#EF4444", fontWeight: "600" }}>Delete product</Text>
                </TouchableOpacity>
              )}
            </ScrollView>
          </View>
        )}
      </View>
    </Modal>
  );
}

// =================== DRAWER / SHIFT ===================
type ShiftType = {
  id: string;
  round_number: number;
  start_cash: number;
  opened_at: string;
  opened_by: string;
  closed_at?: string;
  closed_by?: string;
  total_sales_cash: number;
  total_paid_in: number;
  total_paid_out: number;
  expected_in_drawer: number;
  actual_in_drawer?: number;
  status: string;
};

type DrawerCat = {
  id: string;
  type: "paid_in" | "paid_out";
  name: string;
  name_th?: string;
  sort_order?: number;
  active?: boolean;
};

function Drawer({ isWide, staff }: { isWide: boolean; staff: string }) {
  const [current, setCurrent] = useState<ShiftType | null>(null);
  const [history, setHistory] = useState<ShiftType[]>([]);
  const [tab, setTab] = useState<"shift" | "history">("shift");
  const [openDlg, setOpenDlg] = useState(false);
  const [closeDlg, setCloseDlg] = useState(false);
  const [moveDlg, setMoveDlg] = useState<"paid_in" | "paid_out" | null>(null);
  const [startCash, setStartCash] = useState("0");
  const [actualCash, setActualCash] = useState("");
  const [moveAmt, setMoveAmt] = useState("");
  const [moveNote, setMoveNote] = useState("");
  const [moveCat, setMoveCat] = useState<string>("");
  const [cats, setCats] = useState<DrawerCat[]>([]);
  const [showCatPicker, setShowCatPicker] = useState(false);
  const [printing, setPrinting] = useState(false);
  const { printShiftSummary, ShiftSummaryOverlay } = useShiftSummaryPrint();

  // Fetch shop header (POS#, tax %) for the printed slip — same shape the
  // receipt reprint uses.
  const fetchShop = async () => {
    try {
      const r = await apiFetch(`${API}/settings`);
      return r.ok ? await r.json() : {};
    } catch {
      return {};
    }
  };

  const load = async () => {
    const [cur, hist] = await Promise.all([
      apiFetch(`${API}/shifts/current`).then((r) => r.json()),
      apiFetch(`${API}/shifts`).then((r) => r.json()),
    ]);
    setCurrent(cur && cur.id ? cur : null);
    setHistory(hist || []);
  };
  useEffect(() => { load(); }, []);

  // Pull the reason-code list for whichever side (Paid In / Paid Out) is open.
  useEffect(() => {
    if (!moveDlg) return;
    apiFetch(`${API}/shift-categories?type=${moveDlg}&active=true`)
      .then((r) => r.json())
      .then((rows) => setCats(Array.isArray(rows) ? rows : []))
      .catch(() => setCats([]));
  }, [moveDlg]);

  const openShift = async () => {
    await apiFetch(`${API}/shifts/open`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_cash: parseFloat(startCash) || 0, opened_by: staff }),
    });
    setOpenDlg(false); setStartCash("0"); load();
  };
  const closeShift = async () => {
    const counted = parseFloat(actualCash);
    if (actualCash.trim() === "" || isNaN(counted) || counted < 0) {
      Alert.alert("Counted cash required", "Enter the actual amount counted in the drawer before closing.");
      return;
    }
    const res = await apiFetch(`${API}/shifts/close`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actual_in_drawer: counted, closed_by: staff }),
    });
    setCloseDlg(false); setActualCash("");
    // Print the close-shift summary (ใบสรุปปิดรอบการขาย) the moment the round
    // is closed, matching the reference POS. Print failures don't block the
    // close — the slip can be reprinted from History.
    try {
      const body = await res.json().catch(() => ({}));
      if (body?.summary) {
        setPrinting(true);
        const shop = await fetchShop();
        await printShiftSummary(body.summary, shop);
      }
    } catch (e) {
      console.error("shift summary print failed", e);
    } finally {
      setPrinting(false);
      load();
    }
  };

  // Reprint a (closed) shift's summary from the History tab.
  const reprintSummary = async (shiftId: string) => {
    setPrinting(true);
    try {
      const [summary, shop] = await Promise.all([
        apiFetch(`${API}/shifts/${shiftId}/summary`).then((r) => r.json()),
        fetchShop(),
      ]);
      await printShiftSummary(summary, shop);
    } catch (e) {
      console.error("reprint summary failed", e);
    } finally {
      setPrinting(false);
    }
  };
  const addMovement = async () => {
    if (!moveDlg || !moveAmt) return;
    await apiFetch(`${API}/shifts/movement`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: moveDlg, amount: parseFloat(moveAmt), category: moveCat, note: moveNote }),
    });
    setMoveDlg(null); setMoveAmt(""); setMoveNote(""); setMoveCat(""); load();
  };
  const closeMoveDlg = () => { setMoveDlg(null); setMoveAmt(""); setMoveNote(""); setMoveCat(""); };

  const expected = current
    ? current.start_cash + current.total_paid_in - current.total_paid_out
    : 0;

  const fmtDT = (iso?: string) =>
    iso ? new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }).replace(",", "") : "-";

  return (
    <View style={{ flex: 1 }} testID="drawer-section">
      <Text style={styles.shiftHeader}>Shift</Text>

      {tab === "shift" ? (
        <ScrollView contentContainerStyle={{ padding: 16 }}>
          {!current ? (
            <View style={styles.shiftCard}>
              <View style={styles.emptyBox}>
                <Ionicons name="file-tray-outline" size={40} color="#CBD5E1" />
                <Text style={styles.emptyText}>No open shift</Text>
                <TouchableOpacity style={[styles.primaryBtn, { marginTop: 14 }]} onPress={() => setOpenDlg(true)} testID="open-shift">
                  <Text style={styles.primaryBtnText}>Open Shift</Text>
                </TouchableOpacity>
              </View>
            </View>
          ) : (
            <>
              <View style={styles.shiftCard}>
                <ShiftRow label="Round" value={String(current.round_number)} strong />
                <ShiftRow label="Start Cash in Drawer" value={current.start_cash.toFixed(2)} />
                <ShiftRow label="Shift opened" value={fmtDT(current.opened_at)} />
                <ShiftRow label="Shift opened by" value={current.opened_by} />
              </View>
              <View style={styles.shiftCard}>
                <ShiftRow label="Total Sales (cash)" value={current.total_sales_cash.toFixed(2)} />
                <ShiftRow label="Total Paid In" value={current.total_paid_in.toFixed(2)} />
                <ShiftRow label="Total Paid Out" value={current.total_paid_out.toFixed(2)} />
                <ShiftRow label="Expected in Drawer" value={expected.toFixed(2)} />
              </View>
              <View style={styles.inOutRow}>
                <TouchableOpacity style={styles.inOutBtn} onPress={() => setMoveDlg("paid_in")} testID="paid-in">
                  <Text style={[styles.inOutText, { color: "#00B14F" }]}>Paid In</Text>
                </TouchableOpacity>
                <TouchableOpacity style={styles.inOutBtn} onPress={() => setMoveDlg("paid_out")} testID="paid-out">
                  <Text style={[styles.inOutText, { color: "#EF4444" }]}>Paid Out</Text>
                </TouchableOpacity>
              </View>
              <TouchableOpacity style={styles.closeShiftBtn} onPress={() => setCloseDlg(true)} testID="close-shift">
                <Text style={styles.closeShiftText}>CLOSE SHIFT</Text>
              </TouchableOpacity>
            </>
          )}
        </ScrollView>
      ) : (
        <ShiftHistory
          shifts={history}
          isWide={isWide}
          onReprint={reprintSummary}
          fmtDT={fmtDT}
        />
      )}

      <View style={styles.invTabs}>
        <TouchableOpacity style={[styles.invTab, tab === "shift" && styles.invTabActive]} onPress={() => setTab("shift")} testID="shift-tab">
          <Ionicons name="file-tray-outline" size={16} color={tab === "shift" ? "#00B14F" : "#475569"} />
          <Text style={[styles.invTabText, tab === "shift" && { color: "#00B14F" }]}>Shift</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.invTab, tab === "history" && styles.invTabActive]} onPress={() => setTab("history")} testID="history-tab">
          <Ionicons name="time-outline" size={16} color={tab === "history" ? "#00B14F" : "#475569"} />
          <Text style={[styles.invTabText, tab === "history" && { color: "#00B14F" }]}>History</Text>
        </TouchableOpacity>
      </View>

      {/* Open Shift dialog */}
      <Modal visible={openDlg} transparent animationType="fade" onRequestClose={() => setOpenDlg(false)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setOpenDlg(false)}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>Open Shift</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>Start Cash in Drawer (THB)</Text>
              <TextInput style={styles.formInput} value={startCash} onChangeText={setStartCash} keyboardType="decimal-pad" testID="start-cash" />
              <TouchableOpacity style={styles.primaryBtn} onPress={openShift} testID="confirm-open-shift">
                <Text style={styles.primaryBtnText}>Open Shift</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* Close Shift dialog — "Actual in Drawer" */}
      <Modal visible={closeDlg} transparent animationType="fade" onRequestClose={() => setCloseDlg(false)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setCloseDlg(false)}><Ionicons name="close" size={24} color="#EF4444" /></TouchableOpacity>
              <Text style={styles.modalTitle}>Actual in Drawer</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <TextInput
                style={[styles.formInput, { fontSize: 18 }]}
                value={actualCash}
                onChangeText={setActualCash}
                keyboardType="decimal-pad"
                placeholder="0.00"
                selectTextOnFocus
                testID="actual-cash"
              />
              <TouchableOpacity style={styles.closeShiftBtn} onPress={closeShift} testID="confirm-close-shift">
                <Ionicons name="save-outline" size={18} color="#FFFFFF" style={{ marginRight: 6 }} />
                <Text style={styles.closeShiftText}>CLOSE SHIFT</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* Paid-In/Out dialog — Amount, Category, Description */}
      <Modal visible={!!moveDlg} transparent animationType="fade" onRequestClose={closeMoveDlg}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={closeMoveDlg}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>{moveDlg === "paid_in" ? "Paid In" : "Paid Out"}</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <View style={styles.moveRow}>
                <Text style={styles.moveRowLabel}>Amount</Text>
                <TextInput
                  style={styles.moveRowInput}
                  value={moveAmt}
                  onChangeText={setMoveAmt}
                  keyboardType="decimal-pad"
                  placeholder="0.00"
                  textAlign="right"
                  testID="move-amt"
                />
              </View>
              <TouchableOpacity style={styles.moveRow} onPress={() => setShowCatPicker(true)} testID="move-cat">
                <Text style={styles.moveRowLabel}>Category</Text>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 4 }}>
                  <Text style={[styles.moveRowValue, !moveCat && { color: "#94A3B8" }]}>
                    {moveCat || "Choose category"}
                  </Text>
                  <Ionicons name="chevron-forward" size={16} color="#94A3B8" />
                </View>
              </TouchableOpacity>
              <Text style={styles.formLabel}>Description</Text>
              <TextInput
                style={[styles.formInput, { height: 96, textAlignVertical: "top" }]}
                value={moveNote}
                onChangeText={setMoveNote}
                placeholder=""
                multiline
                testID="move-note"
              />
              <TouchableOpacity
                style={[styles.primaryBtn, { backgroundColor: moveDlg === "paid_in" ? "#00B14F" : "#EF4444" }]}
                onPress={addMovement}
                testID="confirm-movement"
              >
                <Text style={styles.primaryBtnText}>{moveDlg === "paid_in" ? "Paid In" : "Paid Out"}</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* Category picker — slides over the Paid In/Out dialog */}
      <Modal visible={showCatPicker} transparent animationType="fade" onRequestClose={() => setShowCatPicker(false)}>
        <TouchableOpacity style={styles.modalOverlay} activeOpacity={1} onPress={() => setShowCatPicker(false)}>
          <TouchableOpacity activeOpacity={1} style={styles.smallModal}>
            <Text style={[styles.modalTitle, { textAlign: "center", paddingTop: 18 }]}>Category</Text>
            <ScrollView style={{ maxHeight: 360 }}>
              {cats.length === 0 ? (
                <Text style={[styles.emptyText, { padding: 24 }]}>No categories yet</Text>
              ) : (
                cats.map((c) => (
                  <TouchableOpacity
                    key={c.id}
                    style={styles.catRow}
                    onPress={() => { setMoveCat(c.name); setShowCatPicker(false); }}
                    testID={`cat-${c.id}`}
                  >
                    <Text style={styles.catRowText}>{c.name}</Text>
                  </TouchableOpacity>
                ))
              )}
            </ScrollView>
          </TouchableOpacity>
        </TouchableOpacity>
      </Modal>

      {/* "Printing…" indicator while the close-shift slip is captured + sent */}
      <Modal visible={printing} transparent animationType="fade">
        <View style={styles.modalOverlay}>
          <View style={styles.printingBox}>
            <ActivityIndicator color="#00B14F" size="large" />
            <Text style={styles.printingText}>Printing…</Text>
          </View>
        </View>
      </Modal>

      {/* Off-screen capture target for the printed summary */}
      <ShiftSummaryOverlay />
    </View>
  );
}

function ShiftRow({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <View style={styles.shiftRow}>
      <Text style={styles.shiftLabel}>{label}</Text>
      <Text style={[styles.shiftVal, strong && { color: "#3B82F6", fontWeight: "700", fontSize: 20 }]}>
        {value}
      </Text>
    </View>
  );
}

// =================== SHIFT HISTORY ===================
// SilomPOS-style History: left = date-range scoped, date-grouped shift list;
// right = detail card for the selected round. Buddhist-era year is shown on
// the range header (year + 543) to match the reference UI; the in-modal
// Calendar reuses the existing Gregorian picker.
const BE_OFFSET = 543;
const MONTHS_SHORT_HIST = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

const isoToDate = (iso: string) => new Date(iso + "T00:00:00");
const shiftIsoDays = (iso: string, delta: number) => {
  const d = isoToDate(iso);
  d.setDate(d.getDate() + delta);
  return fmtISO(d);
};
const fmtRangeLabel = (r: DateRange) => {
  if (!r.start) return "Select date";
  const s = isoToDate(r.start);
  const e = isoToDate(r.end || r.start);
  return `${s.getDate()} ${MONTHS_SHORT_HIST[s.getMonth()]} ${s.getFullYear() + BE_OFFSET} - ` +
    `${e.getDate()} ${MONTHS_SHORT_HIST[e.getMonth()]} ${e.getFullYear() + BE_OFFSET}`;
};
const fmtTimeOfDay = (iso?: string) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
};

function ShiftHistory({
  shifts,
  isWide,
  onReprint,
  fmtDT,
}: {
  shifts: ShiftType[];
  isWide: boolean;
  onReprint: (id: string) => void;
  fmtDT: (iso?: string) => string;
}) {
  const initialRange = useMemo<DateRange>(() => {
    const today = new Date();
    const start = new Date(today);
    start.setDate(start.getDate() - 9);
    return { start: fmtISO(start), end: fmtISO(today) };
  }, []);
  const [range, setRange] = useState<DateRange>(initialRange);
  const [showCalendar, setShowCalendar] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Filter to the chosen range; group by closed_at date (or opened_at if open).
  const grouped = useMemo(() => {
    const fromMs = isoToDate(range.start).getTime();
    const toMs = isoToDate(range.end || range.start).getTime() + 24 * 60 * 60 * 1000 - 1;
    const byDate = new Map<string, ShiftType[]>();
    for (const s of shifts) {
      const ref = s.closed_at || s.opened_at;
      const ts = new Date(ref).getTime();
      if (ts < fromMs || ts > toMs) continue;
      const d = new Date(ref);
      const key = fmtISO(d);
      const arr = byDate.get(key) || [];
      arr.push(s);
      byDate.set(key, arr);
    }
    return Array.from(byDate.entries())
      .map(([date, rows]) => ({
        date,
        rows: rows.sort((a, b) =>
          new Date(b.closed_at || b.opened_at).getTime() -
          new Date(a.closed_at || a.opened_at).getTime(),
        ),
      }))
      .sort((a, b) => (a.date < b.date ? 1 : -1));
  }, [shifts, range]);

  const flat = useMemo(() => grouped.flatMap((g) => g.rows), [grouped]);
  const selected = useMemo(
    () => flat.find((s) => s.id === selectedId) || flat[0] || null,
    [flat, selectedId],
  );

  const shiftRange = (delta: number) => {
    setRange((r) => ({
      start: shiftIsoDays(r.start, delta),
      end: shiftIsoDays(r.end || r.start, delta),
    }));
  };

  const detailCard = selected ? (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 12 }}>
      <View style={styles.histCard}>
        <ShiftRow label="Round" value={String(selected.round_number)} strong />
        <ShiftRow label="Shift opened" value={fmtDT(selected.opened_at)} />
        <ShiftRow label="Shift opened by" value={selected.opened_by || "-"} />
        <ShiftRow label="Shift closed" value={fmtDT(selected.closed_at)} />
        <ShiftRow label="Shift closed by" value={selected.closed_by || "-"} />
      </View>
      <View style={styles.histCard}>
        <ShiftRow label="Total Sales (cash)" value={(selected.total_sales_cash || 0).toFixed(2)} />
        <ShiftRow label="Start Drawer" value={(selected.start_cash || 0).toFixed(2)} />
        <View style={styles.shiftRow}>
          <Text style={styles.shiftLabel}>Paid In</Text>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <Text style={styles.shiftVal}>{(selected.total_paid_in || 0).toFixed(2)}</Text>
            <Ionicons name="chevron-forward" size={14} color="#94A3B8" />
          </View>
        </View>
        <View style={styles.shiftRow}>
          <Text style={styles.shiftLabel}>Paid Out</Text>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <Text style={[styles.shiftVal, { color: "#EF4444" }]}>{(selected.total_paid_out || 0).toFixed(2)}</Text>
            <Ionicons name="chevron-forward" size={14} color="#94A3B8" />
          </View>
        </View>
        <ShiftRow label="Actual in Drawer" value={(selected.actual_in_drawer ?? 0).toFixed(2)} />
        <ShiftRow label="Expected in Drawer" value={(selected.expected_in_drawer || 0).toFixed(2)} />
        <ShiftRow
          label="Difference"
          value={((selected.actual_in_drawer ?? 0) - (selected.expected_in_drawer || 0)).toFixed(2)}
        />
      </View>
      {selected.status !== "open" && (
        <TouchableOpacity
          style={styles.histReprintBtn}
          onPress={() => onReprint(selected.id)}
          testID={`shift-reprint-${selected.id}`}
        >
          <Ionicons name="print-outline" size={18} color="#00B14F" />
          <Text style={styles.histReprintText}>Reprint summary</Text>
        </TouchableOpacity>
      )}
    </ScrollView>
  ) : (
    <View style={styles.emptyBox}>
      <Ionicons name="time-outline" size={40} color="#CBD5E1" />
      <Text style={styles.emptyText}>Pick a shift to see details</Text>
    </View>
  );

  const listPanel = (
    <View style={styles.histListPanel}>
      <View style={styles.histRangeRow}>
        <TouchableOpacity onPress={() => shiftRange(-10)} testID="hist-range-prev">
          <Ionicons name="chevron-back" size={20} color="#00B14F" />
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.histRangeLabelBtn}
          onPress={() => setShowCalendar(true)}
          testID="hist-range-open"
        >
          <Text style={styles.histRangeLabel}>{fmtRangeLabel(range)}</Text>
        </TouchableOpacity>
        <TouchableOpacity onPress={() => shiftRange(10)} testID="hist-range-next">
          <Ionicons name="chevron-forward" size={20} color="#00B14F" />
        </TouchableOpacity>
      </View>
      <ScrollView contentContainerStyle={{ paddingBottom: 16 }}>
        {grouped.length === 0 ? (
          <View style={styles.emptyBox}>
            <Text style={styles.emptyText}>No shifts in this range</Text>
          </View>
        ) : (
          grouped.map((g) => (
            <View key={g.date}>
              <Text style={styles.histDateHeader}>{g.date}</Text>
              {g.rows.map((row) => {
                const active = selected?.id === row.id;
                return (
                  <TouchableOpacity
                    key={row.id}
                    style={[styles.histListRow, active && styles.histListRowActive]}
                    onPress={() => setSelectedId(row.id)}
                    testID={`shift-hist-${row.id}`}
                  >
                    <View style={{ flex: 1 }}>
                      <Text style={styles.histListRound}>Round {row.round_number}</Text>
                      <Text style={styles.histListSub}>
                        End Drawer: {fmtTimeOfDay(row.closed_at || row.opened_at)}
                      </Text>
                    </View>
                    <Ionicons name="chevron-forward" size={18} color="#94A3B8" />
                  </TouchableOpacity>
                );
              })}
            </View>
          ))
        )}
      </ScrollView>
    </View>
  );

  return (
    <View style={{ flex: 1, flexDirection: isWide ? "row" : "column" }}>
      <View style={isWide ? styles.histLeftWide : { maxHeight: 280 }}>
        {listPanel}
      </View>
      <View style={isWide ? styles.histRightWide : { flex: 1 }}>
        {detailCard}
      </View>

      <DateRangeModal
        visible={showCalendar}
        initial={range}
        onClose={() => setShowCalendar(false)}
        onApply={(r) => {
          setRange(r);
          setShowCalendar(false);
        }}
      />
    </View>
  );
}

// =================== OLD DRAWER (removed below, see new one above) ===================

// =================== SETTINGS ===================
// Shows THIS tablet's branch self-ordering link + QR so staff can display it
// for customers to scan.  The URL is derived from the backend host + the active
// branch id, so each branch/tablet shows its own — no configuration needed.
function SelfOrderQrView({ branchId, branchName }: { branchId: string; branchName: string }) {
  const url = branchId ? `${SELF_ORDER_BASE}/${branchId}/` : "";
  const qr = useMemo(() => (url ? makeQrDataUrl(url) : null), [url]);

  const share = useCallback(async () => {
    if (!url) return;
    try {
      await Share.share({ message: url });
    } catch {
      /* user dismissed the share sheet */
    }
  }, [url]);

  if (!branchId) {
    return (
      <ScrollView contentContainerStyle={{ padding: 20 }}>
        <Text style={styles.h2}>Self-Order QR</Text>
        <Text style={{ color: "#64748B", marginTop: 8 }}>
          No active branch on this device. Log in to a branch to see its QR.
        </Text>
      </ScrollView>
    );
  }

  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 16, alignItems: "center" }}>
      <Text style={[styles.h2, { alignSelf: "flex-start" }]}>Self-Order QR</Text>
      <Text style={{ color: "#475569", alignSelf: "flex-start", marginTop: -6 }}>
        Customers scan this to order &amp; pay from their phone for{" "}
        <Text style={{ fontWeight: "700" }}>{branchName || "this branch"}</Text>.
      </Text>

      <View
        style={{
          backgroundColor: "#fff",
          borderRadius: 20,
          padding: 20,
          marginTop: 8,
          alignItems: "center",
          borderWidth: 1,
          borderColor: "#EEE",
          shadowColor: "#000",
          shadowOpacity: 0.06,
          shadowRadius: 12,
          shadowOffset: { width: 0, height: 4 },
          elevation: 2,
        }}
      >
        {qr ? (
          <Image source={{ uri: qr }} style={{ width: 240, height: 240 }} resizeMode="contain" />
        ) : (
          <View style={{ width: 240, height: 240, alignItems: "center", justifyContent: "center" }}>
            <Text style={{ color: "#94A3B8" }}>Couldn’t render QR</Text>
          </View>
        )}
        <Text style={{ fontWeight: "800", fontSize: 16, marginTop: 12, color: "#0F172A" }}>
          {branchName || "Self Order"}
        </Text>
      </View>

      <View
        style={{
          backgroundColor: "#F8FAFC",
          borderRadius: 12,
          padding: 12,
          width: "100%",
          maxWidth: 360,
          borderWidth: 1,
          borderColor: "#E2E8F0",
        }}
      >
        <Text style={{ color: "#64748B", fontSize: 12, marginBottom: 4 }}>Link</Text>
        <Text selectable style={{ color: "#0F172A", fontSize: 13 }}>{url}</Text>
      </View>

      <View style={{ flexDirection: "row", gap: 12, marginTop: 4 }}>
        <TouchableOpacity
          onPress={share}
          style={{
            flexDirection: "row", alignItems: "center", gap: 8,
            backgroundColor: "#D61222", paddingVertical: 12, paddingHorizontal: 22,
            borderRadius: 12,
          }}
        >
          <Ionicons name="share-outline" size={18} color="#fff" />
          <Text style={{ color: "#fff", fontWeight: "700" }}>Share link</Text>
        </TouchableOpacity>
        <TouchableOpacity
          onPress={() => Linking.openURL(url)}
          style={{
            flexDirection: "row", alignItems: "center", gap: 8,
            backgroundColor: "#fff", paddingVertical: 12, paddingHorizontal: 22,
            borderRadius: 12, borderWidth: 1.5, borderColor: "#D61222",
          }}
        >
          <Ionicons name="open-outline" size={18} color="#D61222" />
          <Text style={{ color: "#D61222", fontWeight: "700" }}>Open</Text>
        </TouchableOpacity>
      </View>

      <Text style={{ color: "#94A3B8", fontSize: 12, textAlign: "center", maxWidth: 340, marginTop: 4 }}>
        Print this and place it on the table or counter. Each branch shows its own link automatically.
      </Text>
    </ScrollView>
  );
}

function SettingsView({ isWide, branchId, branchName }: { isWide: boolean; branchId: string; branchName: string }) {
  const sections: { name: string; icon: any; color: string }[] = [
    { name: "Shop", icon: "home", color: "#EF4444" },
    { name: "Floor plan", icon: "grid", color: "#3B82F6" },
    { name: "Language", icon: "language", color: "#8B5CF6" },
    { name: "Receipt", icon: "receipt", color: "#EF4444" },
    { name: "Payment", icon: "card", color: "#F59E0B" },
    { name: "Self-Order QR", icon: "qr-code", color: "#D61222" },
    { name: "Drawer", icon: "calculator", color: "#06B6D4" },
    { name: "Sales channels", icon: "link", color: "#EC4899" },
    { name: "Printers", icon: "print", color: "#10B981" },
    { name: "Customer display", icon: "tv", color: "#3B82F6" },
    { name: "Users", icon: "person", color: "#F97316" },
    { name: "Advance Settings", icon: "settings", color: "#64748B" },
    { name: "Backup & Restore", icon: "cloud-upload", color: "#A855F7" },
    { name: "Data synchronization", icon: "sync", color: "#06B6D4" },
    { name: "CRM System", icon: "star", color: "#EAB308" },
    { name: "Plugins", icon: "extension-puzzle", color: "#14B8A6" },
  ];
  const [active, setActive] = useState("Shop");
  const [s, setS] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  // Narrow phones can't show the list + the detail side by side (leftNav is
  // 280px wide, leaving ~95px for the detail pane).  Use a master-detail
  // pattern: list first, then drill into a section.
  const [drilled, setDrilled] = useState(false);
  const showDetail = isWide || drilled;
  const showList = isWide || !drilled;

  useEffect(() => {
    apiFetch(`${API}/settings`).then((r) => r.json()).then(setS);
  }, []);

  const update = (patch: Partial<Settings>) => setS((c) => (c ? { ...c, ...patch } : c));

  const save = async () => {
    if (!s) return;
    setSaving(true);
    try {
      const res = await apiFetch(`${API}/settings`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        Alert.alert("Save failed", `Settings did not save (HTTP ${res.status}). ${detail.slice(0, 200)}`);
        return;
      }
      // Refresh from the server response so saved (and masked) values are
      // reflected immediately — confirms the write actually landed.
      const saved = await res.json().catch(() => null);
      if (saved) setS(saved);
    } catch (e: any) {
      Alert.alert("Save failed", e?.message || "Could not reach the server.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <View style={styles.twoCol} testID="settings-section">
      {showList && (
        <View style={[styles.leftNav, !isWide && styles.fullCol]}>
          <Text style={styles.sectionHeader}>Settings</Text>
          <ScrollView>
            {sections.map((sec) => (
              <TouchableOpacity
                key={sec.name}
                style={[styles.settingsRow, isWide && active === sec.name && styles.leftNavRowActive]}
                onPress={() => { setActive(sec.name); setDrilled(true); }}
                testID={`settings-${sec.name}`}
              >
                <Ionicons name={sec.icon} size={18} color={sec.color} style={{ marginRight: 10 }} />
                <Text style={[styles.settingsLabel, { flex: 1 }, isWide && active === sec.name && { color: "#00B14F", fontWeight: "700" }]}>
                  {sec.name}
                </Text>
                <Ionicons name="chevron-forward" size={14} color="#94A3B8" />
              </TouchableOpacity>
            ))}
          </ScrollView>
        </View>
      )}
      {showDetail && (
      <View style={{ flex: 1 }}>
        {!isWide && (
          <TouchableOpacity
            style={styles.backRow}
            onPress={() => setDrilled(false)}
            testID="settings-back"
          >
            <Ionicons name="chevron-back" size={18} color="#00B14F" />
            <Text style={styles.backText}>Settings</Text>
          </TouchableOpacity>
        )}
        {active === "Self-Order QR" ? (
          <SelfOrderQrView branchId={branchId} branchName={branchName} />
        ) : active === "Shop" && s ? (
          <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
            <Text style={styles.h2}>Shop</Text>
            <Field label="Shop Name">
              <TextInput style={styles.formInput} value={s.shop_name} onChangeText={(v) => update({ shop_name: v })} testID="set-shop-name" />
            </Field>
            <Field label="Business Type">
              <View style={{ flexDirection: "row", gap: 8 }}>
                {["General", "Restaurant", "Hostel"].map((bt) => (
                  <TouchableOpacity
                    key={bt}
                    style={[styles.bizBtn, s.business_type === bt && styles.bizBtnActive]}
                    onPress={() => update({ business_type: bt })}
                    testID={`biz-${bt}`}
                  >
                    <Text style={[styles.bizBtnText, s.business_type === bt && { color: "#FFF" }]}>{bt}</Text>
                  </TouchableOpacity>
                ))}
              </View>
            </Field>
            <View style={{ flexDirection: "row", gap: 12 }}>
              <Field label="Tax ID" flex>
                <TextInput style={styles.formInput} value={s.tax_id || ""} onChangeText={(v) => update({ tax_id: v })} placeholder="—" />
              </Field>
              <Field label="POS ID" flex>
                <TextInput style={styles.formInput} value={s.pos_id} onChangeText={(v) => update({ pos_id: v })} />
              </Field>
              <Field label="Branch" flex>
                <TextInput style={styles.formInput} value={s.branch} onChangeText={(v) => update({ branch: v })} />
              </Field>
              <Field label="POS #" flex>
                <TextInput style={styles.formInput} value={s.pos_number} onChangeText={(v) => update({ pos_number: v })} />
              </Field>
            </View>
            <View style={{ flexDirection: "row", gap: 12 }}>
              <Field label="Open time" flex>
                <TextInput style={styles.formInput} value={s.open_time} onChangeText={(v) => update({ open_time: v })} />
              </Field>
              <Field label="Close time" flex>
                <TextInput style={styles.formInput} value={s.close_time} onChangeText={(v) => update({ close_time: v })} />
              </Field>
            </View>
            <Field label={`Tax ${s.tax_percent}%`}>
              <View style={{ flexDirection: "row", gap: 8 }}>
                {["exclusive", "inclusive"].map((m) => (
                  <TouchableOpacity
                    key={m}
                    style={[styles.bizBtn, s.tax_mode === m && styles.bizBtnActive]}
                    onPress={() => update({ tax_mode: m })}
                  >
                    <Text style={[styles.bizBtnText, s.tax_mode === m && { color: "#FFF" }]}>{m}</Text>
                  </TouchableOpacity>
                ))}
                <TextInput
                  style={[styles.formInput, { width: 80 }]}
                  value={String(s.tax_percent)}
                  onChangeText={(v) => update({ tax_percent: parseFloat(v) || 0 })}
                  keyboardType="decimal-pad"
                  testID="tax-pct"
                />
              </View>
            </Field>
            <Field label="Service charge">
              <View style={{ flexDirection: "row", gap: 8, alignItems: "center" }}>
                <TouchableOpacity
                  style={[styles.toggleBox, s.service_charge_enabled && styles.toggleBoxOn]}
                  onPress={() => update({ service_charge_enabled: !s.service_charge_enabled })}
                  testID="svc-toggle"
                >
                  <View
                    style={[styles.toggleKnob, s.service_charge_enabled && styles.toggleKnobOn]}
                  />
                </TouchableOpacity>
                <Text>{s.service_charge_enabled ? "Enabled" : "Disabled"}</Text>
                {s.service_charge_enabled && (
                  <TextInput
                    style={[styles.formInput, { width: 80 }]}
                    value={String(s.service_charge_percent)}
                    onChangeText={(v) => update({ service_charge_percent: parseFloat(v) || 0 })}
                    keyboardType="decimal-pad"
                  />
                )}
              </View>
            </Field>
            <TouchableOpacity
              style={[styles.primaryBtn, saving && { opacity: 0.5 }]}
              onPress={save}
              disabled={saving}
              testID="settings-save"
            >
              <Text style={styles.primaryBtnText}>{saving ? "Saving…" : "Save Settings"}</Text>
            </TouchableOpacity>
          </ScrollView>
        ) : active === "Payment" && s ? (
          (() => {
            // Normalize Beam-related settings once so the JSX stays clean.
            const isMaskedKey = s.beam_api_key?.startsWith(BEAM_API_KEY_MASK_PREFIX) ?? false;
            const sandbox = s.beam_sandbox ?? true;
            return (
          <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
            <Text style={styles.h2}>Payment</Text>

            {/* ── Beam QR Payment ── */}
            <View style={styles.beamSettingsCard}>
              <View style={styles.beamSettingsHeader}>
                <View style={styles.beamLogoBox}>
                  <Ionicons name="qr-code" size={20} color="#FFF" />
                </View>
                <View style={{ flex: 1 }}>
                  <Text style={styles.beamSettingsTitle}>Beam Payment</Text>
                  <Text style={styles.beamSettingsSub}>PromptPay QR + credit card via Beam Checkout</Text>
                </View>
              </View>

              <Field label="Merchant ID">
                <TextInput
                  style={styles.formInput}
                  value={s.beam_merchant_id || ""}
                  onChangeText={(v) => update({ beam_merchant_id: v })}
                  placeholder="m_xxxxxxxxxxxxxxxx"
                  autoCapitalize="none"
                  autoCorrect={false}
                  testID="beam-merchant-id"
                />
              </Field>

              <Field label="API Key">
                <TextInput
                  style={styles.formInput}
                  value={isMaskedKey ? "" : (s.beam_api_key || "")}
                  onChangeText={(v) => update({ beam_api_key: v })}
                  placeholder={isMaskedKey ? "Key saved — enter new key to replace" : "Enter your Beam API Key"}
                  autoCapitalize="none"
                  autoCorrect={false}
                  testID="beam-api-key"
                />
              </Field>

              <Field label="Mode">
                <View style={{ flexDirection: "row", gap: 8 }}>
                  <TouchableOpacity
                    style={[styles.bizBtn, sandbox && styles.bizBtnActive]}
                    onPress={() => update({ beam_sandbox: true })}
                    testID="beam-sandbox"
                  >
                    <Text style={[styles.bizBtnText, sandbox && { color: "#FFF" }]}>Test (Playground)</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.bizBtn, !sandbox && styles.bizBtnActive]}
                    onPress={() => update({ beam_sandbox: false })}
                    testID="beam-production"
                  >
                    <Text style={[styles.bizBtnText, !sandbox && { color: "#FFF" }]}>Production</Text>
                  </TouchableOpacity>
                </View>
                <Text style={styles.beamSettingsHint}>
                  Use Test mode with Beam Playground credentials. Switch to Production when you are ready to accept real payments.
                </Text>
              </Field>

              <Field label="Card Processing Fee %">
                <TextInput
                  style={styles.formInput}
                  // Uncontrolled (defaultValue) so a half-typed decimal like
                  // "3." isn't reformatted away mid-keystroke.
                  defaultValue={s.beam_card_fee_percent != null ? String(s.beam_card_fee_percent) : ""}
                  onChangeText={(v) => {
                    const n = parseFloat(v.replace(/[^0-9.]/g, ""));
                    update({ beam_card_fee_percent: isNaN(n) ? 0 : n });
                  }}
                  placeholder="3.65"
                  keyboardType="decimal-pad"
                  testID="beam-card-fee-percent"
                />
                <Text style={styles.beamSettingsHint}>
                  Charged to the customer on the "Beam Card" payment method (percentage of the order total). 7% VAT is added on top of this fee. Test/live mode follows the Mode setting above.
                </Text>
              </Field>
            </View>

            {/* ── Omise Credit Card ── */}
            {(() => {
              const isMaskedOmise = s.omise_secret_key?.startsWith(BEAM_API_KEY_MASK_PREFIX) ?? false;
              return (
            <View style={styles.beamSettingsCard}>
              <View style={styles.beamSettingsHeader}>
                <View style={[styles.beamLogoBox, { backgroundColor: "#1A1F71" }]}>
                  <Ionicons name="card" size={20} color="#FFF" />
                </View>
                <View style={{ flex: 1 }}>
                  <Text style={styles.beamSettingsTitle}>Credit Card (Omise)</Text>
                  <Text style={styles.beamSettingsSub}>Card payment link · customer scans a QR to pay</Text>
                </View>
              </View>

              <Field label="Public Key">
                <TextInput
                  style={styles.formInput}
                  value={s.omise_public_key || ""}
                  onChangeText={(v) => update({ omise_public_key: v })}
                  placeholder="pkey_test_xxxxxxxxxxxxxxxx"
                  autoCapitalize="none"
                  autoCorrect={false}
                  testID="omise-public-key"
                />
              </Field>

              <Field label="Secret Key">
                <TextInput
                  style={styles.formInput}
                  value={isMaskedOmise ? "" : (s.omise_secret_key || "")}
                  onChangeText={(v) => update({ omise_secret_key: v })}
                  placeholder={isMaskedOmise ? "Key saved — enter new key to replace" : "skey_test_xxxxxxxxxxxxxxxx"}
                  autoCapitalize="none"
                  autoCorrect={false}
                  testID="omise-secret-key"
                />
              </Field>

              <Field label="Processing Fee %">
                <TextInput
                  style={styles.formInput}
                  // Uncontrolled (defaultValue, not value) so a half-typed
                  // decimal like "3." isn't reformatted away mid-keystroke.
                  defaultValue={s.omise_fee_percent != null ? String(s.omise_fee_percent) : ""}
                  onChangeText={(v) => {
                    const n = parseFloat(v.replace(/[^0-9.]/g, ""));
                    update({ omise_fee_percent: isNaN(n) ? 0 : n });
                  }}
                  placeholder="3.65"
                  keyboardType="decimal-pad"
                  testID="omise-fee-percent"
                />
                <Text style={styles.beamSettingsHint}>
                  Charged to the customer on card payments (percentage of the order total). 7% VAT is added on top of this fee. Test/live mode is set by the key prefix (skey_test_ vs skey_).
                </Text>
              </Field>
            </View>
              );
            })()}

            <TouchableOpacity
              style={[styles.primaryBtn, saving && { opacity: 0.5 }]}
              onPress={save}
              disabled={saving}
              testID="settings-save-payment"
            >
              <Text style={styles.primaryBtnText}>{saving ? "Saving…" : "Save Settings"}</Text>
            </TouchableOpacity>
          </ScrollView>
            );
          })()
        ) : active === "Printers" && s ? (
          <PrintersSection s={s} update={update} save={save} saving={saving} />
        ) : active === "Drawer" ? (
          <DrawerCategoriesSection />
        ) : (
          <View style={styles.emptyBox}>
            <Ionicons name="construct-outline" size={40} color="#CBD5E1" />
            <Text style={styles.emptyText}>{active}</Text>
            <Text style={[styles.emptyText, { color: "#CBD5E1" }]}>Coming soon</Text>
          </View>
        )}
      </View>
      )}
    </View>
  );
}

function Field({ label, children, flex }: { label: string; children: any; flex?: boolean }) {
  return (
    <View style={{ gap: 6, flex: flex ? 1 : undefined }}>
      <Text style={styles.formLabel}>{label}</Text>
      {children}
    </View>
  );
}

// ── Settings → Drawer: manage Paid In / Paid Out reason codes ──
function DrawerCategoriesSection() {
  const [type, setType] = useState<"paid_in" | "paid_out">("paid_in");
  const [rows, setRows] = useState<DrawerCat[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<DrawerCat | null>(null);
  const [name, setName] = useState("");

  const load = async (t: "paid_in" | "paid_out") => {
    setLoading(true);
    try {
      const r = await apiFetch(`${API}/shift-categories?type=${t}`);
      const data = await r.json();
      setRows(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(type); }, [type]);

  const openNew = () => { setEditing({ id: "", type, name: "" }); setName(""); };
  const openEdit = (c: DrawerCat) => { setEditing(c); setName(c.name); };

  const submit = async () => {
    const trimmed = name.trim();
    if (!editing || !trimmed) return;
    const body = JSON.stringify({ type, name: trimmed, name_th: trimmed, sort_order: rows.length });
    if (editing.id) {
      await apiFetch(`${API}/shift-categories/${editing.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed, name_th: trimmed }),
      });
    } else {
      await apiFetch(`${API}/shift-categories`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
    }
    setEditing(null); setName(""); load(type);
  };

  const remove = async (c: DrawerCat) => {
    await apiFetch(`${API}/shift-categories/${c.id}`, { method: "DELETE" });
    load(type);
  };

  return (
    <View style={{ flex: 1 }} testID="drawer-settings">
      <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
        <Text style={styles.h2}>Drawer Categories</Text>
        <View style={{ flexDirection: "row", gap: 8 }}>
          {([["paid_in", "Paid In"], ["paid_out", "Paid Out"]] as const).map(([k, label]) => (
            <TouchableOpacity
              key={k}
              style={[styles.bizBtn, type === k && styles.bizBtnActive]}
              onPress={() => setType(k)}
              testID={`drawer-cat-tab-${k}`}
            >
              <Text style={[styles.bizBtnText, type === k && { color: "#FFF" }]}>{label}</Text>
            </TouchableOpacity>
          ))}
        </View>

        {loading ? (
          <ActivityIndicator color="#00B14F" style={{ marginTop: 20 }} />
        ) : (
          rows.map((c) => (
            <View key={c.id} style={styles.moveRow} testID={`drawer-cat-row-${c.id}`}>
              <Text style={styles.moveRowValue}>{c.name}</Text>
              <View style={{ flexDirection: "row", gap: 16 }}>
                <TouchableOpacity onPress={() => openEdit(c)} testID={`drawer-cat-edit-${c.id}`}>
                  <Ionicons name="create-outline" size={20} color="#3B82F6" />
                </TouchableOpacity>
                <TouchableOpacity onPress={() => remove(c)} testID={`drawer-cat-del-${c.id}`}>
                  <Ionicons name="trash-outline" size={20} color="#EF4444" />
                </TouchableOpacity>
              </View>
            </View>
          ))
        )}

        <TouchableOpacity style={[styles.primaryBtn, { marginTop: 6 }]} onPress={openNew} testID="drawer-cat-add">
          <Ionicons name="add" size={18} color="#FFFFFF" style={{ marginRight: 4 }} />
          <Text style={styles.primaryBtnText}>Add Category</Text>
        </TouchableOpacity>
      </ScrollView>

      <Modal visible={!!editing} transparent animationType="fade" onRequestClose={() => setEditing(null)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setEditing(null)}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>{editing?.id ? "Edit Category" : "New Category"}</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>Category name</Text>
              <TextInput style={styles.formInput} value={name} onChangeText={setName} autoFocus testID="drawer-cat-name" />
              <TouchableOpacity style={styles.primaryBtn} onPress={submit} testID="drawer-cat-save">
                <Text style={styles.primaryBtnText}>Save</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

function PrintersSection({
  s,
  update,
  save,
  saving,
}: {
  s: Settings;
  update: (patch: Partial<Settings>) => void;
  save: () => Promise<void>;
  saving: boolean;
}) {
  const [status, setStatus] = useState<PrinterStatus | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string>("");
  const [editing, setEditing] = useState(false);
  const [detected, setDetected] = useState<{ path: string; writable: boolean }[]>([]);
  const [detecting, setDetecting] = useState(false);

  // ── Local (this-tablet) printer state — uses the Star SDK to talk to a USB
  //    printer attached to the tablet itself.  Stored in AsyncStorage.
  const [localCfg, setLocalCfg] = useState<PrinterConfig | null>(null);
  const [localScanning, setLocalScanning] = useState(false);
  const [localFound, setLocalFound] = useState<DiscoveredPrinter[]>([]);
  const [localTesting, setLocalTesting] = useState(false);
  const [localResult, setLocalResult] = useState<string>("");
  const [manualIp, setManualIp] = useState<string>("");
  // Live printer reachability: ping every 30s while this screen is open.
  // Drives the status pill colour/label in real time (green=online,
  // red=offline, grey=not yet known / no config).
  const livePrinter = usePrinterStatus(localCfg?.identifier);

  // List of receipts that failed to print and are sitting in the
  // AsyncStorage queue waiting for the printer to come back online.
  // useStarPrinter's drainer retries them every 30s, but the cashier
  // needs visibility into what's pending — and a way to drop a job
  // they no longer want (e.g. they reprinted manually via order hub).
  const [queuedJobs, setQueuedJobs] = useState<printerQueue.PrintJob[]>([]);

  useEffect(() => { loadLocalPrinterConfig().then(setLocalCfg); }, []);

  // Poll the queue every 5s while settings is open so the list reflects
  // drainer progress (jobs disappear as they print, attempts tick up).
  useEffect(() => {
    let stopped = false;
    const refresh = async () => {
      try {
        const jobs = await printerQueue.listJobs();
        if (!stopped) setQueuedJobs(jobs);
      } catch { /* AsyncStorage hiccups don't matter — try again next tick */ }
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  const removeQueuedJob = useCallback(async (id: string) => {
    await printerQueue.removeJob(id);
    setQueuedJobs((jobs) => jobs.filter((j) => j.id !== id));
  }, []);

  const scanLocal = useCallback(async () => {
    setLocalScanning(true);
    setLocalResult("");
    try {
      const found = await starDiscover(["Lan", "Usb"], 6000);
      setLocalFound(found);
      if (found.length === 0) {
        setLocalResult(
          "No printers found. Make sure the Epson TM-T82X is powered on and either connected to the same Wi-Fi/router as this tablet, or plugged in via USB-C.",
        );
      }
    } catch (e: any) {
      setLocalResult(`Scan failed: ${e?.message || e}`);
    } finally {
      setLocalScanning(false);
    }
  }, []);

  const selectLocal = useCallback(async (d: DiscoveredPrinter) => {
    const next: PrinterConfig = {
      enabled: true,
      interface: d.interfaceType,
      identifier: d.identifier,
      // Persist the model name from discovery so the printer card can
      // show "TM-T82X" instead of a hardcoded "USB Star Printer".
      model: d.model,
      paperWidth: localCfg?.paperWidth ?? 80,
    };
    setLocalCfg(next);
    await saveLocalPrinterConfig(next);
    setLocalResult(`Saved. Will print to ${d.identifier}`);
  }, [localCfg?.paperWidth]);

  // Manual-IP fallback when network discovery doesn't surface the printer
  // (router blocks multicast, different VLAN, etc.).  We construct the
  // Epson SDK target string directly from the user-entered IP.
  const addByIp = useCallback(async () => {
    const ip = manualIp.trim();
    const ipv4 = /^(\d{1,3}\.){3}\d{1,3}$/;
    if (!ipv4.test(ip)) {
      setLocalResult("Please enter a valid IPv4 address like 192.168.1.100");
      return;
    }
    const identifier = `TCP:${ip}`;
    const next: PrinterConfig = {
      enabled: true,
      interface: "Lan",
      identifier,
      // No discovery happened — default model name for the card label.
      model: "Epson TM-T82X",
      paperWidth: localCfg?.paperWidth ?? 80,
    };
    setLocalCfg(next);
    await saveLocalPrinterConfig(next);
    setManualIp("");
    setLocalResult(`Saved. Will print to ${identifier}`);
  }, [manualIp, localCfg?.paperWidth]);

  const toggleLocalEnabled = useCallback(async () => {
    if (!localCfg) return;
    const next = { ...localCfg, enabled: !localCfg.enabled };
    setLocalCfg(next);
    await saveLocalPrinterConfig(next);
  }, [localCfg]);

  // Per-device receipt print width.  Lower it if this printer clips the right
  // edge; undefined = full 576 (default, e.g. biohouse).
  const setPrintWidth = useCallback(async (w: number | undefined) => {
    if (!localCfg) return;
    const next = { ...localCfg, printWidth: w };
    setLocalCfg(next);
    await saveLocalPrinterConfig(next);
  }, [localCfg]);

  const runLocalTest = useCallback(async () => {
    if (!localCfg) return;
    setLocalTesting(true);
    setLocalResult("");
    try {
      const shopRes = await apiFetch(`${API}/settings`);
      const shop = shopRes.ok ? await shopRes.json() : {};
      const r = await starTestPrint(localCfg, shop);
      setLocalResult(r.ok ? "Sent. Check the printer." : `Test failed: ${(r as any).error}`);
    } catch (e: any) {
      setLocalResult(`Test failed: ${e?.message || e}`);
    } finally {
      setLocalTesting(false);
    }
  }, [localCfg]);

  const transport = (s.printer_transport ?? "disabled") as "disabled" | "file" | "network";
  const enabled = s.printer_enabled ?? false;
  const address = s.printer_address ?? "";
  const configured = transport !== "disabled";

  const refresh = useCallback(async () => {
    try {
      const r = await apiFetch(`${API}/printers/status`);
      if (r.ok) setStatus(await r.json());
    } catch {
      // network blip — keep last status
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [refresh]);

  // Re-poll right after settings save so the indicator updates immediately.
  useEffect(() => {
    if (!saving) refresh();
  }, [saving, refresh]);

  const detect = useCallback(async () => {
    setDetecting(true);
    try {
      const r = await apiFetch(`${API}/printers/detect`);
      if (r.ok) {
        const body = await r.json();
        setDetected(body.candidates || []);
      }
    } catch {
      setDetected([]);
    } finally {
      setDetecting(false);
    }
  }, []);

  // Auto-detect when entering edit mode with USB selected.
  useEffect(() => {
    if (editing && transport === "file") detect();
  }, [editing, transport, detect]);

  // When entering edit mode for the first time (transport unset), default to USB.
  useEffect(() => {
    if (editing && transport === "disabled") update({ printer_transport: "file" });
    // intentionally only depend on `editing` so we don't override user choices later
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  const runTest = async () => {
    setTesting(true);
    setTestResult("");
    try {
      const r = await apiFetch(`${API}/print-test`, { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (r.ok) setTestResult("Sent. Check the printer.");
      else setTestResult(body?.detail || `Failed (HTTP ${r.status})`);
    } catch (e: any) {
      setTestResult(`Network error: ${e?.message || e}`);
    } finally {
      setTesting(false);
      refresh();
    }
  };

  const dotColor =
    !status || status.status === "disabled"
      ? "#94A3B8"
      : status.connected
      ? "#10B981"
      : "#EF4444";
  const statusLabel = !status
    ? "Checking…"
    : status.status === "disabled"
    ? "Disabled"
    : status.connected
    ? "Connected"
    : "Offline";

  // ── List view (default) ──
  // The legacy "Printers / Receipt Printer (FILE · —) / Edit Printer"
  // section that talked to backend/printer.py was removed — we now use
  // the on-device Epson SDK printer exclusively (the Local Printer card
  // below).  The Edit form at the bottom of this file is dead code now
  // but kept in case the backend-printing path returns.
  if (!editing) {
    return (
      <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
        {/* ─── LOCAL TABLET PRINTER (Epson ePOS SDK) ─────────────────────────── */}
        <Text style={styles.h2}>Local Printer (this tablet)</Text>
        <Text style={styles.printerListMeta}>
          Connect an Epson TM-T82X to the same Wi-Fi/router as this tablet, then tap
          Scan. (USB-C is also supported if the printer is plugged in directly.)
        </Text>

        <View style={styles.printerCard}>
          <View style={styles.printerHeader}>
            <Ionicons name="phone-portrait-outline" size={22} color="#10B981" />
            <View style={{ flex: 1 }}>
              <Text style={styles.printerName}>
                {localCfg?.identifier
                  ? (localCfg.model || "Epson TM-T82X")
                  : "Not configured"}
              </Text>
              <Text style={styles.printerSub} numberOfLines={1}>
                {localCfg?.identifier ? localCfg.identifier : "Tap Scan to find printer"}
              </Text>
            </View>
            {/* Status pill reflects three things in order of priority:
                1. No config       → grey "Off"
                2. Config disabled → grey "Disabled"
                3. Live ping (online/offline checked every 30s):
                     online      → green "Online"
                     offline     → red   "Offline"
                     unknown yet → amber "Checking…" */}
            {(() => {
              const noConfig = !localCfg?.identifier;
              const disabled = !!localCfg && !localCfg.enabled;
              let dotColor = "#94A3B8";
              let label: string = "Off";
              if (noConfig) {
                dotColor = "#94A3B8"; label = "Off";
              } else if (disabled) {
                dotColor = "#94A3B8"; label = "Disabled";
              } else if (livePrinter.online === true) {
                dotColor = "#10B981"; label = "Online";
              } else if (livePrinter.online === false) {
                dotColor = "#EF4444"; label = "Offline";
              } else {
                dotColor = "#F59E0B"; label = "Checking…";
              }
              return (
                <View style={styles.printerStatusPill}>
                  <View style={[styles.printerDot, { backgroundColor: dotColor }]} />
                  <Text style={styles.printerStatusText}>{label}</Text>
                </View>
              );
            })()}
          </View>
        </View>

        <View style={{ flexDirection: "row", gap: 10, flexWrap: "wrap" }}>
          <TouchableOpacity
            style={[styles.secondaryBtn, localScanning && { opacity: 0.5 }]}
            onPress={scanLocal}
            disabled={localScanning}
            testID="local-printer-scan"
          >
            <Ionicons name="search" size={16} color="#0F172A" />
            <Text style={styles.secondaryBtnText}>
              {localScanning ? "Scanning…" : "Scan"}
            </Text>
          </TouchableOpacity>
          {localCfg?.identifier && (
            <>
              <TouchableOpacity
                style={[styles.secondaryBtn, localTesting && { opacity: 0.5 }]}
                onPress={runLocalTest}
                disabled={localTesting}
                testID="local-printer-test"
              >
                <Ionicons name="document-text-outline" size={16} color="#0F172A" />
                <Text style={styles.secondaryBtnText}>
                  {localTesting ? "Sending…" : "Test Print"}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={styles.secondaryBtn}
                onPress={toggleLocalEnabled}
                testID="local-printer-toggle"
              >
                <Text style={styles.secondaryBtnText}>
                  {localCfg.enabled ? "Disable" : "Enable"}
                </Text>
              </TouchableOpacity>
            </>
          )}
        </View>

        {/* Manual IP entry — use when Scan doesn't surface the network printer
            (router blocks multicast, separate VLAN, etc.).  Find the IP from
            your router's "Connected Devices" page or the printer self-test. */}
        <View style={{ flexDirection: "row", gap: 8, alignItems: "center", marginTop: 10 }}>
          <TextInput
            style={[styles.input, { flex: 1 }]}
            placeholder="Or enter IP, e.g. 192.168.1.100"
            value={manualIp}
            onChangeText={setManualIp}
            keyboardType="numeric"
            autoCapitalize="none"
            autoCorrect={false}
            testID="local-printer-manual-ip"
          />
          <TouchableOpacity
            style={[styles.secondaryBtn, !manualIp && { opacity: 0.5 }]}
            onPress={addByIp}
            disabled={!manualIp}
            testID="local-printer-add-by-ip"
          >
            <Ionicons name="add" size={16} color="#0F172A" />
            <Text style={styles.secondaryBtnText}>Add by IP</Text>
          </TouchableOpacity>
        </View>

        {/* Receipt width — fixes right-edge clipping on printers whose head is
            narrower than the default 576 dots.  Lower it and Test Print until
            the whole receipt fits. */}
        {localCfg?.identifier && (
          <View style={{ marginTop: 14 }}>
            <Text style={styles.formLabel}>Receipt width</Text>
            <Text style={{ color: "#64748B", fontSize: 12, marginBottom: 8 }}>
              If the right side of the receipt is cut off, pick a smaller width, then Test Print. Keep lowering until it fits.
            </Text>
            <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
              {[
                { label: "Full", v: undefined as number | undefined },
                { label: "560", v: 560 },
                { label: "512", v: 512 },
                { label: "480", v: 480 },
                { label: "448", v: 448 },
              ].map((opt) => {
                const isActive = (localCfg.printWidth ?? 576) === (opt.v ?? 576);
                return (
                  <TouchableOpacity
                    key={opt.label}
                    onPress={() => setPrintWidth(opt.v)}
                    style={[
                      styles.secondaryBtn,
                      isActive && { borderColor: "#10B981", backgroundColor: "#F0FDF4" },
                    ]}
                    testID={`printwidth-${opt.label}`}
                  >
                    <Text style={styles.secondaryBtnText}>{opt.label}</Text>
                  </TouchableOpacity>
                );
              })}
            </View>
          </View>
        )}

        {localFound.length > 0 && (
          <View style={{ gap: 6, marginTop: 8 }}>
            <Text style={styles.formLabel}>Found {localFound.length} printer(s):</Text>
            {localFound.map((d) => (
              <TouchableOpacity
                key={d.identifier}
                style={[
                  styles.printerListRow,
                  localCfg?.identifier === d.identifier && {
                    borderColor: "#10B981",
                    backgroundColor: "#F0FDF4",
                  },
                ]}
                onPress={() => selectLocal(d)}
                testID={`local-printer-${d.identifier}`}
              >
                <Text style={styles.printerListName}>
                  {d.model || "Star Printer"}{" "}
                  <Text style={styles.printerListMeta}>
                    ({d.interfaceType} · {d.identifier})
                  </Text>
                </Text>
                {localCfg?.identifier === d.identifier && (
                  <Ionicons name="checkmark-circle" size={18} color="#10B981" />
                )}
              </TouchableOpacity>
            ))}
          </View>
        )}

        {localResult ? (
          <Text style={styles.printerError}>{localResult}</Text>
        ) : null}

        {/* ─── Queued receipts ────────────────────────────────────────────
            Receipts that hit the printer while it was offline get queued
            to AsyncStorage and retried every 30s by useStarPrinter.  This
            block shows the cashier exactly what's pending so they know
            nothing has been lost — and lets them drop a job if they
            already handed the customer a manual reprint. */}
        <Text style={[styles.h2, { marginTop: 8 }]}>Queued receipts</Text>
        {queuedJobs.length === 0 ? (
          <Text style={styles.printerListMeta}>
            No receipts waiting to print. New orders print immediately when the printer is online.
          </Text>
        ) : (
          <View style={{ gap: 8 }}>
            <Text style={styles.printerListMeta}>
              {queuedJobs.length} receipt{queuedJobs.length === 1 ? "" : "s"} waiting — will print automatically when the printer is back online.
            </Text>
            {queuedJobs.map((j) => {
              const ageMin = Math.max(0, Math.round((Date.now() - j.createdAt) / 60000));
              return (
                <View key={j.id} style={styles.queuedRow} testID={`queued-job-${j.order.order_number}`}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.queuedOrder}>{j.order.order_number}</Text>
                    <Text style={styles.queuedMeta}>
                      {THB(j.order.total)} · {ageMin < 1 ? "just now" : `${ageMin} min ago`} · {j.attempts} attempt{j.attempts === 1 ? "" : "s"}
                    </Text>
                    {j.lastError ? (
                      <Text style={styles.queuedError} numberOfLines={1}>{j.lastError}</Text>
                    ) : null}
                  </View>
                  <TouchableOpacity
                    style={styles.queuedRemoveBtn}
                    onPress={() => removeQueuedJob(j.id)}
                    testID={`queued-remove-${j.order.order_number}`}
                  >
                    <Ionicons name="close" size={16} color="#EF4444" />
                    <Text style={styles.queuedRemoveText}>Remove</Text>
                  </TouchableOpacity>
                </View>
              );
            })}
          </View>
        )}
      </ScrollView>
    );
  }

  // ── Edit view ── shown when the user taps the row.
  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
        <TouchableOpacity onPress={() => setEditing(false)} testID="printer-back">
          <Ionicons name="chevron-back" size={22} color="#0F172A" />
        </TouchableOpacity>
        <Text style={styles.h2}>Receipt Printer</Text>
      </View>

      {/* ── Enable toggle ── */}
      <Field label="Auto-print receipt on every order">
        <View style={{ flexDirection: "row", gap: 8, alignItems: "center" }}>
          <TouchableOpacity
            style={[styles.toggleBox, enabled && styles.toggleBoxOn]}
            onPress={() => update({ printer_enabled: !enabled })}
            testID="printer-enabled-toggle"
          >
            <View style={[styles.toggleKnob, enabled && styles.toggleKnobOn]} />
          </TouchableOpacity>
          <Text>{enabled ? "Enabled" : "Disabled"}</Text>
        </View>
      </Field>

      {/* ── Transport ── */}
      <Field label="Connection type">
        <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
          {[
            { k: "file", label: "USB" },
            { k: "network", label: "Network (TCP)" },
          ].map((t) => (
            <TouchableOpacity
              key={t.k}
              style={[styles.bizBtn, transport === t.k && styles.bizBtnActive]}
              onPress={() => update({ printer_transport: t.k as any })}
              testID={`printer-transport-${t.k}`}
            >
              <Text style={[styles.bizBtnText, transport === t.k && { color: "#FFF" }]}>
                {t.label}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      </Field>

      {/* ── USB: detected devices only (no manual path input) ── */}
      {transport === "file" && (
        <Field label="Detected printers">
          <View style={{ flexDirection: "row", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {detected.length === 0 ? (
              <Text style={styles.printerListMeta}>
                {detecting ? "Scanning…" : "No USB printers detected — plug one in"}
              </Text>
            ) : (
              detected.map((d) => (
                <TouchableOpacity
                  key={d.path}
                  style={[
                    styles.detectChip,
                    address === d.path && styles.detectChipActive,
                  ]}
                  onPress={() => update({ printer_address: d.path })}
                  testID={`detect-${d.path}`}
                >
                  <Text
                    style={[
                      styles.detectChipText,
                      address === d.path && { color: "#FFF" },
                    ]}
                  >
                    {d.path}
                  </Text>
                </TouchableOpacity>
              ))
            )}
            <TouchableOpacity
              onPress={detect}
              disabled={detecting}
              style={styles.detectRefresh}
              testID="detect-refresh"
            >
              <Ionicons name="refresh" size={14} color="#475569" />
              <Text style={styles.printerListMeta}>{detecting ? "…" : "Rescan"}</Text>
            </TouchableOpacity>
          </View>
        </Field>
      )}

      {/* ── Network: address still needed (can't auto-detect) ── */}
      {transport === "network" && (
        <Field label="Host:port (e.g. 192.168.1.50:9100)">
          <TextInput
            style={styles.formInput}
            value={address}
            onChangeText={(v) => update({ printer_address: v })}
            placeholder="192.168.1.50:9100"
            autoCapitalize="none"
            autoCorrect={false}
            testID="printer-address"
          />
        </Field>
      )}

      {/* ── Actions ── */}
      <View style={{ flexDirection: "row", gap: 10, flexWrap: "wrap", marginTop: 6 }}>
        <TouchableOpacity
          style={[styles.primaryBtn, saving && { opacity: 0.5 }]}
          onPress={async () => {
            await save();
            setEditing(false);
          }}
          disabled={saving}
          testID="printer-save"
        >
          <Text style={styles.primaryBtnText}>{saving ? "Saving…" : "Save Printer Settings"}</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[styles.secondaryBtn, (testing || transport === "disabled") && { opacity: 0.5 }]}
          onPress={runTest}
          disabled={testing || transport === "disabled"}
          testID="printer-test"
        >
          <Ionicons name="document-text-outline" size={16} color="#0F172A" />
          <Text style={styles.secondaryBtnText}>{testing ? "Sending…" : "Test Print"}</Text>
        </TouchableOpacity>
      </View>
      {testResult ? <Text style={styles.printerError}>{testResult}</Text> : null}
    </ScrollView>
  );
}

// =================== STYLES ===================
const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#F1F5F9" },
  rootRow: { flex: 1, flexDirection: "row" },

  // Sidebar — direct child of a flexDirection: "row" container in both
  // desktop and modal layouts.  width:220 sets the fixed column width;
  // cross-axis stretch gives it the full row height (no flex needed).
  sidebar: {
    width: 220,
    backgroundColor: "#FFFFFF",
    borderRightWidth: 1,
    borderRightColor: "#E2E8F0",
    paddingVertical: 20,
    paddingHorizontal: 12,
  },
  avatarBox: { alignItems: "center", marginBottom: 20 },
  avatarCircle: {
    width: 56, height: 56, borderRadius: 28,
    borderWidth: 2, borderColor: "#CBD5E1",
    alignItems: "center", justifyContent: "center",
    backgroundColor: "#F1F5F9",
  },
  avatarText: { fontSize: 14, color: "#475569", marginTop: 6, fontWeight: "600" },
  sideBranchChip: {
    flexDirection: "row", alignItems: "center", gap: 4,
    marginTop: 6, paddingHorizontal: 8, paddingVertical: 4,
    backgroundColor: "#E5F7ED", borderRadius: 999, maxWidth: 160,
  },
  sideBranchChipText: { fontSize: 11, color: "#00B14F", fontWeight: "600" },
  sideItem: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, borderRadius: 10, marginBottom: 4,
  },
  sideItemActive: { backgroundColor: "#E5F7ED" },
  sideLabel: { fontSize: 14, color: "#475569", fontWeight: "500" },
  sideLabelActive: { color: "#00B14F", fontWeight: "700" },
  logoutSide: { flexDirection: "row", gap: 8, padding: 12, alignItems: "center" },
  logoutSideText: { color: "#EF4444", fontSize: 13, fontWeight: "600" },
  versionText: { fontSize: 10, color: "#CBD5E1", textAlign: "center", marginTop: 4 },
  sideFooter: {
    flexDirection: "row", alignItems: "center", gap: 4,
    paddingHorizontal: 12, paddingTop: 6, paddingBottom: 2,
    borderTopWidth: 1, borderTopColor: "#F1F5F9",
  },
  sideFooterDate: { fontSize: 10, color: "#94A3B8" },

  mobileTop: {
    height: 56, backgroundColor: "#FFFFFF", flexDirection: "row",
    alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  mobileTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  mobileSidebarOverlay: { flex: 1, flexDirection: "row", backgroundColor: "rgba(15,23,42,0.4)" },

  content: { flex: 1 },

  // Text
  h1: { fontSize: 24, fontWeight: "700", color: "#0F172A", marginBottom: 12 },
  h2: { fontSize: 18, fontWeight: "700", color: "#0F172A", marginBottom: 4 },
  helperText: { color: "#94A3B8", fontSize: 13, marginBottom: 14 },
  sectionHeader: {
    fontSize: 15, fontWeight: "700", color: "#0F172A",
    padding: 14, borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },

  // Two-column layout
  twoCol: { flex: 1, flexDirection: "row" },
  stackedCol: { flexDirection: "column" },
  fullCol: { width: "100%", maxHeight: "100%", flex: 1, borderRightWidth: 0 },
  // leftNav variant used when the layout is column-stacked on narrow screens.
  // Replaces the fixed 280px width (which leaves wasted whitespace on the
  // right) with full width.  No height cap — the inner ScrollView would
  // otherwise hide categories beyond the first one, and users don't realise
  // they need to scroll inside a panel that visually looks complete.
  leftNavStacked: {
    width: "100%",
    height: "auto",
    flexShrink: 0,
    borderRightWidth: 0,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  // Compact horizontal category strip used on narrow screens for Products /
  // Inventory.  Replaces the ~600px-tall vertical rail so the FlatList of
  // products below has enough vertical space to render.
  narrowCatBar: {
    width: "100%",
    backgroundColor: "#FFFFFF",
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  catChip: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  catChipText: { fontSize: 12, color: "#0F172A", fontWeight: "600" },
  backRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    padding: 14,
    backgroundColor: "#FFFFFF",
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  backText: { color: "#00B14F", fontSize: 14, fontWeight: "600" },
  leftNav: {
    width: 280, backgroundColor: "#FFFFFF",
    borderRightWidth: 1, borderRightColor: "#E2E8F0",
  },
  leftNavRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 14, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  leftNavRowActive: { backgroundColor: "#F1F5F9" },
  leftNavText: { flex: 1, fontSize: 13, color: "#475569" },

  // Reports
  periodRow: {
    flexDirection: "row", backgroundColor: "#FFFFFF", borderRadius: 10,
    padding: 4, gap: 4, marginBottom: 16, alignSelf: "flex-start",
  },
  periodBtn: { paddingHorizontal: 16, paddingVertical: 8, borderRadius: 8 },
  periodBtnActive: { backgroundColor: "#00B14F" },
  periodText: { fontSize: 13, color: "#475569", fontWeight: "600" },
  periodTextActive: { color: "#FFFFFF" },
  kpiRow: { flexDirection: "row", gap: 12, marginBottom: 14, flexWrap: "wrap" },
  kpiCard: {
    flex: 1, minWidth: 160, backgroundColor: "#FFFFFF", padding: 16,
    borderRadius: 12, borderTopWidth: 3,
  },
  kpiHead: { flexDirection: "row", alignItems: "center", gap: 6, marginBottom: 8 },
  kpiLabel: { fontSize: 12, color: "#94A3B8", fontWeight: "600" },
  kpiValue: { fontSize: 22, fontWeight: "700", color: "#0F172A" },
  gpRow: { flexDirection: "row", gap: 12, marginBottom: 16, flexWrap: "wrap" },
  gpStat: {
    flex: 1, minWidth: 140, padding: 14, borderRadius: 10,
    backgroundColor: "#FFFFFF",
  },
  gpLabel: { fontSize: 11, color: "#94A3B8", fontWeight: "600" },
  gpValue: { fontSize: 18, color: "#0F172A", fontWeight: "700", marginTop: 4 },
  chartsRow: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  chartCard: {
    flex: 1, minWidth: 280, backgroundColor: "#FFFFFF",
    padding: 16, borderRadius: 12,
  },
  chartTitle: { fontSize: 13, fontWeight: "700", color: "#0F172A", marginBottom: 10 },
  chart: { flexDirection: "row", alignItems: "flex-end", height: 180, gap: 8 },
  barCol: { flex: 1, alignItems: "center", gap: 6 },
  bar: { width: "100%", backgroundColor: "#00B14F", borderRadius: 6 },
  barLabel: { fontSize: 10, color: "#94A3B8" },
  emptyChart: { color: "#94A3B8", fontSize: 12, padding: 20 },
  rankRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 10 },
  rankDot: { width: 8, height: 8, borderRadius: 4 },
  rankName: { flex: 1, fontSize: 12, color: "#475569" },
  rankBarBg: { width: 80, height: 6, backgroundColor: "#F1F5F9", borderRadius: 3 },
  rankBar: { height: 6, borderRadius: 3 },
  rankVal: { fontSize: 11, color: "#0F172A", fontWeight: "600", minWidth: 70, textAlign: "right" },

  // Transactions
  txList: { width: 320, backgroundColor: "#FFFFFF", borderRightWidth: 1, borderRightColor: "#E2E8F0" },
  txDetail: { flex: 1 },
  txDateChips: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, paddingBottom: 10,
  },
  txDateChip: {
    flex: 1, height: 34, borderRadius: 17,
    alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: "#E2E8F0", backgroundColor: "#FFFFFF",
  },
  txDateChipText: { fontSize: 11, color: "#0F172A", fontWeight: "600" },
  txDateChipActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  txEmpty: { padding: 24, alignItems: "center" },
  txRow: { flexDirection: "row", alignItems: "center", gap: 10, padding: 12 },
  txRowActive: { backgroundColor: "#E5F7ED" },
  txNum: { fontSize: 13, color: "#3B82F6", fontWeight: "600" },
  txTime: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  txAmount: { fontSize: 14, color: "#0F172A", fontWeight: "700" },
  txVoided: { color: "#DC2626", fontWeight: "700" },
  divider: { height: 1, backgroundColor: "#F1F5F9" },
  voidedBy: { fontSize: 13, color: "#DC2626", fontWeight: "700", marginTop: 4 },

  // ── Transaction detail (reference "Sale Transactions" layout) ──
  txDetailWrap: { flex: 1, backgroundColor: "#FFFFFF" },
  txDetailScroll: { padding: 24, paddingBottom: 32 },
  tdHeadRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "flex-start" },
  tdOrderNo: { fontSize: 26, fontWeight: "700", color: "#0F172A", flexShrink: 1 },
  tdGrand: { fontSize: 26, fontWeight: "700", color: "#0F172A", marginLeft: 12 },
  tdMeta: { fontSize: 13, color: "#94A3B8", marginTop: 4 },
  tdSectionRow: { flexDirection: "row", alignItems: "center", marginVertical: 16 },
  tdSectionLine: { flex: 1, height: 1, backgroundColor: "#E2E8F0" },
  tdSectionText: { fontSize: 13, color: "#64748B", fontWeight: "600", marginHorizontal: 12 },
  tdItemRow: { flexDirection: "row", alignItems: "center", marginBottom: 16 },
  tdItemImg: { width: 44, height: 44, borderRadius: 8, backgroundColor: "#F1F5F9" },
  tdItemImgEmpty: { alignItems: "center", justifyContent: "center" },
  tdItemMid: { flex: 1, marginLeft: 12 },
  tdItemName: { fontSize: 15, color: "#0F172A", fontWeight: "500" },
  tdItemSub: { fontSize: 12, color: "#94A3B8", marginTop: 3 },
  tdItemTotal: { fontSize: 15, color: "#0F172A", fontWeight: "600", marginLeft: 12 },
  tdTotalsBlock: { marginTop: 4 },
  tdLineRow: { flexDirection: "row", justifyContent: "flex-end", alignItems: "center", marginBottom: 8 },
  tdLineLabel: { fontSize: 14, color: "#64748B", marginRight: 24 },
  tdLineValue: { fontSize: 14, color: "#0F172A", fontWeight: "500", minWidth: 80, textAlign: "right" },
  tdLineBold: { fontWeight: "700", color: "#0F172A" },
  tdChannelRow: { flexDirection: "row", justifyContent: "flex-end" },
  tdChannelBadge: { flexDirection: "row", alignItems: "center", gap: 6 },
  tdChannelText: { fontSize: 14, color: "#0F172A", fontWeight: "500" },
  tdActionBar: { flexDirection: "row" },
  tdCancelBtn: { flex: 1, height: 60, alignItems: "center", justifyContent: "center", backgroundColor: "#9F1239" },
  tdCancelBtnDisabled: { backgroundColor: "#F9A8C4" },
  tdReprintBtn: { flex: 1, height: 60, alignItems: "center", justifyContent: "center", backgroundColor: "#15803D" },
  tdActionText: { fontSize: 16, fontWeight: "700", color: "#FFFFFF" },
  // Generic text input used by the "Add by IP" field in Local Printer.
  // Standard 40px height + rounded corners + slate border to match the
  // rest of the admin form fields.
  input: {
    height: 40,
    paddingHorizontal: 12,
    fontSize: 14,
    color: "#0F172A",
    backgroundColor: "#FFFFFF",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 10,
  },
  emptyBox: { flex: 1, alignItems: "center", justifyContent: "center", padding: 40, gap: 6 },
  emptyText: { color: "#94A3B8", fontSize: 14 },

  // Inventory
  invRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, backgroundColor: "#FFFFFF", borderRadius: 10,
    marginBottom: 8, borderWidth: 1, borderColor: "#F1F5F9",
  },
  invImg: { width: 48, height: 48, borderRadius: 8, backgroundColor: "#F1F5F9" },
  invName: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  invPrice: { fontSize: 12, color: "#00B14F", fontWeight: "700", marginTop: 2 },
  stockBox: { alignItems: "flex-end" },
  stockNum: { fontSize: 18, fontWeight: "700", color: "#00B14F" },
  stockStatus: { fontSize: 10, color: "#94A3B8" },
  nonStockText: { fontSize: 11, color: "#94A3B8", fontWeight: "600" },
  invTabs: {
    flexDirection: "row", borderTopWidth: 1, borderTopColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  invTab: {
    flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center",
    padding: 14, gap: 6,
  },
  invTabActive: { borderTopWidth: 2, borderTopColor: "#00B14F" },
  invTabText: { fontSize: 12, color: "#475569", fontWeight: "600" },
  sortRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 8,
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  sortLabel: { fontSize: 12, color: "#475569", fontWeight: "600", marginRight: 4 },
  sortTab: {
    paddingHorizontal: 14, paddingVertical: 6,
    borderRadius: 6, borderWidth: 1, borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  sortTabActive: { borderColor: "#00B14F", backgroundColor: "#FFFFFF" },
  sortTabText: { fontSize: 12, color: "#475569", fontWeight: "600" },
  sortTabTextActive: { color: "#00B14F" },

  // Customers
  custHeader: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    padding: 14, borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  addLink: { color: "#00B14F", fontSize: 14, fontWeight: "700" },
  searchBoxRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    margin: 10, padding: 10, backgroundColor: "#F1F5F9", borderRadius: 8,
  },
  searchBoxInput: { flex: 1, fontSize: 13, color: "#0F172A", ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}) },
  custAdminRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  custAdminActive: { backgroundColor: "#E5F7ED" },
  custAv: {
    width: 40, height: 40, borderRadius: 20,
    alignItems: "center", justifyContent: "center",
  },
  custAvText: { color: "#FFFFFF", fontSize: 14, fontWeight: "700" },
  custAdminName: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  custAdminPhone: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  custProfile: { alignItems: "center", padding: 20 },
  custAvLarge: {
    width: 80, height: 80, borderRadius: 40,
    alignItems: "center", justifyContent: "center",
  },
  custAvLargeText: { color: "#FFFFFF", fontSize: 32, fontWeight: "700" },
  custProfileName: { fontSize: 18, fontWeight: "700", color: "#0F172A", marginTop: 10 },
  custPoints: { fontSize: 14, color: "#0F172A", marginTop: 6 },
  statsRow: {
    flexDirection: "row", gap: 12, padding: 20,
    backgroundColor: "#FFFFFF", borderRadius: 12,
  },
  statCell: { flex: 1, alignItems: "center", gap: 4 },
  statSub: { fontSize: 10, color: "#94A3B8", marginTop: 2 },
  topBox: {
    flex: 1, backgroundColor: "#FFFFFF", padding: 14, borderRadius: 12,
    minHeight: 160,
  },
  topRow: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingVertical: 5, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  topRank: { fontSize: 11, color: "#94A3B8", fontWeight: "600", width: 22 },
  topName: { flex: 1, fontSize: 12, color: "#0F172A" },
  topValue: { fontSize: 12, fontWeight: "700", color: "#0F172A" },

  // Products Management
  allCatsLabel: {
    padding: 14, fontSize: 12, color: "#94A3B8", fontWeight: "600",
    letterSpacing: 1, textTransform: "uppercase",
  },
  catMgmtRow: {
    flexDirection: "row", alignItems: "center", gap: 10,
    padding: 14, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  catMgmtName: { fontSize: 13, color: "#0F172A", fontWeight: "500" },
  catMgmtSource: { fontSize: 10, color: "#94A3B8", marginTop: 2 },
  prodHeader: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    padding: 14, backgroundColor: "#FFFFFF",
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  addProdBtn: {
    flexDirection: "row", alignItems: "center", gap: 4,
    backgroundColor: "#00B14F", paddingHorizontal: 14, paddingVertical: 8,
    borderRadius: 8,
  },
  addProdText: { color: "#FFFFFF", fontSize: 13, fontWeight: "700" },
  prodMgmtRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, backgroundColor: "#FFFFFF",
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  prodPriceLabel: { fontSize: 12, color: "#94A3B8" },
  prodTags: { gap: 2 },
  tag: { fontSize: 10, color: "#475569" },
  editBtn: { padding: 8 },
  sortGroup: {
    flexDirection: "row",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 6,
    overflow: "hidden",
  },
  sortBtn: { paddingHorizontal: 14, paddingVertical: 6 },
  sortBtnActive: { borderWidth: 1, borderColor: "#00B14F", borderRadius: 5 },
  sortText: { fontSize: 12, color: "#94A3B8", fontWeight: "600" },
  linkText: { fontSize: 14, color: "#00B14F", fontWeight: "500" },
  linkTextBold: { fontSize: 14, color: "#00B14F", fontWeight: "700" },

  // Warning pill for ฿0 cost
  warnPill: {
    width: 16,
    height: 16,
    borderRadius: 8,
    backgroundColor: "#EF4444",
    alignItems: "center",
    justifyContent: "center",
  },
  warnPillText: { color: "#FFFFFF", fontSize: 11, fontWeight: "700" },

  // Shift
  shiftHeader: {
    fontSize: 16, fontWeight: "700", color: "#00B14F",
    padding: 16, backgroundColor: "#FFFFFF",
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
    textAlign: "center",
  },
  shiftCard: {
    backgroundColor: "#F8FAFC", borderRadius: 10,
    padding: 16, marginBottom: 14,
  },
  shiftRow: {
    flexDirection: "row", justifyContent: "space-between",
    paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  shiftLabel: { fontSize: 14, color: "#475569" },
  shiftVal: { fontSize: 14, color: "#0F172A", fontWeight: "600" },
  inOutRow: { flexDirection: "row", gap: 14, marginBottom: 14 },
  inOutBtn: {
    flex: 1, padding: 16, borderRadius: 10,
    backgroundColor: "#FFFFFF", borderWidth: 1,
    borderColor: "#E2E8F0", alignItems: "center",
  },
  inOutText: { fontSize: 15, fontWeight: "700" },
  closeShiftBtn: {
    backgroundColor: "#00B14F", padding: 18,
    borderRadius: 10, alignItems: "center", justifyContent: "center",
    flexDirection: "row",
  },
  closeShiftText: { color: "#FFFFFF", fontSize: 16, fontWeight: "700", letterSpacing: 1 },
  // Paid In / Paid Out form rows (label left, value/input right)
  moveRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    backgroundColor: "#F8FAFC", borderRadius: 10,
    paddingHorizontal: 14, paddingVertical: 12,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  moveRowLabel: { fontSize: 14, color: "#475569", fontWeight: "500" },
  moveRowValue: { fontSize: 14, color: "#0F172A", fontWeight: "600" },
  moveRowInput: { fontSize: 16, color: "#0F172A", fontWeight: "600", minWidth: 120, padding: 0 },
  // Category picker rows
  catRow: {
    paddingHorizontal: 20, paddingVertical: 16,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  catRowText: { fontSize: 15, color: "#0F172A" },
  printingBox: {
    backgroundColor: "#FFFFFF", borderRadius: 16,
    paddingVertical: 28, paddingHorizontal: 40,
    alignItems: "center", gap: 14, alignSelf: "center",
  },
  printingText: { fontSize: 15, fontWeight: "600", color: "#475569" },

  // Shift History — split list + detail
  histLeftWide: { width: 340, borderRightWidth: 1, borderRightColor: "#E2E8F0" },
  histRightWide: { flex: 1 },
  histListPanel: { flex: 1 },
  histRangeRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 12, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  histRangeLabelBtn: {
    flex: 1, alignItems: "center",
    paddingVertical: 6, paddingHorizontal: 8,
    borderRadius: 999,
  },
  histRangeLabel: { fontSize: 13, fontWeight: "600", color: "#00B14F" },
  histDateHeader: {
    fontSize: 13, fontWeight: "600", color: "#475569",
    paddingHorizontal: 16, paddingVertical: 10,
    backgroundColor: "#F8FAFC",
  },
  histListRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingHorizontal: 16, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
    backgroundColor: "#FFFFFF",
  },
  histListRowActive: { backgroundColor: "#F0FDF4" },
  histListRound: { fontSize: 14, fontWeight: "700", color: "#0F172A" },
  histListSub: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  histCard: {
    backgroundColor: "#FFFFFF", borderRadius: 12,
    borderWidth: 1, borderColor: "#F1F5F9",
    paddingHorizontal: 16, paddingVertical: 4,
  },
  histReprintBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingVertical: 10, borderRadius: 10,
    borderWidth: 1, borderColor: "#00B14F", backgroundColor: "#F0FDF4",
  },
  histReprintText: { fontSize: 13, fontWeight: "700", color: "#00B14F" },

  catPick: {
    paddingHorizontal: 10, paddingVertical: 6, borderRadius: 14,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  catPickActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  catPickText: { fontSize: 11, color: "#475569", fontWeight: "600" },

  // Product image picker
  imgThumb: {
    width: 88, height: 88, borderRadius: 12,
    borderWidth: 1, borderColor: "#E2E8F0", backgroundColor: "#F8FAFC",
    alignItems: "center", justifyContent: "center", overflow: "hidden",
  },
  imgThumbImage: { width: "100%", height: "100%" },
  imgPickBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingVertical: 12, paddingHorizontal: 14,
    borderRadius: 10, borderWidth: 1, borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  imgPickBtnText: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  imgClearText: { fontSize: 12, color: "#EF4444", textAlign: "center" },

  favToggle: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 12, backgroundColor: "#F8FAFC", borderRadius: 8,
  },

  // Settings
  settingsRow: {
    flexDirection: "row", alignItems: "center",
    paddingVertical: 12, paddingHorizontal: 14,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  settingsLabel: { fontSize: 13, color: "#475569" },
  bizBtn: {
    paddingHorizontal: 16, paddingVertical: 10, borderRadius: 8,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  bizBtnActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  bizBtnText: { fontSize: 13, fontWeight: "600", color: "#475569" },

  // Beam payment settings card
  beamSettingsCard: {
    backgroundColor: "#F8FAFC", borderRadius: 12, padding: 16, gap: 12,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  beamSettingsHeader: {
    flexDirection: "row", alignItems: "center", gap: 10,
  },
  beamLogoBox: {
    width: 36, height: 36, borderRadius: 8, backgroundColor: "#00B14F",
    alignItems: "center", justifyContent: "center",
  },
  beamSettingsTitle: { fontSize: 15, fontWeight: "700", color: "#1E293B" },
  beamSettingsSub: { fontSize: 12, color: "#64748B" },
  beamSettingsHint: { fontSize: 11, color: "#94A3B8", marginTop: 4 },
  toggleBox: {
    width: 44, height: 24, borderRadius: 12, backgroundColor: "#CBD5E1",
    padding: 2, justifyContent: "center",
  },
  toggleBoxOn: { backgroundColor: "#00B14F" },
  toggleKnob: {
    width: 20, height: 20, borderRadius: 10, backgroundColor: "#FFFFFF",
  },
  toggleKnobOn: { transform: [{ translateX: 20 }] },

  // Forms
  formLabel: { fontSize: 12, color: "#475569", fontWeight: "600" },
  formInput: {
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 8,
    padding: 12, fontSize: 14, color: "#0F172A",
    backgroundColor: "#FFFFFF",
  },
  primaryBtn: {
    backgroundColor: "#00B14F", padding: 14, borderRadius: 10,
    alignItems: "center", justifyContent: "center", flexDirection: "row", marginTop: 6,
  },
  primaryBtnText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },
  secondaryBtn: {
    flexDirection: "row", gap: 6, alignItems: "center", justifyContent: "center",
    paddingVertical: 14, paddingHorizontal: 16,
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    backgroundColor: "#FFFFFF",
  },
  secondaryBtnText: { color: "#0F172A", fontSize: 14, fontWeight: "600" },

  // Printers
  printerCard: {
    backgroundColor: "#FFFFFF", borderRadius: 12,
    borderWidth: 1, borderColor: "#E2E8F0", padding: 14, gap: 8,
  },
  printerHeader: { flexDirection: "row", alignItems: "center", gap: 10 },
  printerName: { fontSize: 15, fontWeight: "700", color: "#0F172A" },
  printerSub: { fontSize: 12, color: "#64748B", marginTop: 2 },
  printerStatusPill: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, paddingVertical: 4,
    borderRadius: 999, backgroundColor: "#F1F5F9",
  },
  printerDot: { width: 8, height: 8, borderRadius: 4 },
  printerStatusText: { fontSize: 12, fontWeight: "600", color: "#475569" },
  printerError: { fontSize: 12, color: "#EF4444", marginTop: 4 },
  printerListRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingVertical: 14, paddingHorizontal: 14,
    backgroundColor: "#FFFFFF", borderRadius: 10,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  printerListName: { flex: 1, fontSize: 14, fontWeight: "600", color: "#0F172A" },
  printerListMeta: { fontSize: 12, fontWeight: "400", color: "#64748B" },

  // Queued-receipts list (offline-print queue) — shown inside the
  // Local Printer settings page so cashiers can see what's pending.
  queuedRow: {
    flexDirection: "row", alignItems: "center", gap: 10,
    paddingVertical: 10, paddingHorizontal: 12,
    backgroundColor: "#FFFBEB", borderRadius: 10,
    borderWidth: 1, borderColor: "#FCD34D",
  },
  queuedOrder: { fontSize: 14, fontWeight: "700", color: "#0F172A" },
  queuedMeta: { fontSize: 12, color: "#64748B", marginTop: 2 },
  queuedError: { fontSize: 11, color: "#B45309", marginTop: 2 },
  queuedRemoveBtn: {
    flexDirection: "row", alignItems: "center", gap: 4,
    paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 8, borderWidth: 1, borderColor: "#FCA5A5",
    backgroundColor: "#FFFFFF",
  },
  queuedRemoveText: { fontSize: 12, color: "#EF4444", fontWeight: "600" },

  addPrinterBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 12,
  },
  addPrinterText: { color: "#00B14F", fontSize: 14, fontWeight: "600" },
  detectChip: {
    paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 999,
    borderWidth: 1, borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },
  detectChipActive: {
    backgroundColor: "#00B14F", borderColor: "#00B14F",
  },
  detectChipText: { fontSize: 12, fontWeight: "600", color: "#475569" },
  detectRefresh: {
    flexDirection: "row", alignItems: "center", gap: 4,
    paddingHorizontal: 8, paddingVertical: 6,
  },
  dangerBtn: {
    flexDirection: "row", gap: 6, alignItems: "center", justifyContent: "center",
    padding: 12, borderWidth: 1, borderColor: "#EF4444", borderRadius: 10,
  },
  typeBtn: {
    flex: 1, padding: 10, borderRadius: 8, borderWidth: 1,
    borderColor: "#E2E8F0", alignItems: "center",
  },
  typeBtnActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  typeBtnText: { fontSize: 13, fontWeight: "600", color: "#475569" },

  // Drawer / actions
  drawerAction: {
    flexDirection: "row", alignItems: "center", gap: 6,
    padding: 12, borderWidth: 1, borderColor: "#E2E8F0",
    borderRadius: 10, backgroundColor: "#FFFFFF",
  },
  drawerActionText: { fontSize: 13, color: "#0F172A", fontWeight: "600" },

  // Modals
  modalOverlay: {
    flex: 1, backgroundColor: "rgba(15,23,42,0.5)",
    alignItems: "center", justifyContent: "center", padding: 20,
  },
  modalHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    padding: 16, borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  modalTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  smallModal: {
    width: "85%", maxWidth: 440, backgroundColor: "#FFFFFF",
    borderRadius: 16, overflow: "hidden",
  },
  editModal: {
    width: "88%", maxWidth: 560, maxHeight: "88%",
    backgroundColor: "#FFFFFF", borderRadius: 16, overflow: "hidden",
  },

  // ── Reports header / Reports button ──
  reportsHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginBottom: 12 },
  reportsBtn: {
    flexDirection: "row", alignItems: "center", gap: 6,
    borderWidth: 1, borderColor: "#00B14F", borderRadius: 10,
    paddingHorizontal: 14, paddingVertical: 8, backgroundColor: "#FFFFFF",
  },
  reportsBtnText: { color: "#00B14F", fontWeight: "700", fontSize: 14 },
  backofficeBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
    marginTop: 20, borderWidth: 1, borderColor: "#00B14F", borderRadius: 12,
    paddingVertical: 14, backgroundColor: "#FFFFFF",
  },
  backofficeBtnText: { color: "#00B14F", fontWeight: "700", fontSize: 16 },
  rangeCard: {
    width: "92%", maxWidth: 420, backgroundColor: "#FFFFFF",
    borderRadius: 16, overflow: "hidden",
  },
  rangeSummary: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    backgroundColor: "#F8FAFC", borderRadius: 10, padding: 12,
  },
  rangeSummaryCol: { flex: 1, gap: 2 },
  rangeSummaryVal: { fontSize: 15, fontWeight: "700", color: "#0F172A" },
  calHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingVertical: 6 },
  calNavBtn: { padding: 8, borderRadius: 8 },
  calMonth: { fontSize: 15, fontWeight: "700", color: "#0F172A" },
  calWeekRow: { flexDirection: "row" },
  calWeekday: { flex: 1, textAlign: "center", fontSize: 11, fontWeight: "600", color: "#94A3B8", paddingVertical: 4 },
  calGrid: { flexDirection: "row", flexWrap: "wrap" },
  calCell: {
    width: `${100 / 7}%`, aspectRatio: 1, alignItems: "center", justifyContent: "center",
    marginVertical: 1,
  },
  calCellText: { fontSize: 14, color: "#0F172A" },
  calCellToday: { color: "#00B14F", fontWeight: "700" },
  calCellSel: { backgroundColor: "#00B14F", borderRadius: 8 },
  calCellTextSel: { color: "#FFFFFF", fontWeight: "700" },
  calCellInRange: { backgroundColor: "#D1FAE5" },
  chPeriodRow: {
    flexDirection: "row", flexWrap: "wrap", gap: 6, justifyContent: "center",
    paddingHorizontal: 12, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },

  // ── Full-screen document scaffolding ──
  docScreen: { flex: 1, backgroundColor: "#FFFFFF" },
  docTopBar: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 14, paddingVertical: 12,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  docBackBtn: { flexDirection: "row", alignItems: "center", width: 70 },
  docBackText: { fontSize: 15, color: "#0F172A", fontWeight: "600" },
  docTopTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  docSaveText: { fontSize: 15, color: "#00B14F", fontWeight: "700", width: 70, textAlign: "right" },

  // ── Channel report ──
  docDateNav: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 24,
    paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  docDateNavText: { fontSize: 15, fontWeight: "700", color: "#00B14F" },
  chTableHead: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  chHeadCell: { fontSize: 12, fontWeight: "700", color: "#64748B", textAlign: "center" },
  chRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  chCell: { fontSize: 13, color: "#0F172A", textAlign: "center" },
  chIcon: {
    width: 24, height: 24, borderRadius: 6, backgroundColor: "#E5F7ED",
    alignItems: "center", justifyContent: "center",
  },
  chName: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  noGpBadge: { backgroundColor: "#E5F7ED", borderRadius: 6, paddingHorizontal: 8, paddingVertical: 2 },
  noGpText: { fontSize: 11, color: "#00B14F", fontWeight: "700" },

  // ── Inventory top tab bar ──
  invTabBar: {
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9", paddingVertical: 8, backgroundColor: "#FFFFFF",
  },
  invTopTab: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 10 },
  invTopTabActive: { backgroundColor: "#EFF6FF" },
  invTopTabText: { fontSize: 14, fontWeight: "600", color: "#475569" },
  invTopTabTextActive: { color: "#2563EB", fontWeight: "700" },
  invSearchRow: {
    flexDirection: "row", alignItems: "center", gap: 8, margin: 10,
    backgroundColor: "#F1F5F9", borderRadius: 10, paddingHorizontal: 10, paddingVertical: 8,
  },
  invSearchInput: { flex: 1, fontSize: 13, color: "#0F172A", padding: 0 },

  // ── Document list ──
  docListBar: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 14, paddingVertical: 10, gap: 10,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  docDateRange: { flexDirection: "row", alignItems: "center", gap: 10 },
  docDateRangeText: { fontSize: 13, fontWeight: "700", color: "#00B14F" },
  docListTotal: { fontSize: 13, color: "#64748B" },
  createDocBtn: {
    flexDirection: "row", alignItems: "center", gap: 4,
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 8,
  },
  createDocBtnText: { color: "#0F172A", fontWeight: "600", fontSize: 13 },
  docColHead: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  docColCell: { fontSize: 12, fontWeight: "600", color: "#64748B" },
  docRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  docCell: { fontSize: 13, color: "#475569" },

  // ── Create document form ──
  docForm: { padding: 14, gap: 12, borderBottomWidth: 1, borderBottomColor: "#F1F5F9" },
  docFormRow: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  docField: { flex: 1, minWidth: 160, gap: 4 },
  docFieldLabel: { fontSize: 12, color: "#64748B", fontWeight: "600" },
  docFieldDate: { fontSize: 14, color: "#00B14F", fontWeight: "700", paddingVertical: 8 },
  docInput: {
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 8,
    paddingHorizontal: 10, paddingVertical: 8, fontSize: 14, color: "#0F172A",
  },
  adjTypeBtn: {
    paddingHorizontal: 18, paddingVertical: 8, borderRadius: 8,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  adjTypeBtnActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  adjTypeText: { fontSize: 14, fontWeight: "700", color: "#475569" },

  // ── Items table ──
  itemsHead: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  itemsHeadCell: { fontSize: 12, fontWeight: "600", color: "#64748B", textAlign: "center" },
  itemRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10, gap: 6,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  itemCell: { fontSize: 13, color: "#0F172A", textAlign: "center" },
  itemInput: {
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 6,
    paddingVertical: 6, paddingHorizontal: 6, alignItems: "center",
  },
  itemInputText: { fontSize: 13, color: "#0F172A" },
  itemsAddBar: {
    backgroundColor: "#00B14F", margin: 14, borderRadius: 8,
    paddingVertical: 14, alignItems: "center",
  },
  itemsAddBarText: { color: "#FFFFFF", fontWeight: "700", fontSize: 14 },

  // ── Create document footer ──
  docFooter: {
    flexDirection: "row", alignItems: "center", justifyContent: "flex-end", flexWrap: "wrap", gap: 18,
    paddingHorizontal: 16, paddingVertical: 10, borderTopWidth: 1, borderTopColor: "#F1F5F9",
  },
  footToggle: { flexDirection: "row", alignItems: "center", gap: 6 },
  footToggleLabel: { fontSize: 12, color: "#64748B", fontWeight: "600" },
  footStat: { alignItems: "center" },
  footStatLabel: { fontSize: 11, color: "#94A3B8" },
  footStatVal: { fontSize: 15, fontWeight: "700", color: "#0F172A" },

  // ── Product picker popup ──
  pickerOverlay: { flex: 1, backgroundColor: "rgba(15,23,42,0.4)", alignItems: "center", justifyContent: "center" },
  pickerCard: {
    width: "92%", maxWidth: 560, height: "82%",
    backgroundColor: "#FFFFFF", borderRadius: 16, overflow: "hidden",
  },
  pickerHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  pickerTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  pickerDone: { fontSize: 15, fontWeight: "700", color: "#0F172A" },
  pickerCatRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    margin: 14, marginBottom: 8, borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 12,
  },
  pickerCatText: { fontSize: 14, color: "#0F172A" },
  pickerCatList: {
    marginHorizontal: 14, marginTop: -4, marginBottom: 8,
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10, overflow: "hidden",
  },
  pickerCatItem: { paddingHorizontal: 12, paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: "#F1F5F9" },
  pickerCatItemText: { fontSize: 13, color: "#0F172A" },
  pickerSearchRow: {
    flexDirection: "row", alignItems: "center", gap: 8, marginHorizontal: 14,
    backgroundColor: "#F1F5F9", borderRadius: 10, paddingHorizontal: 10, paddingVertical: 10,
  },
  pickerSearchInput: { flex: 1, fontSize: 14, color: "#0F172A", padding: 0 },
  pickerSortRow: { flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 14, paddingVertical: 12 },
  pickerRow: {
    flexDirection: "row", alignItems: "center", gap: 12, paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  pickerImg: { width: 44, height: 44, borderRadius: 8, backgroundColor: "#F1F5F9" },
  pickerName: { fontSize: 14, fontWeight: "600", color: "#0F172A" },
  pickerBarcode: { fontSize: 12, color: "#94A3B8" },

  // ── Amount keypad ──
  keypadOverlay: { flex: 1, backgroundColor: "rgba(15,23,42,0.3)", alignItems: "center", justifyContent: "center" },
  keypadCard: { width: 300, backgroundColor: "#FFFFFF", borderRadius: 16, overflow: "hidden", paddingTop: 16 },
  keypadTitle: { fontSize: 15, fontWeight: "700", color: "#0F172A", textAlign: "center", marginBottom: 8 },
  keypadValue: {
    fontSize: 34, fontWeight: "700", color: "#0F172A", textAlign: "right",
    paddingHorizontal: 24, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  keypadGrid: { flexDirection: "row", flexWrap: "wrap" },
  keypadKey: {
    width: "33.333%", height: 60, alignItems: "center", justifyContent: "center",
    borderBottomWidth: 1, borderRightWidth: 1, borderColor: "#F1F5F9",
  },
  keypadKeyText: { fontSize: 22, fontWeight: "600", color: "#0F172A" },
  keypadDone: { backgroundColor: "#00B14F", paddingVertical: 16, alignItems: "center" },
  keypadDoneText: { color: "#FFFFFF", fontSize: 16, fontWeight: "700" },

  // ── Reconcile (adjust/check) form ──
  importBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 12,
  },
  importBtnText: { fontSize: 14, color: "#00B14F", fontWeight: "600" },
  itemCellRO: {
    fontSize: 13, color: "#64748B", textAlign: "right",
    backgroundColor: "#F8FAFC", borderRadius: 6, paddingVertical: 6, paddingHorizontal: 6,
  },

  // ── Select Documents popup ──
  loadDocsBtn: {
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 8,
  },
  loadDocsText: { fontSize: 14, fontWeight: "600", color: "#0F172A" },
  selDateRow: { flexDirection: "row", gap: 16, paddingHorizontal: 16, paddingVertical: 14 },
  selDateField: { flexDirection: "row", alignItems: "center", gap: 8 },
  selDateVal: { fontSize: 14, color: "#00B14F", fontWeight: "600" },
  selCalPop: {
    marginHorizontal: 16, marginBottom: 8, padding: 8,
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 12, backgroundColor: "#FFFFFF",
  },
});
