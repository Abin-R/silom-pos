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
  Linking,
  Share,
} from "react-native";
import qrcode from "qrcode-generator";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { useRouter, useLocalSearchParams, useFocusEffect } from "expo-router";
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
import { AppShell, TopBar, Body, WIDE } from "../components/AppShell";
import { SIDEBAR_ITEMS } from "../components/NavRail";
import {
  loadLocalPrinterConfig,
  saveLocalPrinterConfig,
} from "../lib/localPrinterConfig";
import * as printerQueue from "../lib/printerQueue";
import { apiFetch, clearAuthToken, safeJson } from "../lib/api";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { C, MONO, R } from "../lib/theme";
import { showAlert } from "../lib/dialog";
import { methodLabel } from "../lib/payments";
import {
  Btn, Col, Empty, KV, Lbl, MixRow, Money, Notice, Panel, PanelHead, Pill,
  Rank, SearchField, Spacer, Stat, TCell, THead, TRow, TText, Tag, Toggle,
} from "../lib/ui";
import { t as tr, useT, LANGUAGES } from "../lib/i18n";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
// Server-rendered backoffice (Django) lives under /backoffice/ on the same host.
const BACKOFFICE_URL = `${process.env.EXPO_PUBLIC_BACKEND_URL}/backoffice/`;
// The customer self-ordering site is served at the host root: /order/<branchId>/.
const SELF_ORDER_BASE = `${process.env.EXPO_PUBLIC_BACKEND_URL}/order`;
const AUTH_KEY = "bravepos:auth:v1";
const RAIL_KEY = "bravepos:rail-collapsed:v1";

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

type Section ="transactions" | "reports" | "inventory" | "customers" | "products" | "drawer" | "settings";

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
  vendor: string; receiver: string; note: string; reason: string;
  subtotal: number; discount: number; tax: number; total: number;
  created_by: string; created_at: string; items: StockDocItem[];
};
type ChannelRow = {
  channel: string; source: string; count: number;
  before_gp: number; gp: number; after_gp: number; has_gp: boolean;
};
type Customer = {
  id: string; name: string; phone?: string; last_visit?: string; color: string;
  // Profile + full-tax-invoice identity.  All optional: rows created before
  // these fields existed, and every customer added from the POS cart, have
  // only name/phone.
  last_name?: string;
  gender?: "male" | "female" | "unspecified" | "";
  birth_date?: string | null;
  group?: string;
  tax_id?: string;
  tax_branch?: string;
  address?: string;
  email?: string;
};
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
  discount_amount?: number; branch_name?: string; branch_pos_id?: string;
  customer_id?: string | null; customer_name?: string;
  // Buyer of record, set once a full tax invoice has been issued for this
  // bill.  Present → the form prefills from it on a re-issue.
  pos_tax_invoice?: TaxInvoiceData | null;
};

// What /orders/<id>/tax-invoice stores and returns.  ``issued_by`` /
// ``issued_at`` are stamped server-side.
type TaxInvoiceData = {
  name: string;
  tax_id: string;
  tax_branch?: string;
  address: string;
  phone?: string;
  email?: string;
  issued_by?: string;
  issued_at?: string;
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
  // No pos_id: the RD machine number is issued per till, so it lives on the
  // Branch and is edited in the backoffice branch form.  A shop-wide one made
  // every branch print the same number.
  branch: string; pos_number: string;
  open_time: string; close_time: string;
  tax_percent: number; tax_mode: string;
  service_charge_enabled: boolean; service_charge_percent: number;
  // No payment fields: gateway credentials and the test/live lane are
  // backoffice-only, and /api/settings strips them in both directions.
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
  // Must match the shell's threshold, or the rail and the page columns
  // disagree about which layout is on screen.
  const isWide = width >= WIDE;
  // Shares the Sale screen's preference key, so the rail doesn't expand again
  // the moment you cross into the back office.
  const [railPref, setRailPref] = useState<boolean | null>(null);
  useEffect(() => {
    AsyncStorage.getItem(RAIL_KEY)
      .then((v) => setRailPref(v === null ? null : v === "1"))
      .catch(() => {});
  }, []);
  const railCollapsed = railPref ?? width < 1280;
  const toggleRail = () => {
    const next = !railCollapsed;
    setRailPref(next);
    AsyncStorage.setItem(RAIL_KEY, next ? "1" : "0").catch(() => {});
  };
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

  // The rail is the single source of truth for labels — this screen only needs
  // them to title the header, and a second list is how "Orders" in the rail
  // ended up sitting above a header that said "Bills".
  const items = SIDEBAR_ITEMS.filter((it) => !it.adminOnly || isAdmin);

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

  if (!authLoaded) {
    return (
      <SafeAreaView style={styles.root} edges={["top", "bottom"]}>
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
          <ActivityIndicator color={C.brand} />
        </View>
      </SafeAreaView>
    );
  }

  const current = items.find((i) => i.key === section);

  return (
    <AppShell
      nav={{
        staff: staff || "Admin",
        role: role || "",
        branchName: activeBranchName || undefined,
        activeKey: section,
        onNavigate: (key) => navigate(key as Section | "shop"),
        onLogout: async () => {
          setSidebarOpen(false);
          await doLogout();
          router.replace("/");
        },
      }}
      drawerOpen={sidebarOpen}
      onDrawerChange={setSidebarOpen}
      railCollapsed={railCollapsed}
      onToggleRail={toggleRail}
      testID="admin-screen"
    >
      {/* One header for every back-office page. Sections supply their own
          subtitle and actions through `sectionHeader`, so the bar reads the
          same on all six rather than each page inventing its own. */}
      <TopBar
        title={current ? tr(current.labelKey) : undefined}
        subtitle={activeBranchName || undefined}
        onMenu={() => (isWide ? toggleRail() : setSidebarOpen(true))}
        menuOpen={isWide ? !railCollapsed : sidebarOpen}
      />

      <View style={styles.content}>
        {section === "reports" && <Reports isWide={isWide} />}
        {section === "transactions" && (
          <Transactions
            isWide={isWide}
            reprint={reprintReceipt}
            staff={staff || "Admin"}
            branchId={activeBranchId}
          />
        )}
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

      {/* Off-screen receipt render target — view-shot captures this. */}
      <PrinterOverlay />
    </AppShell>
  );
}


// =================== REPORTS / DASHBOARD ===================
function Reports({ isWide }: { isWide: boolean }) {
  useT(); // re-render this screen when the language changes
  // A 731px-tall tablet can't show four full stat cards *and* a 210px chart.
  // Scrolling past them is what made this page look empty, so it densifies
  // rather than overflowing.
  const { height: winH } = useWindowDimensions();
  const dense = winH < 820;
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

  const timeline = data?.timeline ?? [];
  const maxBar = Math.max(1, ...timeline.map((t) => t.value));
  const topProdTotal = (data?.top_products || []).reduce((s, p) => s + p.total, 0) || 1;
  const topCatTotal = (data?.top_categories || []).reduce((s, c) => s + c.total, 0) || 1;
  const mixPalette = [C.brand, C.ok, C.accent, C.warn, C.ink2Soft];

  // The chart's own conclusion, in words. The sentence is what changes a
  // staffing decision; the bars are only the evidence for it.
  const peak = useMemo(() => {
    if (timeline.length === 0) return null;
    const total = timeline.reduce((s, t) => s + t.value, 0);
    if (total <= 0) return null;
    let best = 0;
    for (let i = 1; i < timeline.length; i++) {
      if (timeline[i].value > timeline[best].value) best = i;
    }
    // Share carried by the best three consecutive buckets.
    let bestWindow = 0;
    let windowAt = 0;
    for (let i = 0; i + 3 <= timeline.length; i++) {
      const sum = timeline[i].value + timeline[i + 1].value + timeline[i + 2].value;
      if (sum > bestWindow) { bestWindow = sum; windowAt = i; }
    }
    return {
      label: timeline[best].label,
      pct: Math.round((bestWindow / total) * 100),
      from: timeline[windowAt]?.label,
      to: timeline[Math.min(windowAt + 2, timeline.length - 1)]?.label,
    };
  }, [timeline]);

  return (
    <Body style={!isWide && { paddingHorizontal: 14 }}>
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

      {/* Period filter + the two things you do with a report. */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        style={{ flexGrow: 0 }}
        contentContainerStyle={{ gap: 10, alignItems: "center" }}
      >
        {PERIODS.map((p) => (
          <Pill
            key={p.k}
            label={p.k === "custom" && period === "custom" && range.start ? rangeLabel(range) : tr(p.l)}
            active={period === p.k}
            onPress={() => (p.k === "custom" ? setShowRange(true) : setPeriod(p.k))}
            testID={`period-${p.k}`}
          />
        ))}
        <View style={{ width: 6 }} />
        <Btn
          label={tr("admin.channels")}
          icon="pie-chart-outline"
          height={40}
          onPress={() => setShowChannels(true)}
          testID="open-channel-report"
        />
        <Btn
          label={tr("admin.back_office")}
          icon="desktop-outline"
          height={40}
          onPress={() => Linking.openURL(BACKOFFICE_URL)}
          testID="open-backoffice"
        />
      </ScrollView>

      {loading || !data ? (
        <ActivityIndicator color={C.brand} style={{ marginTop: 40 }} />
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={{ gap: 16, paddingBottom: 8 }}
          showsVerticalScrollIndicator={false}
        >
          {/* Only the headline metric gets the blue sparkline — if every card
              is blue the eye has nowhere to land first. */}
          <View style={[styles.statRow, !isWide && { flexWrap: "wrap" }]}>
            <Stat
              icon="bar-chart-outline"
              tint={C.brandTintSoft}
              tintFg={C.brand}
              label={tr("admin.net_sales")}
              value={THB(data.total_sales)}
              delta={`GP ${(data.gp_percent ?? 0).toFixed(1)}%`}
              deltaDir={(data.gp_percent ?? 0) >= 60 ? "up" : "down"}
              spark={timeline.map((t) => t.value)}
              sparkAccent
              dense={dense}
              style={!isWide ? { minWidth: "46%" } : undefined}
            />
            <Stat
              icon="receipt-outline"
              tint={C.okTint}
              tintFg={C.ok}
              label={tr("common.orders")}
              dense={dense}
              value={String(data.tx_count ?? 0)}
              delta={`${THB(data.avg_bill)} average bill`}
              style={!isWide ? { minWidth: "46%" } : undefined}
            />
            <Stat
              icon="trending-up-outline"
              tint={C.accentTint}
              tintFg={C.accentDark}
              label={tr("admin.profit")}
              dense={dense}
              value={THB(data.profit)}
              delta={`after ${THB(data.cost)} cost`}
              style={!isWide ? { minWidth: "46%" } : undefined}
            />
            <Stat
              icon="shield-checkmark-outline"
              tint={C.warnTint}
              tintFg={C.warn}
              label={tr("admin.vat_collected")}
              dense={dense}
              value={THB((data.total_sales * 7) / 107)}
              delta={tr("admin.vat_included_in_prices")}
              style={!isWide ? { minWidth: "46%" } : undefined}
            />
          </View>

          <View style={[styles.reportCols, !isWide && { flexDirection: "column" }]}>
            <Panel style={{ flex: isWide ? 1.55 : undefined }}>
              <PanelHead
                title={tr("admin.sales_trend")}
                right={peak ? <Tag tone="info">{`Peak ${peak.label}`}</Tag> : undefined}
              />
              <View
                style={[styles.bars, dense && { height: 150, paddingTop: 12 }]}
                testID="sales-chart"
              >
                {timeline.length === 0 ? (
                  <Empty icon="bar-chart-outline" title={tr("admin.no_data_for_this_period")} />
                ) : (
                  timeline.map((t, i) => (
                    <View key={i} style={{ flex: 1, justifyContent: "flex-end" }}>
                      <View
                        style={[
                          styles.bar,
                          t.value === maxBar && { backgroundColor: C.brand },
                          { height: Math.max(4, (t.value / maxBar) * (dense ? 118 : 170)) },
                        ]}
                      />
                    </View>
                  ))
                )}
              </View>
              {timeline.length > 0 && (
                <View style={styles.baxis}>
                  {timeline.map((t, i) => (
                    <Money key={i} style={styles.baxisText} numberOfLines={1}>
                      {t.label.slice(5)}
                    </Money>
                  ))}
                </View>
              )}
              {!!peak && peak.pct > 0 && (
                <View style={styles.chartNote}>
                  <View style={styles.chartNoteKey} />
                  <Text style={styles.chartNoteText}>
                    {`${peak.pct}% of the period's takings land between ${peak.from} and ${peak.to}.`}
                  </Text>
                </View>
              )}
            </Panel>

            <View style={{ flex: 1, gap: 16, minWidth: 0 }}>
              <Panel testID="top-categories">
                <PanelHead title={tr("admin.top_categories")} />
                <View style={{ padding: 20 }}>
                  {(data.top_categories ?? []).length === 0 ? (
                    <Empty icon="pie-chart-outline" title={tr("admin.no_sales_yet")} />
                  ) : (
                    (data.top_categories ?? []).map((c, i) => (
                      <MixRow
                        key={c.name}
                        label={c.name}
                        value={`${THB(c.total)} · ${Math.round((c.total / topCatTotal) * 100)}%`}
                        pct={(c.total / topCatTotal) * 100}
                        color={mixPalette[i % mixPalette.length]}
                      />
                    ))
                  )}
                </View>
              </Panel>

              <Panel testID="top-products">
                <PanelHead title={tr("admin.top_products")} />
                {(data.top_products ?? []).length === 0 ? (
                  <Empty icon="cube-outline" title={tr("admin.no_sales_yet")} />
                ) : (
                  <>
                    <THead cols={TOP_PROD_COLS} />
                    {(data.top_products ?? []).map((p, i, arr) => (
                      <TRow key={p.product_id} last={i === arr.length - 1}>
                        <TCell col={TOP_PROD_COLS[0]}>
                          <View style={styles.rankRow}>
                            <Rank n={i + 1} />
                            <TText strong numberOfLines={1}>{p.name}</TText>
                          </View>
                        </TCell>
                        <TCell col={TOP_PROD_COLS[1]}>
                          <TText mono>{p.qty}</TText>
                        </TCell>
                        <TCell col={TOP_PROD_COLS[2]}>
                          <TText mono strong>{THB(p.total)}</TText>
                        </TCell>
                      </TRow>
                    ))}
                  </>
                )}
              </Panel>
            </View>
          </View>
        </ScrollView>
      )}
    </Body>
  );
}

const TOP_PROD_COLS: Col[] = [
  { key: "name", title: "admin.product_2", flex: 2 },
  { key: "qty", title: "admin.sold", flex: 1, right: true },
  { key: "total", title: "admin.revenue", flex: 1.2, right: true },
];


// ── Shared period filter helpers (dashboard + channel report) ──
type DateRange = { start: string; end: string };
const PERIODS = [
  { k: "today", l: "admin.today" },
  { k: "week", l: "admin.this_week" },
  { k: "month", l: "admin.this_month" },
  { k: "year", l: "admin.this_year" },
  { k: "custom", l: "admin.custom" },
] as const;
const PERIOD_LABELS: Record<string, string> = {
  today: "admin.today", week: "admin.this_week", month: "admin.this_month", year: "admin.this_year", custom: "admin.custom",
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
          <Ionicons name="chevron-back" size={20} color={C.ink} />
        </TouchableOpacity>
        <Text style={styles.calMonth}>{monthLabel}</Text>
        <TouchableOpacity onPress={() => shift(1)} style={styles.calNavBtn} testID="cal-next">
          <Ionicons name="chevron-forward" size={20} color={C.ink} />
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
  useT(); // re-render this screen when the language changes
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
            <TouchableOpacity onPress={onClose}><Ionicons name="close" size={22} color={C.ink2} /></TouchableOpacity>
            <Text style={styles.modalTitle}>{tr("admin.custom_range")}</Text>
            <View style={{ width: 22 }} />
          </View>
          <View style={{ padding: 16, gap: 12 }}>
            <View style={styles.rangeSummary}>
              <View style={styles.rangeSummaryCol}>
                <Text style={styles.docFieldLabel}>{tr("admin.start")}</Text>
                <Text style={styles.rangeSummaryVal}>{start || "—"}</Text>
              </View>
              <Ionicons name="arrow-forward" size={16} color={C.ink3} />
              <View style={styles.rangeSummaryCol}>
                <Text style={styles.docFieldLabel}>{tr("admin.end")}</Text>
                <Text style={styles.rangeSummaryVal}>{end || (start ? tr("admin.same_day") : "—")}</Text>
              </View>
            </View>
            <Calendar start={start} end={end} onPick={pick} />
            <TouchableOpacity
              style={[styles.primaryBtn, !start && { opacity: 0.5 }]}
              disabled={!start}
              onPress={() => onApply({ start, end })}
              testID="range-apply"
            >
              <Text style={styles.primaryBtnText}>{tr("admin.apply")}</Text>
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
  useT(); // re-render this screen when the language changes
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
            <Ionicons name="chevron-back" size={22} color={C.ink} />
            <Text style={styles.docBackText}>{tr("common.back")}</Text>
          </TouchableOpacity>
          <Text style={styles.docTopTitle}>{tr("admin.sales_channel_report")}</Text>
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
                {p.k === "custom" && curPeriod === "custom" && curRange.start ? rangeLabel(curRange) : tr(p.l)}
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
          <Text style={[styles.chHeadCell, { flex: 1, textAlign: "left" }]}>{tr("admin.channel_name")}</Text>
          <Text style={[styles.chHeadCell, { width: 70 }]}>{tr("admin.count")}</Text>
          <Text style={[styles.chHeadCell, { width: 110 }]}>{tr("admin.before_gp")}</Text>
          <Text style={[styles.chHeadCell, { width: 90 }]}>{tr("admin.gp")}</Text>
          <Text style={[styles.chHeadCell, { width: 110 }]}>{tr("admin.after_gp")}</Text>
        </View>

        {rows === null ? (
          <ActivityIndicator color={C.brand} style={{ marginTop: 40 }} />
        ) : rows.length === 0 ? (
          <View style={styles.emptyBox}><Text style={styles.emptyText}>{tr("admin.no_sales")}</Text></View>
        ) : (
          <ScrollView>
            {rows.map((r, i) => (
              <View key={r.source + i} style={styles.chRow}>
                <Text style={[styles.chCell, { width: 30 }]}>{i + 1}</Text>
                <View style={{ flex: 1, flexDirection: "row", alignItems: "center", gap: 8 }}>
                  <View style={styles.chIcon}>
                    <Ionicons name="storefront" size={14} color={C.brand} />
                  </View>
                  <Text style={styles.chName} numberOfLines={1}>{r.channel}</Text>
                  {!r.has_gp && (
                    <View style={styles.noGpBadge}><Text style={styles.noGpText}>{tr("admin.no_gp")}</Text></View>
                  )}
                </View>
                <Text style={[styles.chCell, { width: 70 }]}>{r.count}</Text>
                <Text style={[styles.chCell, { width: 110 }]}>{r.before_gp.toFixed(2)}</Text>
                <Text style={[styles.chCell, { width: 90 }]}>{r.gp.toFixed(2)}</Text>
                <Text style={[styles.chCell, { width: 110 }]}>{r.after_gp.toFixed(2)}</Text>
              </View>
            ))}
            <View style={[styles.chRow, { backgroundColor: C.bgSoft }]}>
              <Text style={[styles.chCell, { width: 30 }]} />
              <Text style={[styles.chCell, { flex: 1, textAlign: "left", fontWeight: "700" }]}>{tr("common.total")}</Text>
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

// Translate a date chip into the ``from``/``to`` window the server filters on.
// Boundaries are the *device's* local day so "Today" matches the wall clock the
// cashier reads; they go over the wire as absolute UTC instants, so the server
// never has to guess the till's timezone.  ``to`` is exclusive.
function dateFilterRange(filter: DateFilter): { from?: string; to?: string } {
  if (filter === "all") return {};
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dayMs = 24 * 60 * 60 * 1000;
  if (filter === "today") return { from: startOfToday.toISOString() };
  if (filter === "yesterday") {
    return {
      from: new Date(startOfToday.getTime() - dayMs).toISOString(),
      to: startOfToday.toISOString(),
    };
  }
  return { from: new Date(startOfToday.getTime() - 6 * dayMs).toISOString() };
}

// One screenful plus a little, so the pager is reachable without a long scroll
// but a busy day is still a handful of pages rather than dozens.
const PAGE_SIZE = 50;

// TEMPORARY OVERRIDE (2026-08-21, tech@therollingpinn.com): every branch may
// issue a full tax invoice, POS ID or not, until the Revenue Department has
// issued a machine number for each till.  Set this back to `false` to restore
// the per-branch gate below — that is the whole revert, nothing else changed.
// While it is `true` a branch with a blank POS ID prints a full tax invoice
// with no "POS ID:" line on it (ReceiptImage only renders that line when the
// value is set), so the document goes out without a machine number.
const FULL_TAX_INVOICE_EVERYWHERE: boolean = true;

function Transactions({ isWide, reprint, staff, branchId }: {
  isWide: boolean; reprint: ReprintFn; staff: string; branchId: string;
}) {
  useT(); // re-render this screen when the language changes
  const [orders, setOrders] = useState<Order[]>([]);
  const [selected, setSelected] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [showDetail, setShowDetail] = useState(false);
  const [query, setQuery] = useState("");
  // Defaults to Today: "All" pulled every bill the branch had ever rung up on
  // every visit, which left the screen on a spinner for seconds.  The cashier
  // almost always wants the current day; the other buckets refetch on tap.
  const [dateFilter, setDateFilter] = useState<DateFilter>("today");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  // Debounced copy of `query` — the search now goes to the server, so firing
  // on every keystroke would be a request per character.
  const [debouncedQuery, setDebouncedQuery] = useState("");
  // Does the server understand offset/q/X-Total-Count? null until the first
  // response answers it. The deployed API predates them, and an APK built
  // against this code will meet that older server — without this the app
  // would send `q`, be ignored, and show unfiltered rows as if they matched.
  const [serverPaged, setServerPaged] = useState<boolean | null>(null);
  // Order items only snapshot name/price/qty, so we join to the live
  // product catalogue (by product_id) to show the thumbnail + barcode on
  // each receipt line — matching the reference Sale Transactions screen.
  const [productMap, setProductMap] = useState<Record<string, ProductRef>>({});
  const [taxPercent, setTaxPercent] = useState(7);
  // A full tax invoice must carry the Revenue Department's approved machine
  // number for the till that issues it, and that number is per branch.  A
  // branch with no POS ID cannot issue one at all, so the document is left off
  // the Reprint menu there rather than printing an invalid invoice.
  const [branchPosId, setBranchPosId] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (!branchId) { setBranchPosId(null); return; }
    apiFetch(`${API}/branches`)
      .then((r) => (r.ok ? r.json() : []))
      .then((list) => {
        if (cancelled) return;
        const b = (Array.isArray(list) ? list : []).find(
          (x: any) => String(x?.id) === String(branchId),
        );
        setBranchPosId(((b?.pos_id ?? "") as string).trim());
      })
      // Unreachable server: leave it null so the menu keeps the option rather
      // than hiding a document the branch may well be entitled to print.
      .catch(() => { if (!cancelled) setBranchPosId(null); });
    return () => { cancelled = true; };
  }, [branchId]);
  const canIssueTaxInvoice =
    FULL_TAX_INVOICE_EVERYWHERE || branchPosId === null || branchPosId.length > 0;

  // Merge a server-updated order (after a void, or after a full tax invoice is
  // issued) back into the list and the open detail pane so the new state shows
  // without a reload.
  const handleOrderUpdated = useCallback((updated: Order) => {
    setOrders((prev) => prev.map((o) => (o.id === updated.id ? { ...o, ...updated } : o)));
    setSelected((cur) => (cur && cur.id === updated.id ? { ...cur, ...updated } : cur));
  }, []);

  // Catalogue + settings are filter-independent, so they load once and are not
  // refetched when the cashier switches date bucket.
  useEffect(() => {
    (async () => {
      const [prodRes, setRes] = await Promise.all([
        apiFetch(`${API}/products`).catch(() => null),
        apiFetch(`${API}/settings`).catch(() => null),
      ]);
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
    })();
  }, []);

  // Orders are fetched per date bucket so the wire only ever carries the rows
  // the chip actually shows.  A stale flag drops the response of a superseded
  // request — tapping through the chips quickly used to let a slow "All" land
  // after a fast "Today" and repopulate the list.
  useEffect(() => {
    let stale = false;
    setLoading(true);
    (async () => {
      const { from, to } = dateFilterRange(dateFilter);
      const params = new URLSearchParams();
      if (from) params.set("from", from);
      if (to) params.set("to", to);
      if (debouncedQuery) params.set("q", debouncedQuery);
      // Only page against a server that can. An older one ignores `offset`
      // and would hand back page 1 forever while the pager claimed otherwise,
      // so there we fall back to the previous behaviour: one large fetch.
      if (serverPaged !== false) {
        params.set("limit", String(PAGE_SIZE));
        params.set("offset", String(page * PAGE_SIZE));
      }
      try {
        const res = await apiFetch(`${API}/orders?${params.toString()}`);
        const o: Order[] = await res.json();
        if (stale) return;
        // The server sends the unpaged total in a header so the body stays a
        // bare list. An older server won't send it — fall back to the page
        // length so the pager degrades to "one page" instead of breaking.
        const totalHeader = res.headers?.get?.("X-Total-Count");
        const parsed = totalHeader ? parseInt(totalHeader, 10) : NaN;
        const supported = Number.isFinite(parsed);
        setServerPaged(supported);
        // Legacy: everything we have is all there is, so one page.
        setTotal(supported ? parsed : o.length);
        setOrders(o);
        setSelected((cur) => (cur ? cur : o[0] && isWide ? o[0] : null));
      } catch {
        if (!stale) { setOrders([]); setTotal(0); }
      } finally {
        if (!stale) setLoading(false);
      }
    })();
    return () => { stale = true; };
  }, [dateFilter, isWide, page, debouncedQuery, serverPaged]);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  // Changing the date bucket or the search restarts at page 1 — staying on
  // page 4 of a filter that now has one page shows an empty list.
  useEffect(() => { setPage(0); }, [dateFilter, debouncedQuery]);

  // Filter by order number (case-insensitive substring) and the chosen
  // date bucket.  Buckets are computed from the local-day boundary of the
  // *current* device so "Today" matches what the cashier sees on the wall
  // clock, not UTC.
  // A new server has already applied the search, so this is a no-op there.
  // Against the old one it is the only thing making search work at all.
  const filteredOrders = useMemo(() => {
    if (serverPaged !== false) return orders;
    const q = debouncedQuery.toLowerCase();
    return q
      ? orders.filter((o) => o.order_number.toLowerCase().includes(q))
      : orders;
  }, [orders, debouncedQuery, serverPaged]);

  // If the currently-selected order falls out of the filter, drop the
  // selection so the right-hand detail pane doesn't show a row the user
  // can no longer see in the list.
  useEffect(() => {
    if (selected && !filteredOrders.some((o) => o.id === selected.id)) {
      setSelected(isWide ? (filteredOrders[0] ?? null) : null);
    }
  }, [filteredOrders, selected, isWide]);

  // Bills hang off a day timeline rather than sitting in a flat list, so the
  // rail needs day headers interleaved with the rows — and each day's takings
  // must be known before its header renders.
  const timeline = useMemo(() => {
    const dayTotal = new Map<string, number>();
    for (const o of filteredOrders) {
      const k = new Date(o.created_at).toDateString();
      if (o.status !== "cancel") dayTotal.set(k, (dayTotal.get(k) ?? 0) + o.total);
    }
    const today = new Date().toDateString();
    const yesterday = new Date(Date.now() - 864e5).toDateString();
    const rows: ({ kind: "day"; key: string; label: string; total: number }
               | { kind: "bill"; key: string; o: Order })[] = [];
    let seen = "";
    for (const o of filteredOrders) {
      const k = new Date(o.created_at).toDateString();
      if (k !== seen) {
        seen = k;
        rows.push({
          kind: "day",
          key: `day-${k}`,
          label: k === today ? tr("admin.today") : k === yesterday ? tr("admin.yesterday")
            : new Date(o.created_at).toLocaleDateString("en-GB", {
                weekday: "short", day: "2-digit", month: "short",
              }),
          total: dayTotal.get(k) ?? 0,
        });
      }
      rows.push({ kind: "bill", key: o.id, o });
    }
    return rows;
  }, [filteredOrders]);

  const { width: winW } = useWindowDimensions();
  const ORDER_COLS = winW >= 1440 ? ORDER_COLS_FULL : ORDER_COLS_COMPACT;
  const ocol = (k: string) => ORDER_COLS.find((c) => c.key === k)!;

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const takings = useMemo(
    () => filteredOrders.reduce((n, o) => (o.status === "cancel" ? n : n + o.total), 0),
    [filteredOrders],
  );

  // Mobile drill-down: show list OR detail
  if (!isWide && showDetail && selected) {
    return (
      <View style={{ flex: 1 }}>
        <TouchableOpacity style={styles.backRow} onPress={() => setShowDetail(false)}>
          <Ionicons name="chevron-back" size={22} color={C.brand} />
          <Text style={styles.backText}>{tr("admin.back_to_orders")}</Text>
        </TouchableOpacity>
        <TransactionDetail
          order={selected}
          reprint={reprint}
          staff={staff}
          onOrderUpdated={handleOrderUpdated}
          productMap={productMap}
          taxPercent={taxPercent}
          canIssueTaxInvoice={canIssueTaxInvoice}
        />
      </View>
    );
  }

  return (
    <Body
      style={!isWide && { paddingHorizontal: 14 }}
      testID="transactions-section"
    >
      {/* Filters, then a search that narrows what the filter returned. */}
      <View style={[styles.filterRow, !isWide && { flexWrap: "wrap" }]}>
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          style={{ flexGrow: 0, flexShrink: 1 }}
          contentContainerStyle={{ gap: 10 }}
        >
          {([
            { key: "today", label: tr("admin.today") },
            { key: "yesterday", label: tr("admin.yesterday") },
            { key: "week", label: tr("admin.last_7_days") },
            { key: "all", label: tr("admin.all") },
          ] as { key: DateFilter; label: string }[]).map((opt) => (
            <Pill
              key={opt.key}
              label={opt.label}
              active={dateFilter === opt.key}
              onPress={() => setDateFilter(opt.key)}
              testID={`tx-date-${opt.key}`}
            />
          ))}
        </ScrollView>
        <Spacer />
        <SearchField
          value={query}
          onChangeText={setQuery}
          placeholder={tr("admin.order_no")}
          height={40}
          style={{ width: isWide ? 230 : "100%" }}
          testID="tx-search"
        />
      </View>

      <View style={[styles.twoCol, !isWide && styles.stackedCol]}>
        <Panel style={{ flex: 1, minWidth: 0 }}>
          {/* Takings sit in the panel header, so the number that matters is
              beside the rows it was computed from. */}
          <PanelHead
            title={
              total > filteredOrders.length
                ? `${page * PAGE_SIZE + 1}\u2013${page * PAGE_SIZE + filteredOrders.length} of ${total}`
                : `${filteredOrders.length} ${filteredOrders.length === 1 ? tr("admin.order") : tr("admin.orders")}`
            }
            right={
              // Only this page's rows are in hand, so don't let the figure
              // sitting beside "of 78" read as the total for all 78.
              <View style={{ flexDirection: "row", alignItems: "baseline", gap: 6 }}>
                <Money style={styles.takings}>{THB(takings)}</Money>
                {total > filteredOrders.length && (
                  <Text style={styles.takingsNote}>{tr("admin.this_page")}</Text>
                )}
              </View>
            }
          />
          {loading ? (
            <ActivityIndicator color={C.brand} style={{ marginTop: 40 }} testID="tx-loading" />
          ) : filteredOrders.length === 0 ? (
            <Empty
              icon="receipt-outline"
              title={tr("admin.no_matching_orders")}
              note={tr("admin.try_a_wider_date_range_or")}
            />
          ) : (
            <>
              {isWide && <THead cols={ORDER_COLS} />}
              <FlatList
                data={timeline}
                keyExtractor={(r) => r.key}
                showsVerticalScrollIndicator={false}
                renderItem={({ item: row }) => {
                  // Day headers stay: they carry each day's takings, which a
                  // flat table would otherwise make you total by hand.
                  if (row.kind === "day") {
                    return (
                      <View style={styles.billsDay}>
                        <Text style={styles.billsDayLabel}>{row.label}</Text>
                        <View style={styles.billsDayRule} />
                        <Money style={styles.billsDayTotal}>{THB(row.total)}</Money>
                      </View>
                    );
                  }
                  const o = row.o;
                  // Voided orders stay in the list, struck through. A missing
                  // order number is what makes staff distrust a till.
                  const voided = o.status === "cancel";
                  const active = selected?.id === o.id && isWide;
                  const qty = (o.items || []).reduce(
                    (n: number, it: any) => n + (Number(it.qty) || 0), 0,
                  );

                  if (!isWide) {
                    return (
                      <TouchableOpacity
                        style={[styles.billRow, active && styles.billRowActive]}
                        onPress={() => { setSelected(o); setShowDetail(true); }}
                        testID={`tx-${o.order_number}`}
                      >
                        <View style={{ flex: 1, minWidth: 0 }}>
                          <Money style={[styles.billNum, voided && styles.txVoided]}>
                            {o.order_number}
                          </Money>
                          <Text style={styles.billMeta} numberOfLines={1}>
                            {`${o.created_time} · ${voided ? tr("admin.voided") : (o.payment_method ? methodLabel(o.payment_method) : o.source || "—")}`}
                          </Text>
                        </View>
                        <Money style={[styles.billAmount, voided && styles.txVoided]}>
                          {THB(o.total)}
                        </Money>
                      </TouchableOpacity>
                    );
                  }

                  return (
                    <TRow
                      selected={active}
                      onPress={() => setSelected(o)}
                      testID={`tx-${o.order_number}`}
                    >
                      <TCell col={ocol("num")}>
                        <TText
                          mono
                          strong
                          muted={voided}
                          color={active ? C.brand : undefined}
                        >
                          {o.order_number}
                        </TText>
                      </TCell>
                      <TCell col={ocol("time")}>
                        <TText mono muted={voided}>{o.created_time}</TText>
                      </TCell>
                      {ORDER_COLS.length === ORDER_COLS_FULL.length && (
                        <>
                          <TCell col={ocol("customer")}>
                            <TText muted={!o.customer_name || voided}>
                              {o.customer_name || tr("admin.walk_in")}
                            </TText>
                          </TCell>
                          <TCell col={ocol("staff")}>
                            <TText muted={voided}>{o.staff || "—"}</TText>
                          </TCell>
                        </>
                      )}
                      <TCell col={ocol("pay")}>
                        <TText muted={voided}>
                          {o.payment_method ? methodLabel(o.payment_method) : (o.source || "—")}
                        </TText>
                      </TCell>
                      <TCell col={ocol("items")}>
                        <TText mono muted={voided}>{qty || "—"}</TText>
                      </TCell>
                      <TCell col={ocol("total")}>
                        <TText mono strong muted={voided} strike={voided}>
                          {THB(o.total)}
                        </TText>
                      </TCell>
                      <TCell col={ocol("status")}>
                        {voided ? (
                          <Tag tone="red">{tr("admin.voided")}</Tag>
                        ) : o.pos_tax_invoice ? (
                          <Tag tone="purple">{tr("admin.tax_inv")}</Tag>
                        ) : (
                          <Tag tone="ok">{tr("admin.paid")}</Tag>
                        )}
                      </TCell>
                    </TRow>
                  );
                }}
              />
              {pageCount > 1 && (
                <View style={styles.pager}>
                  <Btn
                    label={tr("admin.previous")}
                    icon="chevron-back"
                    height={40}
                    disabled={page === 0 || loading}
                    onPress={() => setPage((p) => Math.max(0, p - 1))}
                    testID="orders-prev"
                  />
                  <Spacer />
                  <Money style={styles.pagerText}>
                    {`Page ${page + 1} of ${pageCount}`}
                  </Money>
                  <Spacer />
                  <Btn
                    label={tr("admin.next")}
                    iconRight="chevron-forward"
                    height={40}
                    disabled={page >= pageCount - 1 || loading}
                    onPress={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                    testID="orders-next"
                  />
                </View>
              )}
            </>
          )}
        </Panel>

        {isWide && (
          <View style={styles.detailCol}>
            {!selected ? (
              <Panel style={{ flex: 1 }}>
                <Empty
                  icon="document-text-outline"
                  title={tr("admin.select_an_order")}
                  note={tr("admin.its_lines_totals_and_reprint_options")}
                />
              </Panel>
            ) : (
              <TransactionDetail
                order={selected}
                reprint={reprint}
                staff={staff}
                onOrderUpdated={handleOrderUpdated}
                productMap={productMap}
                taxPercent={taxPercent}
                canIssueTaxInvoice={canIssueTaxInvoice}
              />
            )}
          </View>
        )}
      </View>
    </Body>
  );
}

const ORDER_COLS_FULL: Col[] = [
  { key: "num", title: "admin.order_2", flex: 1.5 },
  { key: "time", title: "admin.time", flex: 0.9 },
  { key: "customer", title: "common.customer", flex: 1.6 },
  { key: "staff", title: "common.cashier", flex: 1.1 },
  { key: "pay", title: "common.payment", flex: 1.2 },
  { key: "items", title: "admin.items", flex: 0.7, right: true },
  { key: "total", title: "common.total", flex: 1.3, right: true },
  { key: "status", title: "admin.status", flex: 1.1, right: true },
];
// Customer and cashier are the first to go — the order number, total and
// status are what the row is actually scanned for.
const ORDER_COLS_COMPACT: Col[] = ORDER_COLS_FULL.filter(
  (c) => !["customer", "staff"].includes(c.key),
);


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
  onOrderUpdated,
  productMap,
  taxPercent,
  canIssueTaxInvoice,
}: {
  order: Order;
  reprint: ReprintFn;
  staff: string;
  onOrderUpdated: (updated: Order) => void;
  productMap: Record<string, ProductRef>;
  taxPercent: number;
  // False when this branch has no POS ID — see Transactions.
  canIssueTaxInvoice: boolean;
}) {
  useT(); // re-render this screen when the language changes
  const [reprintBusy, setReprintBusy] = useState(false);
  const [voidBusy, setVoidBusy] = useState(false);
  // Reprint is a menu of documents (abbreviated slip, full tax invoice, …),
  // not a single action — see the reference POS's พิมพ์ซ้ำ popup.
  const [menuOpen, setMenuOpen] = useState(false);
  const [taxFlowOpen, setTaxFlowOpen] = useState(false);

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
  const toReceiptOrder = (
    voided: boolean,
    voidedBy?: string,
    taxInvoice?: TaxInvoiceData,
  ): ReceiptOrder => ({
    order_number: order.order_number,
    // The RD machine number of the branch this bill was rung up at — not the
    // shop-wide Settings value, which is one number for every branch.
    branch_pos_id: order.branch_pos_id || "",
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
    doc_type: taxInvoice ? "full" : "abbreviated",
    tax_invoice: taxInvoice && {
      name: taxInvoice.name,
      tax_id: taxInvoice.tax_id,
      tax_branch: taxInvoice.tax_branch,
      address: taxInvoice.address,
      phone: taxInvoice.phone,
      email: taxInvoice.email,
    },
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
      showAlert(
        tr("admin.print_failed"),
        `${r.error}\n\nCheck the printer under Settings → Local Printer (it must be enabled and connected on this device).`,
      );
    }
    return r.ok;
  };

  const printAbbreviated = async () => {
    setReprintBusy(true);
    try {
      await sendPrint(toReceiptOrder(isVoided));
    } catch (e: any) {
      showAlert(tr("admin.print_failed"), e?.message || String(e));
    } finally {
      setReprintBusy(false);
    }
  };

  // Called by TaxInvoiceFlow once the buyer details are saved on the server.
  // Printing is separate from saving on purpose: a print failure (paper out,
  // printer asleep) must not lose the details the cashier just typed — they
  // are already persisted, so Reprint replays them without retyping.
  const printFullTaxInvoice = async (data: TaxInvoiceData) => {
    setTaxFlowOpen(false);
    setReprintBusy(true);
    try {
      await sendPrint(toReceiptOrder(isVoided, undefined, data));
    } catch (e: any) {
      showAlert(tr("admin.print_failed"), e?.message || String(e));
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
      onOrderUpdated(updated);
      // Auto-print the void receipt (fires the "Printing…" overlay).
      await sendPrint(toReceiptOrder(true, updated.voided_by));
    } catch (e: any) {
      showAlert(tr("admin.void_failed"), e?.message || String(e));
    } finally {
      setVoidBusy(false);
    }
  };

  const onVoid = () => {
    showAlert(
      tr("admin.void_bill"),
      tr("admin.are_you_sure_you_want_to_2"),
      [
        { text: "Close", style: "cancel" },
        { text: "Confirm", style: "destructive", onPress: doVoid },
      ],
    );
  };

  return (
    <View style={styles.txDetailWrap}>
      <ScrollView style={{ flex: 1 }} contentContainerStyle={styles.txDetailScroll}>
        {/* ── Header: what happened, for how much, and what you can do ── */}
        <View style={styles.tdHeadRow}>
          <View style={{ flex: 1, minWidth: 0 }}>
            <View style={styles.tdEyebrowRow}>
              <Text style={styles.tdOrderNo} numberOfLines={1}>
                {order.order_number}
              </Text>
              <View style={[styles.tdStatus, isVoided && styles.tdStatusVoid]}>
                <Text style={[styles.tdStatusText, isVoided && styles.tdStatusTextVoid]}>
                  {isVoided ? tr("admin.voided") : tr("admin.paid")}
                </Text>
              </View>
            </View>
            <Text style={[styles.tdGrand, isVoided && styles.txVoided]}>{THB(order.total)}</Text>
          </View>

          {/* Actions live with the bill, not as two coloured slabs pinned to
              the bottom of the pane. */}
          <View style={styles.tdActions}>
            <TouchableOpacity
              style={[styles.tdReprintBtn, reprintBusy && { opacity: 0.6 }]}
              onPress={() => setMenuOpen(true)}
              disabled={reprintBusy}
              testID={`reprint-${order.order_number}`}
            >
              <Ionicons name="print-outline" size={15} color={C.surface} />
              <Text style={styles.tdReprintText}>{reprintBusy ? tr("admin.printing") : tr("admin.re_print")}</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.tdCancelBtn, (isVoided || voidBusy) && styles.tdCancelBtnDisabled]}
              onPress={onVoid}
              disabled={isVoided || voidBusy}
              testID={`void-${order.order_number}`}
            >
              <Text style={styles.tdCancelText}>{voidBusy ? tr("admin.voiding") : tr("admin.cancel_bill")}</Text>
            </TouchableOpacity>
          </View>
        </View>

        {/* Meta reads as labelled facts, not a stack of grey sentences. */}
        <View style={styles.tdMetaGrid}>
          <TdFact label={tr("admin.when")} value={formatThaiDateTime(order.created_at)} />
          <TdFact label={tr("common.cashier")} value={order.staff || "—"} />
          <TdFact label={tr("admin.channel")} value={channelLabel(order.source)} />
          {isVoided && <TdFact label={tr("admin.voided_by")} value={order.voided_by || "—"} danger />}
        </View>
        {!!order.pos_tax_invoice && (
          <Text style={styles.tdTaxIssued}>
            {tr("admin.tax_invoice_issued_to")} {order.pos_tax_invoice.name}
            {order.pos_tax_invoice.issued_by ? ` by ${order.pos_tax_invoice.issued_by}` : ""}
          </Text>
        )}

        <Text style={styles.tdHeading}>{order.items.length} {order.items.length === 1 ? tr("common.item") : tr("common.items")}</Text>
        {order.items.map((it: any, i: number) => {
          const ref = it.product_id ? productMap[it.product_id] : undefined;
          const img = ref?.image;
          return (
            <View key={i} style={styles.tdItemRow}>
              {img ? (
                <Image source={{ uri: img }} style={styles.tdItemImg} />
              ) : (
                <View style={[styles.tdItemImg, styles.tdItemImgEmpty]}>
                  <Ionicons name="image-outline" size={18} color={C.lineStrong} />
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
          <TdLine label={tr("common.subtotal")} value={THB(subtotal)} />
          <TdLine label={tr("admin.subtotal_ex_tax")} value={THB(exTax)} />
          <TdLine label={`Tax ${taxPercent} %`} value={THB(tax)} />
        </View>

        <Text style={styles.tdHeading}>{tr("common.payment")}</Text>
        <TdLine label={order.payment_method || tr("common.cash")} value={THB(paid)} />
        <TdLine label={tr("common.change")} value={THB(change)} bold />
      </ScrollView>

      <ReprintMenu
        visible={menuOpen}
        onClose={() => setMenuOpen(false)}
        onAbbreviated={() => { setMenuOpen(false); printAbbreviated(); }}
        onFullTaxInvoice={() => { setMenuOpen(false); setTaxFlowOpen(true); }}
        showFullTaxInvoice={canIssueTaxInvoice}
      />

      {taxFlowOpen && (
        <TaxInvoiceFlow
          order={order}
          onClose={() => setTaxFlowOpen(false)}
          onOrderUpdated={onOrderUpdated}
          onPrint={printFullTaxInvoice}
        />
      )}
    </View>
  );
}

// ─── Reprint document picker ────────────────────────────────────────────────
// Mirrors the reference POS's พิมพ์ซ้ำ popup.  All five documents are listed so
// the menu matches what cashiers are trained on, but only the two thermal
// documents are wired up; the rest say so rather than failing silently.
const REPRINT_UNBUILT = [
  { key: "a4", labelKey: "admin.receipt_tax_invoice_a4" },
  { key: "image", labelKey: "admin.save_as_image" },
  { key: "email", labelKey: "admin.email" },
];

function ReprintMenu({
  visible,
  onClose,
  onAbbreviated,
  onFullTaxInvoice,
  showFullTaxInvoice,
}: {
  visible: boolean;
  onClose: () => void;
  onAbbreviated: () => void;
  onFullTaxInvoice: () => void;
  // A branch with no POS ID has no approved machine number to put on a full
  // tax invoice, so the row is left out entirely there — offering a document
  // the branch may not legally issue is worse than a shorter menu.
  showFullTaxInvoice: boolean;
}) {
  useT(); // re-render this screen when the language changes
  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <TouchableOpacity style={styles.tiBackdrop} activeOpacity={1} onPress={onClose}>
        <TouchableOpacity style={styles.rpMenu} activeOpacity={1} testID="reprint-menu">
          <Text style={styles.rpTitle}>{tr("admin.reprint")}</Text>
          <TouchableOpacity
            style={styles.rpItem}
            onPress={onAbbreviated}
            testID="reprint-abbreviated"
          >
            <Text style={styles.rpItemText}>{tr("admin.abbreviated_tax_invoice")}</Text>
          </TouchableOpacity>
          {showFullTaxInvoice && (
            <TouchableOpacity
              style={styles.rpItem}
              onPress={onFullTaxInvoice}
              testID="reprint-full-tax-invoice"
            >
              <Text style={styles.rpItemText}>{tr("admin.receipt_tax_invoice")}</Text>
            </TouchableOpacity>
          )}
          {REPRINT_UNBUILT.map((opt) => (
            <View key={opt.key} style={[styles.rpItem, styles.rpItemDisabled]}>
              <Text style={[styles.rpItemText, styles.rpItemTextDisabled]}>{tr(opt.labelKey)}</Text>
              <Text style={styles.rpSoon}>{tr("admin.not_available_yet")}</Text>
            </View>
          ))}
        </TouchableOpacity>
      </TouchableOpacity>
    </Modal>
  );
}

// ─── Full tax invoice (ใบกำกับภาษีเต็มรูป) ──────────────────────────────────
// Three steps, matching the reference POS: find or create the buyer, fill in
// their Revenue Department particulars, then print.  The particulars are saved
// twice on purpose — onto the Customer, so the next invoice for the same
// company prefills, and onto the Order, so this bill keeps a permanent buyer of
// record that a reprint can replay.
type TaxStep = "search" | "add" | "form";

const GENDERS: { key: NonNullable<Customer["gender"]>; labelKey: string }[] = [
  { key: "male", labelKey: "admin.male" },
  { key: "female", labelKey: "admin.female" },
  { key: "unspecified", labelKey: "admin.unspecified" },
];

// Display name for a picker row: companies use `name` alone, people have both.
function customerFullName(c: Customer): string {
  return [c.name, c.last_name].filter(Boolean).join(" ").trim();
}

function TaxInvoiceFlow({
  order,
  onClose,
  onOrderUpdated,
  onPrint,
}: {
  order: Order;
  onClose: () => void;
  onOrderUpdated: (updated: Order) => void;
  onPrint: (data: TaxInvoiceData) => void;
}) {
  useT(); // re-render this screen when the language changes
  const existing = order.pos_tax_invoice;

  // Re-issuing an invoice that already names a buyer opens straight on the
  // form: the buyer is settled, and sending the cashier back through the picker
  // invites them to silently change who the bill was invoiced to.
  const [step, setStep] = useState<TaxStep>(existing ? "form" : "search");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Customer | null>(null);
  // Non-null while a request is in flight; the string is the overlay caption.
  const [busy, setBusy] = useState<string | null>(null);
  // Set once Print has been tapped, so tr("admin.required") messages appear on the fields
  // the cashier skipped instead of greeting them on a blank form.
  const [attempted, setAttempted] = useState(false);

  const [form, setForm] = useState({
    name: existing?.name || order.customer_name || "",
    tax_id: existing?.tax_id || "",
    tax_branch: existing?.tax_branch || "",
    address: existing?.address || "",
    phone: existing?.phone || "",
    email: existing?.email || "",
  });
  const setField = (k: keyof typeof form) => (v: string) =>
    setForm((f) => ({ ...f, [k]: v }));

  const [add, setAdd] = useState({
    name: "",
    last_name: "",
    gender: "unspecified" as NonNullable<Customer["gender"]>,
    phone: "",
    birth_date: "",
    group: "",
  });
  const setAddField = (k: keyof typeof add) => (v: string) =>
    setAdd((a) => ({ ...a, [k]: v }));

  useEffect(() => {
    (async () => {
      try {
        const res = await apiFetch(`${API}/customers`);
        setCustomers(res.ok ? await res.json() : []);
      } catch {
        setCustomers([]);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return customers;
    return customers.filter((c) =>
      customerFullName(c).toLowerCase().includes(q) ||
      (c.phone || "").includes(q) ||
      (c.tax_id || "").includes(q),
    );
  }, [customers, query]);

  // Picking a buyer carries over whatever tax details they already have, so a
  // repeat company is one tap and a Print away.
  const pick = (c: Customer) => {
    setSelected(c);
    setForm({
      name: customerFullName(c),
      tax_id: c.tax_id || "",
      tax_branch: c.tax_branch || "",
      address: c.address || "",
      phone: c.phone || "",
      email: c.email || "",
    });
    setStep("form");
  };

  const createCustomer = async () => {
    if (!add.name.trim() || !add.last_name.trim()) {
      showAlert(tr("admin.missing_details"), tr("admin.first_name_and_last_name_are"));
      return;
    }
    setBusy(tr("admin.saving_customer"));
    try {
      const res = await apiFetch(`${API}/customers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: add.name.trim(),
          last_name: add.last_name.trim(),
          gender: add.gender,
          phone: add.phone.trim(),
          // Sent as "" when untouched; the serializer maps that to NULL rather
          // than rejecting the whole save over an optional field.
          birth_date: add.birth_date.trim(),
          group: add.group.trim(),
        }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        throw new Error(detail || `Server error (${res.status})`);
      }
      const created: Customer = await res.json();
      setCustomers((list) => [created, ...list]);
      pick(created);
    } catch (e: any) {
      showAlert(tr("common.couldnt_save_customer"), e?.message || "Please try again.");
    } finally {
      setBusy(null);
    }
  };

  const taxDigits = form.tax_id.replace(/\D/g, "");

  // Why Print can't proceed, per field.  These are shown inline rather than
  // only greying the button out: a disabled button with no explanation is
  // indistinguishable from a broken one, and a 12-digit tax ID looks complete
  // at a glance.  The tax-ID count shows as soon as there is something to
  // count; the bare tr("admin.required") messages wait until Print is tapped so a
  // freshly-opened form isn't shouting at the cashier.
  const errors = {
    name: !form.name.trim() && attempted ? tr("admin.required") : "",
    tax_id: !form.tax_id.trim()
      ? (attempted ? tr("admin.required") : "")
      : taxDigits.length !== 13
        ? `A Thai tax ID is 13 digits — this has ${taxDigits.length}`
        : "",
    address: !form.address.trim() && attempted ? tr("admin.required") : "",
  };

  const saveAndPrint = async () => {
    setAttempted(true);
    if (!form.name.trim()) {
      showAlert(tr("admin.missing_details"), tr("admin.taxpayer_or_company_name_is_required"));
      return;
    }
    if (taxDigits.length !== 13) {
      showAlert(
        tr("admin.invalid_tax_id"),
        `A Thai tax ID is 13 digits — this has ${taxDigits.length}. Check it against the buyer's paperwork.`,
      );
      return;
    }
    if (!form.address.trim()) {
      showAlert(tr("admin.missing_details"), tr("admin.address_is_required"));
      return;
    }

    setBusy(tr("admin.updating"));
    const payload = {
      name: form.name.trim(),
      tax_id: taxDigits,
      tax_branch: form.tax_branch.trim(),
      address: form.address.trim(),
      phone: form.phone.trim(),
      email: form.email.trim(),
    };

    try {
      // Remember the details on the customer so the next invoice for this buyer
      // prefills.  Best-effort: this is a convenience, and failing it must not
      // stop the invoice the cashier is standing there waiting for.
      if (selected) {
        try {
          await apiFetch(`${API}/customers/${selected.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              tax_id: payload.tax_id,
              tax_branch: payload.tax_branch,
              address: payload.address,
              email: payload.email,
              ...(payload.phone ? { phone: payload.phone } : {}),
            }),
          });
        } catch {/* non-fatal — the invoice itself is what matters */}
      }

      const res = await apiFetch(`${API}/orders/${order.id}/tax-invoice`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, customer_id: selected?.id || "" }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `Server error (${res.status})`);
      }
      const updated: Order = await res.json();
      onOrderUpdated(updated);
      // Print from what the server stored, not the local form, so the slip can
      // never disagree with the buyer of record.
      onPrint(updated.pos_tax_invoice ?? payload);
    } catch (e: any) {
      showAlert(tr("admin.couldnt_save_tax_invoice"), e?.message || "Please try again.");
    } finally {
      setBusy(null);
    }
  };

  const title =
    step === "search" ? tr("admin.search_customer")
      : step === "add" ? tr("admin.add_customer_2")
        : tr("admin.full_tax_invoice");

  // The picker is the first screen of a fresh issue, so its left action closes
  // the whole flow.  A re-issue opens on the form with no picker behind it, so
  // there is nothing to go back to and its left action closes too.
  const showsBack = step === "add" || (step === "form" && !existing);

  return (
    <Modal visible transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.tiBackdrop}>
        <View style={styles.tiCard} testID="tax-invoice-flow">
          {/* ── Header ── */}
          <View style={styles.tiHeader}>
            {showsBack ? (
              <TouchableOpacity onPress={() => setStep("search")} testID="tax-invoice-back">
                <View style={styles.tiBackRow}>
                  <Ionicons name="chevron-back" size={20} color={C.brand} />
                  <Text style={styles.tiHeaderAction}>{tr("common.back")}</Text>
                </View>
              </TouchableOpacity>
            ) : (
              <TouchableOpacity onPress={onClose} testID="tax-invoice-close">
                <Text style={styles.tiHeaderAction}>{tr("admin.close")}</Text>
              </TouchableOpacity>
            )}
            <Text style={styles.tiTitle}>{title}</Text>
            {step === "search" ? (
              <TouchableOpacity onPress={() => setStep("add")} testID="tax-invoice-add-customer">
                <Ionicons name="create-outline" size={22} color={C.brand} />
              </TouchableOpacity>
            ) : (
              <View style={{ width: 22 }} />
            )}
          </View>

          {/* ── Step 1: pick the buyer ── */}
          {step === "search" && (
            <>
              <View style={styles.tiSearchBox}>
                <Ionicons name="search" size={16} color={C.ink3} />
                <TextInput
                  placeholder={tr("admin.search_by_name_or_phone")}
                  placeholderTextColor={C.ink3}
                  style={styles.tiSearchInput}
                  value={query}
                  onChangeText={setQuery}
                  testID="tax-invoice-search"
                />
              </View>
              {loading ? (
                <ActivityIndicator color={C.brand} style={{ marginTop: 32 }} />
              ) : filtered.length === 0 ? (
                <View style={styles.tiEmpty}>
                  <Text style={styles.emptyText}>
                    {customers.length === 0
                      ? tr("admin.no_customers_yet_tap_the_pencil")
                      : tr("admin.no_matching_customers")}
                  </Text>
                </View>
              ) : (
                <FlatList
                  data={filtered}
                  keyExtractor={(c) => c.id}
                  style={{ flexGrow: 0 }}
                  ItemSeparatorComponent={() => <View style={styles.divider} />}
                  renderItem={({ item }) => (
                    <TouchableOpacity
                      style={styles.tiCustRow}
                      onPress={() => pick(item)}
                      testID={`tax-invoice-cust-${item.id}`}
                    >
                      <View style={[styles.tiAvatar, { backgroundColor: item.color || C.ink3 }]}>
                        <Text style={styles.tiAvatarText}>
                          {(customerFullName(item)[0] || "?").toUpperCase()}
                        </Text>
                      </View>
                      <View style={{ flex: 1 }}>
                        <Text style={styles.tiCustName}>{customerFullName(item) || "—"}</Text>
                        <Text style={styles.tiCustSub}>
                          {item.phone || tr("admin.no_phone")}
                          {item.tax_id ? `   Tax ID ${item.tax_id}` : ""}
                        </Text>
                      </View>
                      {!!item.last_visit && (
                        <Text style={styles.tiCustVisit}>{tr("admin.last_purchase")} {item.last_visit}</Text>
                      )}
                    </TouchableOpacity>
                  )}
                />
              )}
            </>
          )}

          {/* ── Step 2: add a buyer ── */}
          {step === "add" && (
            <ScrollView contentContainerStyle={styles.tiForm} keyboardShouldPersistTaps="handled">
              <TiField label={tr("admin.first_name")} required value={add.name} onChange={setAddField("name")} testID="ti-add-first" />
              <TiField label={tr("admin.last_name")} required value={add.last_name} onChange={setAddField("last_name")} testID="ti-add-last" />
              <Text style={styles.tiLabel}>{tr("admin.gender")}</Text>
              <View style={styles.tiRadioRow}>
                {GENDERS.map((g) => {
                  const on = add.gender === g.key;
                  return (
                    <TouchableOpacity
                      key={g.key}
                      style={styles.tiRadio}
                      onPress={() => setAdd((a) => ({ ...a, gender: g.key }))}
                      testID={`ti-gender-${g.key}`}
                    >
                      <Ionicons
                        name={on ? "checkmark-circle" : "ellipse-outline"}
                        size={20}
                        color={on ? C.brand : C.lineStrong}
                      />
                      <Text style={styles.tiRadioText}>{tr(g.labelKey)}</Text>
                    </TouchableOpacity>
                  );
                })}
              </View>
              <Text style={styles.tiLabel}>{tr("common.phone_optional")}</Text>
              <PhoneInput
                value={add.phone}
                onChange={(e164) => setAddField("phone")(e164)}
                placeholder={tr("common.phone_optional")}
                defaultCountryCode="TH"
                testID="ti-add-phone"
              />
              <TiField
                label={tr("admin.date_of_birth_optional")}
                value={add.birth_date}
                onChange={setAddField("birth_date")}
                placeholder={tr("admin.yyyy_mm_dd")}
                testID="ti-add-dob"
              />
              <TiField
                label={tr("admin.customer_group_optional")}
                value={add.group}
                onChange={setAddField("group")}
                testID="ti-add-group"
              />
              <TouchableOpacity
                style={[styles.tiPrimaryBtn, (!add.name.trim() || !add.last_name.trim()) && { opacity: 0.4 }]}
                onPress={createCustomer}
                disabled={!add.name.trim() || !add.last_name.trim()}
                testID="ti-add-next"
              >
                <Text style={styles.tiPrimaryText}>{tr("admin.next")}</Text>
              </TouchableOpacity>
            </ScrollView>
          )}

          {/* ── Step 3: the invoice particulars ── */}
          {step === "form" && (
            <ScrollView contentContainerStyle={styles.tiForm} keyboardShouldPersistTaps="handled">
              <TiField
                label={tr("admin.taxpayer_name_or_company_name")}
                required
                value={form.name}
                onChange={setField("name")}
                error={errors.name}
                testID="ti-name"
              />
              <TiField
                label={tr("admin.tax_id_juristic_person_no")}
                required
                value={form.tax_id}
                onChange={setField("tax_id")}
                placeholder={tr("admin.i_e_1234567890121")}
                keyboardType="number-pad"
                maxLength={20}
                error={errors.tax_id}
                testID="ti-tax-id"
              />
              <TiField
                label={tr("admin.branch_name_optional")}
                value={form.tax_branch}
                onChange={setField("tax_branch")}
                placeholder={tr("admin.head_office")}
                testID="ti-branch"
              />
              <TiField
                label={tr("admin.address")}
                required
                value={form.address}
                onChange={setField("address")}
                multiline
                error={errors.address}
                testID="ti-address"
              />
              <TiField
                label={tr("common.phone_optional")}
                value={form.phone}
                onChange={setField("phone")}
                keyboardType="phone-pad"
                testID="ti-phone"
              />
              <TiField
                label={tr("admin.email_optional")}
                value={form.email}
                onChange={setField("email")}
                placeholder={tr("admin.abc_mail_com")}
                keyboardType="email-address"
                testID="ti-email"
              />
              {/* Deliberately always enabled.  Greying it out is what sent a
                  cashier hunting for a bug in a form that was simply one digit
                  short — tapping now always says what's wrong. */}
              <TouchableOpacity
                style={styles.tiPrimaryBtn}
                onPress={saveAndPrint}
                testID="ti-print"
              >
                <Text style={styles.tiPrimaryText}>{tr("admin.print")}</Text>
              </TouchableOpacity>
            </ScrollView>
          )}

          {/* Blocking overlay for the save round trip — mirrors the reference
              POS's "กำลังอัพเดตข้อมูล…" spinner. */}
          {!!busy && (
            <View style={styles.tiBusy}>
              <View style={styles.tiBusyCard}>
                <ActivityIndicator color={C.brand} />
                <Text style={styles.tiBusyText}>{busy}</Text>
              </View>
            </View>
          )}
        </View>
      </View>
    </Modal>
  );
}

// One labelled field in the tax-invoice forms.  Required fields carry the same
// red asterisk the reference POS uses.
function TiField({
  label,
  value,
  onChange,
  required,
  placeholder,
  multiline,
  keyboardType,
  maxLength,
  error,
  testID,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  required?: boolean;
  placeholder?: string;
  multiline?: boolean;
  keyboardType?: "default" | "number-pad" | "phone-pad" | "email-address";
  maxLength?: number;
  error?: string;
  testID?: string;
}) {
  return (
    <View>
      <Text style={styles.tiLabel}>
        {label}
        {required && <Text style={styles.tiRequired}>*</Text>}
      </Text>
      <TextInput
        style={[styles.input, multiline && styles.tiTextArea, !!error && styles.tiInputError]}
        value={value}
        onChangeText={onChange}
        placeholder={placeholder}
        placeholderTextColor={C.ink3}
        multiline={multiline}
        keyboardType={keyboardType}
        maxLength={maxLength}
        autoCapitalize={keyboardType === "email-address" ? "none" : "sentences"}
        testID={testID}
      />
      {!!error && (
        <Text style={styles.tiError} testID={testID ? `${testID}-error` : undefined}>
          {error}
        </Text>
      )}
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
// One labelled fact in the bill header — label above, value below, so the
// meta reads as a scannable grid rather than a stack of grey sentences.
function TdFact({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return (
    <View style={styles.tdFact}>
      <Text style={styles.tdFactLabel}>{label}</Text>
      <Text style={[styles.tdFactValue, danger && styles.tdFactValueDanger]} numberOfLines={2}>
        {value}
      </Text>
    </View>
  );
}

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
    case "delivery": return tr("admin.channel_delivery");
    case "kiosk": return tr("admin.channel_kiosk");
    case "table":
    case "other":
    default: return tr("admin.channel_store");
  }
}

// =================== INVENTORY ===================
const INV_TABS = [
  { k: "movement", l: "admin.stock_movement" },
  { k: "in", l: "admin.stock_in" },
  { k: "out", l: "admin.stock_out" },
  { k: "adjust", l: "admin.adjust_stock" },
  { k: "check", l: "admin.check_stock" },
] as const;
type InvTab = (typeof INV_TABS)[number]["k"];

function Inventory({ isWide }: { isWide: boolean }) {
  useT(); // re-render this screen when the language changes
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
      apiFetch(`${API}/categories`).then((r) => safeJson<Category[]>(r, [])),
      apiFetch(`${API}/products`).then((r) => safeJson<Product[]>(r, [])),
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
                {tr(t.l)}
              </Text>
            </TouchableOpacity>
          ))}
          <Ionicons name="chevron-forward" size={18} color={C.ink3} style={{ marginLeft: 4 }} />
        </ScrollView>
      </View>

      {isDocTab ? (
        <StockDocuments type={tab as DocType} products={products} categories={categories} onChanged={load} />
      ) : (
        <View style={[styles.twoCol, !isWide && styles.stackedCol, { flex: 1 }]}>
          {isWide ? (
            <View style={styles.leftNav}>
              <View style={styles.invSearchRow}>
                <Ionicons name="search" size={16} color={C.ink3} />
                <TextInput
                  style={styles.invSearchInput}
                  placeholder={tr("common.search")}
                  placeholderTextColor={C.ink3}
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
                    <Ionicons name="chevron-forward" size={14} color={C.ink3} />
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
                      <Text style={[styles.catChipText, active && { color: C.surface }]}>{c.name}</Text>
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
              <Text style={styles.sortLabel}>{tr("admin.sort")}</Text>
              {(["custom", "name", "inventory"] as const).map((s) => (
                <TouchableOpacity
                  key={s}
                  style={[styles.sortTab, sortBy === s && styles.sortTabActive]}
                  onPress={() => setSortBy(s)}
                  testID={`inv-sort-${s}`}
                >
                  <Text style={[styles.sortTabText, sortBy === s && styles.sortTabTextActive]}>
                    {s === "custom" ? tr("admin.custom") : s === "name" ? tr("common.name") : tr("admin.inventory")}
                  </Text>
                </TouchableOpacity>
              ))}
            </View>
            <FlatList
              data={filtered}
              keyExtractor={(i) => i.id}
              contentContainerStyle={{ padding: 14 }}
              refreshControl={
                <RefreshControl refreshing={refreshing} onRefresh={onRefresh} colors={[C.brand]} tintColor={C.brand} />
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
                      <Text style={styles.nonStockText}>{tr("admin.non_stock_product")}</Text>
                    ) : (
                      <>
                        <Text style={[styles.stockNum, item.stock <= 0 && { color: C.danger }]}>
                          {item.stock}
                        </Text>
                        <Text style={[styles.stockStatus, item.stock <= 0 && { color: C.danger }]}>
                          {item.stock <= 0 ? tr("admin.out_of_stock") : tr("admin.in_stock")}
                        </Text>
                      </>
                    )}
                  </View>
                  <Ionicons name="chevron-forward" size={16} color={C.lineStrong} />
                </TouchableOpacity>
              )}
              ListEmptyComponent={
                <View style={styles.emptyBox}>
                  <Text style={styles.emptyText}>{tr("admin.no_products")}</Text>
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
    title: "admin.create_stock_in_document", partyLabel: "admin.vendor",
    refLabel: "admin.purchasing_document_ref", refCol: "admin.purchasing_document_ref",
    hasParty: true, hasPrice: true, hasName: false, hasAdjustType: false, hasAvgCost: true,
    addBarLabel: "admin.items",
  },
  out: {
    mode: "purchase",
    title: "admin.create_stock_out_document", partyLabel: "admin.receiver",
    refLabel: "admin.ref_doc_no", refCol: "admin.ref_doc_no",
    hasParty: true, hasPrice: true, hasName: false, hasAdjustType: false, hasAvgCost: false,
    addBarLabel: "admin.items",
  },
  adjust: {
    mode: "reconcile",
    title: "admin.create_adjust_stock_document", refCol: "admin.document_name",
    hasParty: false, hasPrice: false, hasName: true, hasAdjustType: true, hasAvgCost: false,
    addBarLabel: "admin.search_products", reasonLabel: "admin.reason", mutates: true,
    reconcileCols: { before: "admin.before_adjust", input: "admin.qty_reconcile", result: "admin.update" },
  },
  check: {
    mode: "reconcile",
    title: "admin.create_check_stock_document", refCol: "admin.document_name",
    hasParty: false, hasPrice: false, hasName: true, hasAdjustType: false, hasAvgCost: false,
    addBarLabel: "admin.search_products", reasonLabel: "admin.note", mutates: false,
    reconcileCols: { before: "admin.before_count", input: "admin.counted_qty", result: "admin.difference" },
  },
};

// Document dates: month name in the UI language, Buddhist year, matching the
// stock paperwork staff file these against.
function thaiDate(d: Date): string {
  return `${d.getDate()} ${tr(`date.months.${d.getMonth()}`)} ${d.getFullYear() + BE_OFFSET}`;
}

// Document list for a given type (images 4 / adjust / check).
function StockDocuments({
  type, products, categories, onChanged,
}: {
  type: DocType; products: Product[]; categories: Category[]; onChanged: () => void;
}) {
  useT(); // re-render this screen when the language changes
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
          <Ionicons name="chevron-back" size={16} color={C.lineStrong} />
          <Text style={styles.docDateRangeText}>{rangeLabel}</Text>
          <Ionicons name="chevron-forward" size={16} color={C.lineStrong} />
        </View>
        {cfg.hasPrice && (
          <Text style={styles.docListTotal}>{tr("common.total")} <Text style={{ fontWeight: "700", color: C.ink }}>{total.toFixed(2)}</Text></Text>
        )}
        <TouchableOpacity style={styles.createDocBtn} onPress={() => setCreating(true)} testID="create-document">
          <Ionicons name="add" size={16} color={C.brand} />
          <Text style={styles.createDocBtnText}>{tr("admin.create_document")}</Text>
        </TouchableOpacity>
      </View>

      {/* column headers */}
      <View style={styles.docColHead}>
        <Text style={[styles.docColCell, { width: 150 }]}>{tr("admin.date")}</Text>
        <Text style={[styles.docColCell, { width: 150 }]}>{tr("admin.document_no")}</Text>
        <Text style={[styles.docColCell, { flex: 1 }]}>{cfg.refCol ? tr(cfg.refCol) : null}</Text>
        {type === "out" && <Text style={[styles.docColCell, { width: 150 }]}>{tr("admin.reason")}</Text>}
        {cfg.hasPrice && <Text style={[styles.docColCell, { width: 90, textAlign: "right" }]}>{tr("common.total")}</Text>}
        {cfg.hasAdjustType && <Text style={[styles.docColCell, { width: 110 }]}>{tr("admin.document_type")}</Text>}
        <Text style={[styles.docColCell, { width: 100, textAlign: "right" }]}>{tr("admin.created_by")}</Text>
      </View>

      {docs === null ? (
        <ActivityIndicator color={C.brand} style={{ marginTop: 40 }} />
      ) : docs.length === 0 ? (
        <View style={styles.emptyBox}><Text style={styles.emptyText}>{tr("admin.no_document")}</Text></View>
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
                <Text style={[styles.docCell, { width: 150, color: C.ink }]}>{item.document_no}</Text>
                <Text style={[styles.docCell, { flex: 1 }]} numberOfLines={1}>{refText}</Text>
                {type === "out" && (
                  // Blank for anything saved before reasons existed — those
                  // documents legitimately have none, so don't invent one.
                  <Text style={[styles.docCell, { width: 150 }]} numberOfLines={1}>
                    {item.reason || item.note || ""}
                  </Text>
                )}
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

// Predefined stock-out reason, managed per branch in Settings → Stock-out
// reasons.  Replaces the free-text remark staff used to type, which arrived
// spelled a different way every time and could not be grouped or counted.
type StockOutReason = {
  id: string; name: string; name_th?: string;
  sort_order?: number; active?: boolean;
};

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
  useT(); // re-render this screen when the language changes
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
  // Stock-out reason.  Kept separate from `reason` above, which belongs to the
  // reconcile flow — the two never coexist, but sharing one slot invites a
  // stray value from whichever form was opened last.
  const [outReason, setOutReason] = useState("");
  const [outReasons, setOutReasons] = useState<StockOutReason[]>([]);
  const [reasonPicker, setReasonPicker] = useState(false);

  useEffect(() => {
    if (visible) {
      setLines([]); setRef(""); setDocName(""); setParty(""); setNote(""); setReason("");
      setTaxIncluded(false); setAvgCost(false); setOutReason("");
    }
  }, [visible]);

  // Only the active list: a reason an admin deactivated should disappear from
  // the picker without disturbing the documents that already recorded it.
  useEffect(() => {
    if (!visible || type !== "out") return;
    let cancelled = false;
    (async () => {
      try {
        const r = await apiFetch(`${API}/stock-out-reasons?active=true`);
        const data = await r.json();
        if (!cancelled) setOutReasons(Array.isArray(data) ? data : []);
      } catch {
        if (!cancelled) setOutReasons([]);
      }
    })();
    return () => { cancelled = true; };
  }, [visible, type]);

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
      if (type === "out") { body.receiver = party; body.reason = outReason; }
    }
    try {
      await apiFetch(`${API}/stock-documents`, { method: "POST", body: JSON.stringify(body) });
      onSaved();
    } catch {}
    setSaving(false);
  };

  // A stock-out with no reason is the thing this feature exists to stop, so
  // Save stays disabled until one is picked rather than silently saving blank.
  const missingReason = type === "out" && !outReason;
  const canSave = lines.length > 0 && !missingReason;

  const confirmSave = () => {
    if (!canSave) return;
    if (Platform.OS === "web") { save(); return; }
    showAlert(tr("admin.confirm_save_document"), tr("admin.are_you_sure_you_want_to"), [
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
            <Ionicons name="chevron-back" size={22} color={C.ink} />
            <Text style={styles.docBackText}>{tr("common.back")}</Text>
          </TouchableOpacity>
          <Text style={styles.docTopTitle}>{tr(cfg.title)}</Text>
          <TouchableOpacity onPress={confirmSave} disabled={!canSave} testID="doc-save">
            <Text style={[styles.docSaveText, !canSave && { color: C.lineStrong }]}>{tr("common.save")}</Text>
          </TouchableOpacity>
        </View>

        <ScrollView keyboardShouldPersistTaps="handled">
          {/* ── header fields ── */}
          {reconcile ? (
            <View style={styles.docForm}>
              <View style={styles.docFormRow}>
                <TouchableOpacity style={[styles.docField, styles.importBtn]} onPress={() => setImportOpen(true)} testID="import-documents">
                  <Text style={styles.importBtnText}>{tr("admin.import_documents")}</Text>
                  <Ionicons name="chevron-forward" size={16} color={C.ink3} />
                </TouchableOpacity>
                <View style={[styles.docField, { flex: 2 }]}>
                  <TextInput
                    style={styles.docInput}
                    value={reason}
                    onChangeText={setReason}
                    placeholder={cfg.reasonLabel ? tr(cfg.reasonLabel) : undefined}
                    placeholderTextColor={C.ink3}
                    testID="reconcile-reason"
                  />
                </View>
              </View>
            </View>
          ) : (
            <View style={styles.docForm}>
              <View style={styles.docFormRow}>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>{type === "in" ? tr("admin.bill_date_ref") : tr("admin.date_ref")}</Text>
                  <Text style={styles.docFieldDate}>{thaiDate(new Date())}</Text>
                </View>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>{cfg.refLabel ? tr(cfg.refLabel) : null}</Text>
                  <TextInput style={styles.docInput} value={ref} onChangeText={setRef} placeholder="" />
                </View>
              </View>
              <View style={styles.docFormRow}>
                <View style={styles.docField}>
                  <Text style={styles.docFieldLabel}>{cfg.partyLabel ? tr(cfg.partyLabel) : null}</Text>
                  <TextInput style={styles.docInput} value={party} onChangeText={setParty} placeholder="" />
                </View>
                {type === "out" ? (
                  // Reason replaces the free-text note on a stock-out: the
                  // whole point is that the answer is one of a known set, so
                  // the export can group by it.
                  <View style={styles.docField}>
                    <Text style={styles.docFieldLabel}>{tr("admin.reason")}</Text>
                    <TouchableOpacity
                      style={[styles.docInput, styles.docSelect]}
                      onPress={() => setReasonPicker(true)}
                      testID="stock-out-reason"
                    >
                      <Text
                        style={[styles.docSelectText, !outReason && { color: C.ink3 }]}
                        numberOfLines={1}
                      >
                        {outReason || tr("admin.choose_a_reason")}
                      </Text>
                      <Ionicons name="chevron-down" size={16} color={C.ink3} />
                    </TouchableOpacity>
                  </View>
                ) : (
                  <View style={styles.docField}>
                    <Text style={styles.docFieldLabel}>{tr("admin.note")}</Text>
                    <TextInput style={styles.docInput} value={note} onChangeText={setNote} placeholder="" />
                  </View>
                )}
              </View>
            </View>
          )}

          {/* ── items table header ── */}
          <View style={styles.itemsHead}>
            <Text style={[styles.itemsHeadCell, { width: 30 }]}>#</Text>
            <Text style={[styles.itemsHeadCell, { width: 130 }]}>{tr("admin.barcode")}</Text>
            <Text style={[styles.itemsHeadCell, { flex: 1, textAlign: "left" }]}>{tr("admin.product_name_2")}</Text>
            {reconcile ? (
              <>
                <Text style={[styles.itemsHeadCell, { width: 90, textAlign: "right" }]}>{rc ? tr(rc.before) : null}</Text>
                <Text style={[styles.itemsHeadCell, { width: 90 }]}>{rc ? tr(rc.input) : null}</Text>
                <Text style={[styles.itemsHeadCell, { width: 80, textAlign: "right" }]}>{rc ? tr(rc.result) : null}</Text>
              </>
            ) : (
              <>
                <Text style={[styles.itemsHeadCell, { width: 70 }]}>{tr("common.quantity")}</Text>
                <Text style={[styles.itemsHeadCell, { width: 80 }]}>{tr("admin.price_unit")}</Text>
                <Text style={[styles.itemsHeadCell, { width: 70 }]}>{tr("common.discount")}</Text>
                <Text style={[styles.itemsHeadCell, { width: 80 }]}>{tr("common.total")}</Text>
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
                    <Text style={[styles.itemCellRO, { width: 80, textAlign: "right", color: d > 0 ? C.ok : d < 0 ? C.danger : C.ink2Soft }]}>
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
                  <Ionicons name="close-circle" size={18} color={C.danger} />
                </TouchableOpacity>
              </View>
            );
          })}

          <TouchableOpacity style={styles.itemsAddBar} onPress={() => setPicker(true)} testID="add-items">
            <Text style={styles.itemsAddBarText}>{tr(cfg.addBarLabel)}</Text>
          </TouchableOpacity>
        </ScrollView>

        {/* ── footer totals (purchase only) ── */}
        {!reconcile && (
          <View style={styles.docFooter}>
            {cfg.hasAvgCost && (
              <View style={styles.footToggle}>
                <Text style={styles.footToggleLabel}>{tr("admin.avg_cost_calculate")}</Text>
                <Switch value={avgCost} onValueChange={setAvgCost} trackColor={{ true: C.brand }} />
              </View>
            )}
            <View style={styles.footToggle}>
              <Text style={styles.footToggleLabel}>{tr("admin.tax_included")}</Text>
              <Switch value={taxIncluded} onValueChange={setTaxIncluded} trackColor={{ true: C.brand }} />
            </View>
            <View style={styles.footStat}><Text style={styles.footStatLabel}>{tr("common.total")}</Text><Text style={styles.footStatVal}>{subtotal.toFixed(2)}</Text></View>
            <View style={styles.footStat}><Text style={styles.footStatLabel}>{tr("common.discount")}</Text><Text style={styles.footStatVal}>{discountSum.toFixed(2)}</Text></View>
            <View style={styles.footStat}><Text style={styles.footStatLabel}>{tr("admin.tax_7_pct")}</Text><Text style={styles.footStatVal}>{tax.toFixed(2)}</Text></View>
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

        {/* Stock-out reason picker */}
        <Modal
          visible={reasonPicker}
          transparent
          animationType="fade"
          onRequestClose={() => setReasonPicker(false)}
        >
          <TouchableOpacity
            style={styles.modalOverlay}
            activeOpacity={1}
            onPress={() => setReasonPicker(false)}
          >
            <TouchableOpacity activeOpacity={1} style={styles.smallModal}>
              <Text style={[styles.modalTitle, { textAlign: "center", paddingTop: 18 }]}>{tr("admin.reason")}</Text>
              <ScrollView style={{ maxHeight: 360 }}>
                {outReasons.length === 0 ? (
                  <Text style={[styles.emptyText, { padding: 24 }]}>
                    {tr("admin.no_reasons_yet_add_them_in")}
                  </Text>
                ) : (
                  outReasons.map((r) => (
                    <TouchableOpacity
                      key={r.id}
                      style={styles.catRow}
                      onPress={() => { setOutReason(r.name); setReasonPicker(false); }}
                      testID={`stock-out-reason-${r.id}`}
                    >
                      <Text style={styles.catRowText}>{r.name}</Text>
                      {!!r.name_th && r.name_th !== r.name && (
                        <Text style={styles.catRowSub}>{r.name_th}</Text>
                      )}
                    </TouchableOpacity>
                  ))
                )}
              </ScrollView>
            </TouchableOpacity>
          </TouchableOpacity>
        </Modal>
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
  useT(); // re-render this screen when the language changes
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

  const curCatName = catId ? (categories.find((c) => c.id === catId)?.name || tr("admin.all_categories_2")) : tr("admin.all_categories_2");

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.pickerOverlay}>
        <View style={styles.pickerCard} testID="product-picker">
          <View style={styles.pickerHead}>
            <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color={C.danger} /></TouchableOpacity>
            <Text style={styles.pickerTitle}>{tr("admin.select_products")}</Text>
            <TouchableOpacity onPress={() => onDone(products.filter((p) => selected.has(p.id)))}>
              <Text style={styles.pickerDone}>{tr("admin.done")}</Text>
            </TouchableOpacity>
          </View>

          <TouchableOpacity style={styles.pickerCatRow} onPress={() => setCatOpen((o) => !o)}>
            <Text style={styles.pickerCatText}>{curCatName}</Text>
            <Ionicons name="chevron-forward" size={16} color={C.ink3} />
          </TouchableOpacity>
          {catOpen && (
            <View style={styles.pickerCatList}>
              <ScrollView style={{ maxHeight: 160 }}>
                <TouchableOpacity style={styles.pickerCatItem} onPress={() => { setCatId(""); setCatOpen(false); }}>
                  <Text style={styles.pickerCatItemText}>{tr("admin.all_categories")}</Text>
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
            <Ionicons name="search" size={16} color={C.ink3} />
            <TextInput style={styles.pickerSearchInput} placeholder={tr("common.search")} placeholderTextColor={C.ink3} value={search} onChangeText={setSearch} />
            <Ionicons name="barcode-outline" size={20} color={C.brand} />
          </View>

          <View style={styles.pickerSortRow}>
            <Text style={styles.sortLabel}>{tr("admin.sort")}</Text>
            {(["custom", "name"] as const).map((s) => (
              <TouchableOpacity key={s} style={[styles.sortTab, sort === s && styles.sortTabActive]} onPress={() => setSort(s)}>
                <Text style={[styles.sortTabText, sort === s && styles.sortTabTextActive]}>{s === "custom" ? tr("admin.custom") : tr("common.name")}</Text>
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
                    color={already ? C.lineStrong : checked ? C.brand : C.lineStrong}
                  />
                  <Image source={{ uri: item.image_base64 || item.image_url }} style={styles.pickerImg} />
                  <View style={{ flex: 1 }}>
                    <Text style={[styles.pickerName, already && { color: C.lineStrong }]} numberOfLines={1}>{item.name}</Text>
                    <Text style={styles.pickerBarcode}>{item.barcode}</Text>
                  </View>
                </TouchableOpacity>
              );
            }}
            ListEmptyComponent={<View style={styles.emptyBox}><Text style={styles.emptyText}>{tr("admin.no_products")}</Text></View>}
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
  useT(); // re-render this screen when the language changes
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
          <Text style={styles.keypadTitle}>{tr("common.amount")}</Text>
          <Text style={styles.keypadValue}>{val || "0"}</Text>
          <View style={styles.keypadGrid}>
            {keys.map((k) => (
              <TouchableOpacity key={k} style={styles.keypadKey} onPress={() => press(k)}>
                {k === "del"
                  ? <Ionicons name="backspace-outline" size={22} color={C.ink} />
                  : <Text style={styles.keypadKeyText}>{k}</Text>}
              </TouchableOpacity>
            ))}
          </View>
          <TouchableOpacity style={styles.keypadDone} onPress={() => onDone(val || "0")}>
            <Text style={styles.keypadDoneText}>{tr("admin.done")}</Text>
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
  useT(); // re-render this screen when the language changes
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
            <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color={C.danger} /></TouchableOpacity>
            <Text style={styles.pickerTitle}>{tr("admin.select_documents")}</Text>
            <TouchableOpacity
              style={styles.loadDocsBtn}
              onPress={() => onLoad((docs || []).filter((d) => selected.has(d.id)))}
            >
              <Text style={styles.loadDocsText}>{tr("admin.load_documents")}</Text>
            </TouchableOpacity>
          </View>

          <View style={styles.selDateRow}>
            <TouchableOpacity style={styles.selDateField} onPress={() => setCal("from")}>
              <Text style={styles.docFieldLabel}>{tr("admin.from_date")}</Text>
              <Text style={styles.selDateVal}>{from || tr("admin.select_date")}</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.selDateField} onPress={() => setCal("to")}>
              <Text style={styles.docFieldLabel}>{tr("admin.to_date")}</Text>
              <Text style={styles.selDateVal}>{to || tr("admin.select_date")}</Text>
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
            <Text style={[styles.docColCell, { width: 150 }]}>{tr("admin.date")}</Text>
            <Text style={[styles.docColCell, { flex: 1 }]}>{tr("admin.document_name")}</Text>
            <View style={{ width: 28 }} />
          </View>

          {docs === null ? (
            <ActivityIndicator color={C.brand} style={{ marginTop: 30 }} />
          ) : filtered.length === 0 ? (
            <View style={styles.emptyBox}><Text style={styles.emptyText}>{tr("admin.no_items")}</Text></View>
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
                      color={checked ? C.brand : C.lineStrong}
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
  useT(); // re-render this screen when the language changes
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
              <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color={C.ink2} /></TouchableOpacity>
              <Text style={styles.modalTitle}>{tr("admin.stock")} {product.name.slice(0, 24)}</Text>
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
                    <Text style={[styles.typeBtnText, type === t && { color: C.surface }]}>
                      {t === "in" ? tr("admin.stock_in") : t === "out" ? tr("admin.stock_out") : tr("admin.adjust")}
                    </Text>
                  </TouchableOpacity>
                ))}
              </View>
              <Text style={styles.formLabel}>{tr("admin.current_stock")} {product.stock}</Text>
              <TextInput
                placeholder={type === "adjust" ? tr("admin.new_stock_value") : tr("common.quantity")}
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
                <Text style={styles.primaryBtnText}>{tr("admin.save_movement")}</Text>
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
  useT(); // re-render this screen when the language changes
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
      showAlert(tr("common.couldnt_save_customer"), e?.message || "Please try again.");
      return;
    }
    if (!c || !c.name) {
      showAlert(tr("common.couldnt_save_customer"), tr("common.unexpected_response_from_server"));
      return;
    }
    setList((l) => [c, ...l]);
    setSel(c); setName(""); setPhone(""); setAddOpen(false);
  };

  return (
    <Body
      style={!isWide && { paddingHorizontal: 14 }}
      testID="customers-section"
    >
      <View style={[styles.filterRow, !isWide && { flexWrap: "wrap" }]}>
        <SearchField
          value={q}
          onChangeText={setQ}
          placeholder={tr("admin.phone_number_or_name")}
          height={40}
          style={{ flex: 1, maxWidth: isWide ? 340 : undefined, minWidth: 200 }}
          testID="cust-admin-search"
        />
        <Spacer />
        <Btn
          label={tr("admin.add_customer")}
          icon="add"
          variant="blue"
          height={40}
          onPress={() => setAddOpen(true)}
          testID="add-customer-admin"
        />
      </View>

      <View style={[styles.twoCol, !isWide && styles.stackedCol]}>
        <Panel style={{ flex: 1, minWidth: 0 }}>
          {isWide && <THead cols={CUST_COLS} />}
          {filtered.length === 0 ? (
            <Empty
              icon="people-outline"
              title={tr("common.no_customers")}
              note={q ? tr("admin.nothing_matches_that_search") : tr("admin.add_a_customer_to_start_tracking")}
            />
          ) : (
            <FlatList
              data={filtered}
              keyExtractor={(i) => i.id}
              showsVerticalScrollIndicator={false}
              renderItem={({ item }) => {
                const on = sel?.id === item.id;
                if (!isWide) {
                  return (
                    <TouchableOpacity
                      style={[styles.custAdminRow, on && styles.custAdminActive]}
                      onPress={() => setSel(item)}
                      testID={`cust-admin-${item.id}`}
                    >
                      <View style={[styles.custAv, { backgroundColor: item.color }]}>
                        <Text style={styles.custAvText}>
                          {item.name[0]?.toUpperCase() || "?"}
                        </Text>
                      </View>
                      <View style={{ flex: 1, minWidth: 0 }}>
                        <Text style={styles.custAdminName} numberOfLines={1}>{item.name}</Text>
                        {!!item.phone && (
                          <Money style={styles.custAdminPhone}>{item.phone}</Money>
                        )}
                      </View>
                    </TouchableOpacity>
                  );
                }
                return (
                  <TRow
                    selected={on}
                    onPress={() => setSel(item)}
                    testID={`cust-admin-${item.id}`}
                  >
                    <TCell col={CUST_COLS[0]}>
                      <View style={styles.rankRow}>
                        <View style={[styles.custAv, { backgroundColor: item.color }]}>
                          <Text style={styles.custAvText}>
                            {item.name[0]?.toUpperCase() || "?"}
                          </Text>
                        </View>
                        <TText strong numberOfLines={1}>{item.name}</TText>
                      </View>
                    </TCell>
                    <TCell col={CUST_COLS[1]}>
                      <TText mono muted={!item.phone}>{item.phone || "—"}</TText>
                    </TCell>
                    <TCell col={CUST_COLS[2]}>
                      <TText muted>{item.group || "—"}</TText>
                    </TCell>
                    <TCell col={CUST_COLS[3]}>
                      <TText mono muted={!item.last_visit}>
                        {item.last_visit
                          ? new Date(item.last_visit).toLocaleDateString("en-GB", {
                              day: "2-digit", month: "short",
                            })
                          : tr("admin.never")}
                      </TText>
                    </TCell>
                  </TRow>
                );
              }}
            />
          )}
        </Panel>

        {isWide && (
          <View style={styles.detailCol}>
            <Panel style={{ flex: 1 }}>
              {!sel ? (
                <Empty
                  icon="person-outline"
                  title={tr("admin.select_a_customer")}
                  note={tr("admin.their_spend_usual_order_and_recent")}
                />
              ) : (
                <ScrollView showsVerticalScrollIndicator={false}>
                  <View style={styles.custProfile}>
                    <View style={[styles.avBig, { backgroundColor: sel.color }]}>
                      <Text style={styles.avBigText}>
                        {sel.name[0]?.toUpperCase() || "?"}
                      </Text>
                    </View>
                    <Text style={styles.custProfileName}>{customerFullName(sel)}</Text>
                    {!!sel.phone && (
                      <Money style={styles.custProfileSub}>{sel.phone}</Money>
                    )}
                  </View>

                  {/* Three figures that answer "is this a good customer". */}
                  <View style={styles.kpis}>
                    <View style={styles.kpi}>
                      {statsLoading ? (
                        <ActivityIndicator size="small" color={C.brand} />
                      ) : (
                        <Money style={styles.kpiVal}>{String(stats?.bill_count ?? 0)}</Money>
                      )}
                      <Text style={styles.kpiLbl}>{tr("admin.bills_2")}</Text>
                    </View>
                    <View style={styles.kpi}>
                      {statsLoading ? (
                        <ActivityIndicator size="small" color={C.brand} />
                      ) : (
                        <Money style={styles.kpiVal}>{THB(stats?.avg_bill ?? 0)}</Money>
                      )}
                      <Text style={styles.kpiLbl}>{tr("admin.avg_bill")}</Text>
                    </View>
                    <View style={[styles.kpi, { borderRightWidth: 0 }]}>
                      {statsLoading ? (
                        <ActivityIndicator size="small" color={C.brand} />
                      ) : (
                        <Money style={styles.kpiVal}>{THB(stats?.success_total ?? 0)}</Money>
                      )}
                      <Text style={styles.kpiLbl}>{tr("admin.lifetime")}</Text>
                    </View>
                  </View>

                  {/* Money still owed is the one figure that needs chasing, so
                      it gets a standing red notice rather than a stat cell. */}
                  {!statsLoading && (stats?.outstanding_total ?? 0) > 0 && (
                    <View style={{ padding: 18 }}>
                      <Notice tone="danger">
                        {tr("admin.outstanding_across", { amount: THB(stats!.outstanding_total), count: stats!.outstanding_count })}
                      </Notice>
                    </View>
                  )}

                  {/* "The usual" is the feature — real loyalty at a shop this
                      size is a cashier who remembers. */}
                  <View style={styles.dsec}>
                    <Lbl style={{ marginBottom: 12 }}>{tr("admin.usual_order")}</Lbl>
                    {statsLoading ? (
                      <ActivityIndicator size="small" color={C.ink3} />
                    ) : stats?.top_products?.length ? (
                      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                        {stats.top_products.slice(0, 5).map((p) => (
                          <Tag key={p.product_id} tone="info">{p.name}</Tag>
                        ))}
                      </View>
                    ) : (
                      <Text style={styles.dsecEmpty}>{tr("admin.nothing_bought_yet")}</Text>
                    )}
                  </View>

                  <View style={styles.dsec}>
                    <Lbl style={{ marginBottom: 12 }}>{tr("admin.top_categories")}</Lbl>
                    {statsLoading ? (
                      <ActivityIndicator size="small" color={C.ink3} />
                    ) : stats?.top_categories?.length ? (
                      stats.top_categories.map((c, i) => (
                        <KV
                          key={c.name}
                          k={`${i + 1}. ${c.name}`}
                          v={THB(c.total)}
                          mono
                        />
                      ))
                    ) : (
                      <Text style={styles.dsecEmpty}>{tr("admin.no_sales_yet_2")}</Text>
                    )}
                  </View>
                </ScrollView>
              )}
            </Panel>
          </View>
        )}
      </View>

      <Modal visible={addOpen} transparent animationType="fade">
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setAddOpen(false)}>
                <Ionicons name="close" size={24} color={C.ink2} />
              </TouchableOpacity>
              <Text style={styles.modalTitle}>{tr("admin.new_customer")}</Text>
              <View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <TextInput placeholder={tr("common.name")} style={styles.formInput} value={name} onChangeText={setName} testID="admin-cust-name" />
              <PhoneInput
                value={phone}
                onChange={(e164, valid) => { setPhone(e164); setPhoneValid(valid); }}
                placeholder={tr("common.phone_optional")}
                defaultCountryCode="TH"
                testID="admin-cust-phone"
              />
              <Btn
                label={tr("common.save")}
                variant="blue"
                height={52}
                onPress={save}
                disabled={!name.trim() || !phoneValid}
                testID="admin-cust-save"
              />
            </View>
          </View>
        </View>
      </Modal>
    </Body>
  );
}

const CUST_COLS: Col[] = [
  { key: "name", title: "common.customer", flex: 2 },
  { key: "phone", title: "admin.phone", flex: 1.3 },
  { key: "group", title: "admin.group", flex: 1 },
  { key: "seen", title: "admin.last_seen", width: 96, right: true },
];


// =================== PRODUCTS ===================
function Products({ isWide, isAdmin }: { isWide: boolean; isAdmin: boolean }) {
  useT(); // re-render this screen when the language changes
  const [cats, setCats] = useState<Category[]>([]);
  const [prods, setProds] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("");
  const [edit, setEdit] = useState<Product | "new" | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"custom" | "name">("custom");
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    const [c, p] = await Promise.all([
      apiFetch(`${API}/categories`).then((r) => safeJson<Category[]>(r, [])),
      apiFetch(`${API}/products`).then((r) => safeJson<Product[]>(r, [])),
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

  // Margin sits next to price because that is the actual decision — nobody
  // edits a price in isolation, they are asking whether it still earns.
  const margin = (p: Product) =>
    p.price > 0 && p.cost > 0 ? ((p.price - p.cost) / p.price) * 100 : null;

  const { width: winW } = useWindowDimensions();
  const PROD_COLS = winW >= 1360 ? PROD_COLS_FULL : PROD_COLS_COMPACT;
  const col = (k: string) => PROD_COLS.find((c) => c.key === k)!;

  const unpriced = prods.filter((p) => p.price === 0).length;
  const uncosted = prods.filter((p) => p.cost === 0 && p.price !== 0).length;

  return (
    <Body
      style={!isWide && { paddingHorizontal: 14 }}
      testID="products-section"
    >
      <View style={[styles.filterRow, !isWide && { flexWrap: "wrap" }]}>
        <SearchField
          value={q}
          onChangeText={setQ}
          placeholder={tr("admin.name_or_thai_name")}
          height={40}
          style={{ flex: 1, maxWidth: isWide ? 320 : undefined, minWidth: 200 }}
          testID="admin-prod-search"
        />
        <Spacer />
        {/* One toggle, not two mutually exclusive pills — "custom order" is
            just the catalogue's own order, which is the default. */}
        <Pill
          label={tr("admin.sort_a_z")}
          active={sort === "name"}
          onPress={() => setSort(sort === "name" ? "custom" : "name")}
          testID="sort-name"
        />
        {isAdmin && (
          <Btn
            label={tr("admin.add_product")}
            icon="add"
            variant="blue"
            height={40}
            onPress={() => setEdit("new")}
            testID="add-product"
          />
        )}
      </View>

      {/* A standing count, not a dialog shown once — a product with no price
          rings up as free, so it must stay visible until it's fixed. */}
      {(unpriced > 0 || uncosted > 0) && (
        <Notice tone="warn">
          {[
            unpriced > 0 && tr("admin.products_no_price", { count: unpriced }),
            uncosted > 0 && tr("admin.products_no_cost", { count: uncosted }),
          ].filter(Boolean).join(" ")}
        </Notice>
      )}

      <View style={[styles.twoCol, !isWide && styles.stackedCol]}>
        {isWide ? (
          <ScrollView
            style={styles.catColAdmin}
            contentContainerStyle={{ gap: 3, paddingBottom: 8 }}
            showsVerticalScrollIndicator={false}
          >
            <Lbl style={{ paddingHorizontal: 16, paddingTop: 4, paddingBottom: 10 }}>
              {tr("admin.categories")}
            </Lbl>
            {cats.map((c) => {
              const active = activeCat === c.id && !q;
              const count = prods.filter((p) =>
                c.name === "Favorite" ? p.is_favorite : p.category_id === c.id,
              ).length;
              return (
                <TouchableOpacity
                  key={c.id}
                  style={[styles.catRowAdmin, active && styles.catRowAdminOn]}
                  onPress={() => { setActiveCat(c.id); setQ(""); }}
                  activeOpacity={0.8}
                  testID={`admin-cat-${c.id}`}
                >
                  <View style={[styles.catDot, { backgroundColor: c.color }]} />
                  <Text
                    style={[styles.catRowAdminText, active && { color: C.brand, fontWeight: "700" }]}
                    numberOfLines={1}
                  >
                    {c.name}
                  </Text>
                  <Money style={[styles.catRowAdminCount, active && { color: C.brand }]}>
                    {count}
                  </Money>
                </TouchableOpacity>
              );
            })}
          </ScrollView>
        ) : (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            style={{ flexGrow: 0 }}
            contentContainerStyle={{ gap: 8, paddingVertical: 4 }}
          >
            {cats.map((c) => (
              <Pill
                key={c.id}
                label={c.name}
                active={activeCat === c.id && !q}
                onPress={() => { setActiveCat(c.id); setQ(""); }}
                testID={`admin-cat-${c.id}`}
              />
            ))}
          </ScrollView>
        )}

        <Panel style={{ flex: 1, minWidth: 0 }}>
          <PanelHead
            title={q ? `Results for “${q}”` : curCat?.name || tr("common.products")}
            right={
              <Text style={styles.prodCount}>
                {filtered.length} {filtered.length === 1 ? tr("admin.product") : tr("admin.products")}
              </Text>
            }
          />
          {isWide && <THead cols={PROD_COLS} />}
          <FlatList
            data={filtered}
            keyExtractor={(i) => i.id}
            showsVerticalScrollIndicator={false}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={onRefresh} colors={[C.brand]} tintColor={C.brand} />
            }
            ListEmptyComponent={
              <Empty
                icon="cube-outline"
                title={tr("common.no_products_here")}
                note={q ? tr("admin.nothing_matches_that_search") : tr("admin.this_category_is_empty")}
              />
            }
            renderItem={({ item }) => {
              const m = margin(item);
              const img = item.image_base64 || item.image_url;

              if (!isWide) {
                return (
                  <TouchableOpacity
                    style={styles.prodMgmtRow}
                    onPress={() => isAdmin && setEdit(item)}
                    disabled={!isAdmin}
                    testID={`prod-${item.id}`}
                  >
                    {img ? (
                      <Image source={{ uri: img }} style={styles.thumb} />
                    ) : (
                      <View style={[styles.thumb, styles.thumbEmpty]}>
                        <Ionicons name="cafe-outline" size={20} color={C.ink3} />
                      </View>
                    )}
                    <View style={{ flex: 1, minWidth: 0 }}>
                      <Text style={styles.invName} numberOfLines={1}>{item.name}</Text>
                      <Money style={styles.prodPriceLabel}>
                        {`${THB(item.price)} · cost ${THB(item.cost)}`}
                      </Money>
                    </View>
                    {item.price === 0 ? (
                      <Tag tone="low">{tr("admin.no_price")}</Tag>
                    ) : m !== null ? (
                      <Tag tone={m >= 60 ? "ok" : "low"} mono>{`${m.toFixed(0)}%`}</Tag>
                    ) : null}
                  </TouchableOpacity>
                );
              }

              return (
                <TRow
                  onPress={isAdmin ? () => setEdit(item) : undefined}
                  testID={`prod-${item.id}`}
                >
                  <TCell col={col("name")}>
                    <View style={styles.rankRow}>
                      {img ? (
                        <Image source={{ uri: img }} style={styles.thumb} />
                      ) : (
                        <View style={[styles.thumb, styles.thumbEmpty]}>
                          <Ionicons name="cafe-outline" size={20} color={C.ink3} />
                        </View>
                      )}
                      <View style={{ flex: 1, minWidth: 0 }}>
                        <TText strong numberOfLines={1}>{item.name}</TText>
                        <Text style={styles.prodSub} numberOfLines={1}>
                          {[item.name_th, item.barcode].filter(Boolean).join(" · ") || "—"}
                        </Text>
                      </View>
                    </View>
                  </TCell>
                  <TCell col={col("price")}>
                    <TText mono strong color={item.price === 0 ? C.danger : undefined}>
                      {THB(item.price)}
                    </TText>
                  </TCell>
                  {PROD_COLS.length === PROD_COLS_FULL.length && (
                    <TCell col={col("cost")}>
                      <TText mono muted>{THB(item.cost)}</TText>
                    </TCell>
                  )}
                  <TCell col={col("margin")}>
                    {m === null ? (
                      <TText muted>—</TText>
                    ) : (
                      // Amber marks anything below the 60% house target,
                      // without shouting about it.
                      <TText mono strong color={m >= 60 ? C.ok : C.warn}>
                        {`${m.toFixed(0)}%`}
                      </TText>
                    )}
                  </TCell>
                  <TCell col={col("stock")}>
                    {/* Out-of-stock products still show, greyed in place,
                        rather than disappearing into an archive tab. */}
                    {item.stock <= 0 ? (
                      <Tag tone="out">{tr("admin.sold_out")}</Tag>
                    ) : item.stock <= 5 ? (
                      <Tag tone="low" mono>{`${item.stock} left`}</Tag>
                    ) : (
                      <Tag tone="ok" mono>{String(item.stock)}</Tag>
                    )}
                  </TCell>
                  <TCell col={col("fav")}>
                    {/* This is `is_favorite`: it only decides whether the
                        product shows under the Favourites filter. It does NOT
                        take anything off sale — the design called this column
                        "On sale screen", which would be a dangerous lie here,
                        because the product stays sellable under its category. */}
                    <Toggle
                      on={item.is_favorite}
                      onPress={() => toggleFav(item)}
                      disabled={!isAdmin}
                      testID={`fav-${item.id}`}
                    />
                  </TCell>
                </TRow>
              );
            }}
          />
          {!isAdmin && (
            <View style={styles.prodFootNote}>
              <Ionicons name="shield-outline" size={16} color={C.ink2Soft} />
              <Text style={styles.prodFootText}>
                {tr("admin.price_and_menu_changes_are_admin")}
              </Text>
            </View>
          )}
        </Panel>
      </View>

      <ProductEditModal
        product={edit}
        categories={cats.filter((c) => c.name !== "Favorite")}
        defaultCat={activeCat}
        onClose={() => setEdit(null)}
        onSaved={() => { setEdit(null); load(); }}
      />
    </Body>
  );
}

// Fixed pixel widths were the design's, drawn at 1536 — on a narrower panel
// they add up to more than the row has and the name column collapses to a
// single letter. Flex lets every column shrink together instead.
const PROD_COLS_FULL: Col[] = [
  { key: "name", title: "admin.product_2", flex: 3 },
  { key: "price", title: "admin.price", flex: 1.1, right: true },
  { key: "cost", title: "admin.cost", flex: 1, right: true },
  { key: "margin", title: "admin.margin", flex: 0.9, right: true },
  { key: "stock", title: "common.stock", flex: 1.1, right: true },
  { key: "fav", title: "admin.favourite", flex: 1, right: true },
];
// Cost is the first thing to go: margin already carries it, and the name is
// what makes a row identifiable at all.
const PROD_COLS_COMPACT: Col[] = PROD_COLS_FULL.filter((c) => c.key !== "cost");


function ProductEditModal({
  product, categories, defaultCat, onClose, onSaved,
}: {
  product: Product | "new" | null;
  categories: Category[];
  defaultCat: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  useT(); // re-render this screen when the language changes
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
      // 512px matches the server's cap (bravepos.images.MAX_DIM). It used to be
      // 800, which the server would then downscale on save — a second lossy
      // re-encode of an already-lossy JPEG, for an image only ever shown as a
      // thumbnail. Matching the cap means what we send is what gets stored.
      const manipulated = await ImageManipulator.manipulateAsync(
        original.uri,
        [{ resize: { width: 512 } }],
        { compress: 0.7, format: ImageManipulator.SaveFormat.JPEG, base64: true },
      );
      if (manipulated.base64) {
        setImgBase64(`data:image/jpeg;base64,${manipulated.base64}`);
        setImg(""); // clear the URL field so the new image wins
      }
    } catch (e) {
      console.warn(tr("admin.image_pick_failed"), e);
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
              <TouchableOpacity onPress={onClose}><Ionicons name="close" size={24} color={C.ink2} /></TouchableOpacity>
              <Text style={styles.modalTitle}>{isNew ? tr("admin.add_product_2") : tr("admin.edit_product")}</Text>
              <View style={{ width: 24 }} />
            </View>
            <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>{tr("common.name")}</Text>
              <TextInput style={styles.formInput} value={name} onChangeText={setName} placeholder={tr("admin.product_name")} testID="prod-name" />
              <View style={{ flexDirection: "row", gap: 12 }}>
                <View style={{ flex: 1, gap: 6 }}>
                  <Text style={styles.formLabel}>{tr("admin.price_thb")}</Text>
                  <TextInput style={styles.formInput} value={price} onChangeText={setPrice} keyboardType="decimal-pad" placeholder="0.00" testID="prod-price" />
                </View>
                <View style={{ flex: 1, gap: 6 }}>
                  <Text style={styles.formLabel}>{tr("admin.cost_thb")}</Text>
                  <TextInput style={styles.formInput} value={cost} onChangeText={setCost} keyboardType="decimal-pad" placeholder="0.00" testID="prod-cost" />
                </View>
              </View>
              <Text style={styles.formLabel}>{tr("common.stock")}</Text>
              <TextInput style={styles.formInput} value={stock} onChangeText={setStock} keyboardType="number-pad" placeholder="0" testID="prod-stock" />
              <Text style={styles.formLabel}>{tr("admin.image")}</Text>
              <View style={{ flexDirection: "row", alignItems: "center", gap: 12 }}>
                <View style={styles.imgThumb}>
                  {imgBase64 || img ? (
                    <Image source={{ uri: imgBase64 || img }} style={styles.imgThumbImage} />
                  ) : (
                    <Ionicons name="image-outline" size={36} color={C.ink3} />
                  )}
                </View>
                <View style={{ flex: 1, gap: 8 }}>
                  <TouchableOpacity
                    style={styles.imgPickBtn}
                    onPress={pickImage}
                    disabled={pickingImage}
                    testID="prod-img-pick"
                  >
                    <Ionicons name="camera-outline" size={18} color={C.ink} />
                    <Text style={styles.imgPickBtnText}>
                      {pickingImage ? tr("admin.loading") : (imgBase64 || img ? tr("admin.change_image") : tr("admin.choose_image"))}
                    </Text>
                  </TouchableOpacity>
                  {(imgBase64 || img) && (
                    <TouchableOpacity onPress={clearImage} testID="prod-img-clear">
                      <Text style={styles.imgClearText}>{tr("admin.remove_image")}</Text>
                    </TouchableOpacity>
                  )}
                </View>
              </View>
              <Text style={styles.formLabel}>{tr("admin.category")}</Text>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
                {categories.map((c) => (
                  <TouchableOpacity
                    key={c.id}
                    style={[styles.catPick, catId === c.id && styles.catPickActive]}
                    onPress={() => setCatId(c.id)}
                  >
                    <Text style={[styles.catPickText, catId === c.id && { color: C.surface }]}>
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
                  color={fav ? C.brand : C.ink3}
                />
                <Text style={{ color: fav ? C.brand : C.ink2, fontWeight: "600" }}>
                  {fav ? tr("common.favorite") : tr("admin.mark_as_favorite")}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.primaryBtn} onPress={save} testID="prod-save">
                <Text style={styles.primaryBtnText}>{isNew ? tr("admin.create") : tr("admin.save_changes")}</Text>
              </TouchableOpacity>
              {!isNew && (
                <TouchableOpacity style={styles.dangerBtn} onPress={del} testID="prod-delete">
                  <Ionicons name="trash-outline" size={16} color={C.danger} />
                  <Text style={{ color: C.danger, fontWeight: "600" }}>{tr("admin.delete_product")}</Text>
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
  useT(); // re-render this screen when the language changes
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

  const load = useCallback(async () => {
    const [cur, hist] = await Promise.all([
      apiFetch(`${API}/shifts/current`).then((r) => safeJson<any>(r, null)),
      apiFetch(`${API}/shifts`).then((r) => safeJson<any[]>(r, [])),
    ]);
    setCurrent(cur && cur.id ? cur : null);
    setHistory(hist || []);
  }, []);
  // Re-read on focus, not just on mount.  The router keeps this screen alive
  // behind the POS, so a cashier who rings up a sale and comes straight back
  // would otherwise be looking at the drawer as it stood before the sale.
  useFocusEffect(useCallback(() => { load(); }, [load]));

  // Pull the reason-code list for whichever side (Cash In / Cash Out) is open.
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
      showAlert(tr("admin.counted_cash_required"), tr("admin.enter_the_actual_amount_counted_in"));
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
      console.error(tr("admin.shift_summary_print_failed"), e);
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
        // Unlike the read-only loads above this must NOT fall back to empty —
        // printing a blank drawer summary is worse than not printing.  Fail on
        // a bad response, but with a message that names the real fault instead
        // of the "Unexpected character: <" you get from parsing an error page.
        apiFetch(`${API}/shifts/${shiftId}/summary`).then(async (r) => {
          if (!r.ok) throw new Error(`Shift summary fetch failed (HTTP ${r.status})`);
          return safeJson<any>(r, null);
        }),
        fetchShop(),
      ]);
      if (!summary) throw new Error("Shift summary response was not valid JSON");
      await printShiftSummary(summary, shop);
    } catch (e) {
      console.error(tr("admin.reprint_summary_failed"), e);
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

  // Take the server's figure rather than recomputing. The local formula left
  // cash sales out entirely, so the screen and the close-shift slip disagreed
  // by exactly the round's cash takings.
  const expected = current ? current.expected_in_drawer || 0 : 0;

  const fmtDT = (iso?: string) =>
    iso ? new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }).replace(",", "") : "-";

  return (
    <View style={{ flex: 1 }} testID="drawer-section">
      <Text style={styles.shiftHeader}>{tr("admin.shift")}</Text>

      {tab === "shift" ? (
        <ScrollView contentContainerStyle={{ padding: 16 }}>
          {!current ? (
            <View style={styles.shiftCard}>
              <View style={styles.emptyBox}>
                <Ionicons name="file-tray-outline" size={40} color={C.lineStrong} />
                <Text style={styles.emptyText}>{tr("admin.no_open_shift")}</Text>
                <TouchableOpacity style={[styles.primaryBtn, { marginTop: 14 }]} onPress={() => setOpenDlg(true)} testID="open-shift">
                  <Text style={styles.primaryBtnText}>{tr("common.open_shift")}</Text>
                </TouchableOpacity>
              </View>
            </View>
          ) : (
            <>
              <View style={styles.shiftCard}>
                <ShiftRow label={tr("admin.round")} value={String(current.round_number)} strong />
                <ShiftRow label={tr("admin.start_cash_in_drawer")} value={current.start_cash.toFixed(2)} />
                <ShiftRow label={tr("admin.shift_opened")} value={fmtDT(current.opened_at)} />
                <ShiftRow label={tr("admin.shift_opened_by")} value={current.opened_by} />
              </View>
              <View style={styles.shiftCard}>
                <ShiftRow label={tr("admin.total_sales_cash")} value={current.total_sales_cash.toFixed(2)} />
                <ShiftRow label={tr("admin.total_cash_in")} value={current.total_paid_in.toFixed(2)} />
                <ShiftRow label={tr("admin.total_cash_out")} value={current.total_paid_out.toFixed(2)} />
                <ShiftRow label={tr("admin.expected_in_drawer")} value={expected.toFixed(2)} />
              </View>
              <View style={styles.inOutRow}>
                <TouchableOpacity style={styles.inOutBtn} onPress={() => setMoveDlg("paid_in")} testID="paid-in">
                  <Text style={[styles.inOutText, { color: C.ok }]}>{tr("admin.cash_in")}</Text>
                </TouchableOpacity>
                <TouchableOpacity style={styles.inOutBtn} onPress={() => setMoveDlg("paid_out")} testID="paid-out">
                  <Text style={[styles.inOutText, { color: C.danger }]}>{tr("admin.cash_out")}</Text>
                </TouchableOpacity>
              </View>
              <TouchableOpacity style={styles.closeShiftBtn} onPress={() => setCloseDlg(true)} testID="close-shift">
                <Text style={styles.closeShiftText}>{tr("admin.close_shift")}</Text>
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
          <Ionicons name="file-tray-outline" size={16} color={tab === "shift" ? C.brand : C.ink2} />
          <Text style={[styles.invTabText, tab === "shift" && { color: C.brand }]}>{tr("admin.shift")}</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.invTab, tab === "history" && styles.invTabActive]} onPress={() => setTab("history")} testID="history-tab">
          <Ionicons name="time-outline" size={16} color={tab === "history" ? C.brand : C.ink2} />
          <Text style={[styles.invTabText, tab === "history" && { color: C.brand }]}>{tr("admin.history")}</Text>
        </TouchableOpacity>
      </View>

      {/* Open Shift dialog */}
      <Modal visible={openDlg} transparent animationType="fade" onRequestClose={() => setOpenDlg(false)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setOpenDlg(false)}><Ionicons name="close" size={24} color={C.ink2} /></TouchableOpacity>
              <Text style={styles.modalTitle}>{tr("common.open_shift")}</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>{tr("common.start_cash_in_drawer_thb")}</Text>
              <TextInput style={styles.formInput} value={startCash} onChangeText={setStartCash} keyboardType="decimal-pad" testID="start-cash" />
              <TouchableOpacity style={styles.primaryBtn} onPress={openShift} testID="confirm-open-shift">
                <Text style={styles.primaryBtnText}>{tr("common.open_shift")}</Text>
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
              <TouchableOpacity onPress={() => setCloseDlg(false)}><Ionicons name="close" size={24} color={C.danger} /></TouchableOpacity>
              <Text style={styles.modalTitle}>{tr("admin.actual_in_drawer")}</Text><View style={{ width: 24 }} />
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
                <Ionicons name="save-outline" size={18} color={C.surface} style={{ marginRight: 6 }} />
                <Text style={styles.closeShiftText}>{tr("admin.close_shift")}</Text>
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
              <TouchableOpacity onPress={closeMoveDlg}><Ionicons name="close" size={24} color={C.ink2} /></TouchableOpacity>
              <Text style={styles.modalTitle}>{moveDlg === "paid_in" ? tr("admin.cash_in") : tr("admin.cash_out")}</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <View style={styles.moveRow}>
                <Text style={styles.moveRowLabel}>{tr("common.amount")}</Text>
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
                <Text style={styles.moveRowLabel}>{tr("admin.category")}</Text>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 4 }}>
                  <Text style={[styles.moveRowValue, !moveCat && { color: C.ink3 }]}>
                    {moveCat || tr("admin.choose_category")}
                  </Text>
                  <Ionicons name="chevron-forward" size={16} color={C.ink3} />
                </View>
              </TouchableOpacity>
              <Text style={styles.formLabel}>{tr("admin.description")}</Text>
              <TextInput
                style={[styles.formInput, { height: 96, textAlignVertical: "top" }]}
                value={moveNote}
                onChangeText={setMoveNote}
                placeholder=""
                multiline
                testID="move-note"
              />
              <TouchableOpacity
                style={[styles.primaryBtn, { backgroundColor: moveDlg === "paid_in" ? C.ok : C.danger }]}
                onPress={addMovement}
                testID="confirm-movement"
              >
                <Text style={styles.primaryBtnText}>{moveDlg === "paid_in" ? tr("admin.cash_in") : tr("admin.cash_out")}</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* Category picker — slides over the Cash In/Out dialog */}
      <Modal visible={showCatPicker} transparent animationType="fade" onRequestClose={() => setShowCatPicker(false)}>
        <TouchableOpacity style={styles.modalOverlay} activeOpacity={1} onPress={() => setShowCatPicker(false)}>
          <TouchableOpacity activeOpacity={1} style={styles.smallModal}>
            <Text style={[styles.modalTitle, { textAlign: "center", paddingTop: 18 }]}>{tr("admin.category")}</Text>
            <ScrollView style={{ maxHeight: 360 }}>
              {cats.length === 0 ? (
                <Text style={[styles.emptyText, { padding: 24 }]}>{tr("admin.no_categories_yet")}</Text>
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
            <ActivityIndicator color={C.brand} size="large" />
            <Text style={styles.printingText}>{tr("admin.printing")}</Text>
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
// Month names follow the UI language; the +543 Buddhist year is applied
// alongside them (see BE_OFFSET below).
const monthShort = (i: number) => tr(`date.monthsShort.${i}`);

const isoToDate = (iso: string) => new Date(iso + "T00:00:00");
const shiftIsoDays = (iso: string, delta: number) => {
  const d = isoToDate(iso);
  d.setDate(d.getDate() + delta);
  return fmtISO(d);
};
const fmtRangeLabel = (r: DateRange) => {
  if (!r.start) return tr("admin.select_date");
  const s = isoToDate(r.start);
  const e = isoToDate(r.end || r.start);
  return `${s.getDate()} ${monthShort(s.getMonth())} ${s.getFullYear() + BE_OFFSET} - ` +
    `${e.getDate()} ${monthShort(e.getMonth())} ${e.getFullYear() + BE_OFFSET}`;
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
  useT(); // re-render this screen when the language changes
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
        <ShiftRow label={tr("admin.round")} value={String(selected.round_number)} strong />
        <ShiftRow label={tr("admin.shift_opened")} value={fmtDT(selected.opened_at)} />
        <ShiftRow label={tr("admin.shift_opened_by")} value={selected.opened_by || "-"} />
        <ShiftRow label={tr("admin.shift_closed")} value={fmtDT(selected.closed_at)} />
        <ShiftRow label={tr("admin.shift_closed_by")} value={selected.closed_by || "-"} />
      </View>
      <View style={styles.histCard}>
        <ShiftRow label={tr("admin.total_sales_cash")} value={(selected.total_sales_cash || 0).toFixed(2)} />
        <ShiftRow label={tr("admin.start_drawer")} value={(selected.start_cash || 0).toFixed(2)} />
        <View style={styles.shiftRow}>
          <Text style={styles.shiftLabel}>{tr("admin.cash_in")}</Text>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <Text style={styles.shiftVal}>{(selected.total_paid_in || 0).toFixed(2)}</Text>
            <Ionicons name="chevron-forward" size={14} color={C.ink3} />
          </View>
        </View>
        <View style={styles.shiftRow}>
          <Text style={styles.shiftLabel}>{tr("admin.cash_out")}</Text>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <Text style={[styles.shiftVal, { color: C.danger }]}>{(selected.total_paid_out || 0).toFixed(2)}</Text>
            <Ionicons name="chevron-forward" size={14} color={C.ink3} />
          </View>
        </View>
        <ShiftRow label={tr("admin.actual_in_drawer")} value={(selected.actual_in_drawer ?? 0).toFixed(2)} />
        <ShiftRow label={tr("admin.expected_in_drawer")} value={(selected.expected_in_drawer || 0).toFixed(2)} />
        <ShiftRow
          label={tr("admin.difference")}
          value={((selected.actual_in_drawer ?? 0) - (selected.expected_in_drawer || 0)).toFixed(2)}
        />
      </View>
      {selected.status !== "open" && (
        <TouchableOpacity
          style={styles.histReprintBtn}
          onPress={() => onReprint(selected.id)}
          testID={`shift-reprint-${selected.id}`}
        >
          <Ionicons name="print-outline" size={18} color={C.brand} />
          <Text style={styles.histReprintText}>{tr("admin.reprint_summary")}</Text>
        </TouchableOpacity>
      )}
    </ScrollView>
  ) : (
    <View style={styles.emptyBox}>
      <Ionicons name="time-outline" size={40} color={C.lineStrong} />
      <Text style={styles.emptyText}>{tr("admin.pick_a_shift_to_see_details")}</Text>
    </View>
  );

  const listPanel = (
    <View style={styles.histListPanel}>
      <View style={styles.histRangeRow}>
        <TouchableOpacity onPress={() => shiftRange(-10)} testID="hist-range-prev">
          <Ionicons name="chevron-back" size={20} color={C.brand} />
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.histRangeLabelBtn}
          onPress={() => setShowCalendar(true)}
          testID="hist-range-open"
        >
          <Text style={styles.histRangeLabel}>{fmtRangeLabel(range)}</Text>
        </TouchableOpacity>
        <TouchableOpacity onPress={() => shiftRange(10)} testID="hist-range-next">
          <Ionicons name="chevron-forward" size={20} color={C.brand} />
        </TouchableOpacity>
      </View>
      <ScrollView contentContainerStyle={{ paddingBottom: 16 }}>
        {grouped.length === 0 ? (
          <View style={styles.emptyBox}>
            <Text style={styles.emptyText}>{tr("admin.no_shifts_in_this_range")}</Text>
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
                      <Text style={styles.histListRound}>{tr("admin.round")} {row.round_number}</Text>
                      <Text style={styles.histListSub}>
                        {tr("admin.end_drawer")} {fmtTimeOfDay(row.closed_at || row.opened_at)}
                      </Text>
                    </View>
                    <Ionicons name="chevron-forward" size={18} color={C.ink3} />
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
  useT(); // re-render this screen when the language changes
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
        <Text style={styles.h2}>{tr("admin.self_order_qr")}</Text>
        <Text style={{ color: C.ink2Soft, marginTop: 8 }}>
          {tr("admin.no_active_branch_on_this_device")}
        </Text>
      </ScrollView>
    );
  }

  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 16, alignItems: "center" }}>
      <Text style={[styles.h2, { alignSelf: "flex-start" }]}>{tr("admin.self_order_qr")}</Text>
      <Text style={{ color: C.ink2, alignSelf: "flex-start", marginTop: -6 }}>
        {tr("admin.customers_scan_this_to_order_and")}{" "}
        <Text style={{ fontWeight: "700" }}>{branchName || tr("admin.this_branch")}</Text>.
      </Text>

      <View
        style={{
          backgroundColor: C.surface,
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
            <Text style={{ color: C.ink3 }}>{tr("admin.couldnt_render_qr")}</Text>
          </View>
        )}
        <Text style={{ fontWeight: "800", fontSize: 16, marginTop: 12, color: C.ink }}>
          {branchName || tr("admin.self_order")}
        </Text>
      </View>

      <View
        style={{
          backgroundColor: C.bgSoft,
          borderRadius: 12,
          padding: 12,
          width: "100%",
          maxWidth: 360,
          borderWidth: 1,
          borderColor: C.line,
        }}
      >
        <Text style={{ color: C.ink2Soft, fontSize: 12, marginBottom: 4 }}>{tr("admin.link")}</Text>
        <Text selectable style={{ color: C.ink, fontSize: 13 }}>{url}</Text>
      </View>

      <View style={{ flexDirection: "row", gap: 12, marginTop: 4 }}>
        <TouchableOpacity
          onPress={share}
          style={{
            flexDirection: "row", alignItems: "center", gap: 8,
            backgroundColor: C.brand, paddingVertical: 12, paddingHorizontal: 22,
            borderRadius: 12,
          }}
        >
          <Ionicons name="share-outline" size={18} color={C.surface} />
          <Text style={{ color: C.surface, fontWeight: "700" }}>{tr("admin.share_link")}</Text>
        </TouchableOpacity>
        <TouchableOpacity
          onPress={() => Linking.openURL(url)}
          style={{
            flexDirection: "row", alignItems: "center", gap: 8,
            backgroundColor: C.surface, paddingVertical: 12, paddingHorizontal: 22,
            borderRadius: 12, borderWidth: 1.5, borderColor: C.brand,
          }}
        >
          <Ionicons name="open-outline" size={18} color={C.brand} />
          <Text style={{ color: C.brand, fontWeight: "700" }}>{tr("admin.open")}</Text>
        </TouchableOpacity>
      </View>

      <Text style={{ color: C.ink3, fontSize: 12, textAlign: "center", maxWidth: 340, marginTop: 4 }}>
        {tr("admin.print_this_and_place_it_on")}
      </Text>
    </ScrollView>
  );
}

function SettingsView({ isWide, branchId, branchName }: { isWide: boolean; branchId: string; branchName: string }) {
  useT(); // re-render this screen when the language changes
  // `key` is the contract, `labelKey` the presentation. The detail pane below
  // dispatches on `key`, so it must stay a stable English identifier — putting
  // translated text here would break every section the moment the language
  // changed, since `active === "Printers"` would never match "เครื่องพิมพ์".
  const sections: { key: string; labelKey: string; icon: any; color: string }[] = [
    { key: "Store profile", labelKey: "admin.store_profile", icon: "home", color: C.danger },
    { key: "Tables & zones", labelKey: "admin.tables_and_zones", icon: "grid", color: "#3B82F6" },
    { key: "Language", labelKey: "admin.language", icon: "language", color: "#8B5CF6" },
    { key: "Receipt", labelKey: "admin.receipt_2", icon: "receipt", color: C.danger },
    // No Payment section: gateway credentials and the test/live lane are
    // backoffice-only. A tablet on a shop counter has no business learning the
    // merchant account or changing where money lands. /api/settings strips the
    // payment fields in both directions, so there is nothing here to edit.
    { key: "Self-Order QR", labelKey: "admin.self_order_qr", icon: "qr-code", color: C.brand },
    { key: "Cash drawer", labelKey: "admin.cash_drawer", icon: "calculator", color: "#06B6D4" },
    { key: "Stock-out reasons", labelKey: "admin.stock_out_reasons", icon: "arrow-undo", color: "#F59E0B" },
    { key: "Delivery partners", labelKey: "admin.delivery_partners", icon: "link", color: "#EC4899" },
    { key: "Printers", labelKey: "admin.printers", icon: "print", color: C.ok },
    { key: "Second screen", labelKey: "admin.second_screen", icon: "tv", color: "#3B82F6" },
    { key: "Users", labelKey: "admin.users", icon: "person", color: "#F97316" },
    { key: "Advanced", labelKey: "admin.advanced", icon: "settings", color: C.ink2Soft },
    { key: "Backup & Restore", labelKey: "admin.backup_and_restore", icon: "cloud-upload", color: "#A855F7" },
    { key: "Sync", labelKey: "admin.sync", icon: "sync", color: "#06B6D4" },
    { key: "Loyalty", labelKey: "admin.loyalty", icon: "star", color: "#EAB308" },
    { key: "Add-ons", labelKey: "admin.add_ons", icon: "extension-puzzle", color: "#14B8A6" },
  ];
  // Must match a `sections` entry exactly — the panel below dispatches on this
  // string, so a name that isn't in the list renders the "not built yet"
  // placeholder instead of the section.
  const [active, setActive] = useState("Store profile");
  const [s, setS] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);

  // Self-ordering is per-branch.  On a branch that doesn't use it (e.g.
  // biohouse) the section is hidden entirely, so the feature is invisible
  // there rather than shown-but-useless.
  const [selfOrderOn, setSelfOrderOn] = useState(false);
  useEffect(() => {
    let cancelled = false;
    if (!branchId) { setSelfOrderOn(false); return; }
    apiFetch(`${API}/branches`)
      .then((r) => (r.ok ? r.json() : []))
      .then((list) => {
        if (cancelled) return;
        const b = (Array.isArray(list) ? list : []).find(
          (x: any) => String(x?.id) === String(branchId),
        );
        setSelfOrderOn(!!b?.self_order_enabled);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [branchId]);
  const visibleSections = sections.filter(
    (sec) => sec.key !== "Self-Order QR" || selfOrderOn,
  );
  // Narrow phones can't show the list + the detail side by side (leftNav is
  // 280px wide, leaving ~95px for the detail pane).  Use a master-detail
  // pattern: list first, then drill into a section.
  const [drilled, setDrilled] = useState(false);
  const showDetail = isWide || drilled;
  const showList = isWide || !drilled;

  useEffect(() => {
    apiFetch(`${API}/settings`).then((r) => safeJson<Settings | null>(r, null)).then(setS);
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
        showAlert(tr("admin.save_failed"), `Settings did not save (HTTP ${res.status}). ${detail.slice(0, 200)}`);
        return;
      }
      // Refresh from the server response so saved (and masked) values are
      // reflected immediately — confirms the write actually landed.
      const saved = await res.json().catch(() => null);
      if (saved) setS(saved);
    } catch (e: any) {
      showAlert(tr("admin.save_failed"), e?.message || tr("admin.could_not_reach_the_server"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Body
      style={!isWide && { paddingHorizontal: 14 }}
      testID="settings-section"
    >
      <View style={[styles.twoCol, !isWide && styles.stackedCol]}>
      {showList && (
        <Panel style={[styles.setNav, !isWide && { width: "100%", flex: 1 }]}>
          <ScrollView showsVerticalScrollIndicator={false}>
            {visibleSections.map((sec, i) => {
              const on = isWide && active === sec.key;
              return (
                <TouchableOpacity
                  key={sec.key}
                  style={[
                    styles.setRow,
                    on && { backgroundColor: C.brandTintSoft },
                    i === visibleSections.length - 1 && { borderBottomWidth: 0 },
                  ]}
                  onPress={() => { setActive(sec.key); setDrilled(true); }}
                  activeOpacity={0.8}
                  testID={`settings-${sec.key}`}
                >
                  {/* Icon tile rather than a bare glyph — at a glance the tint
                      says which part of the shop a setting belongs to. */}
                  <View style={[styles.setIco, { backgroundColor: on ? C.brandTint : C.sunk }]}>
                    <Ionicons
                      name={sec.icon}
                      size={19}
                      color={on ? C.brand : sec.color}
                    />
                  </View>
                  <Text
                    style={[styles.setLabel, on && { color: C.brand, fontWeight: "700" }]}
                    numberOfLines={1}
                  >
                    {tr(sec.labelKey)}
                  </Text>
                  <Ionicons
                    name="chevron-forward"
                    size={15}
                    color={on ? C.brand : C.ink3}
                  />
                </TouchableOpacity>
              );
            })}
          </ScrollView>
        </Panel>
      )}
      {showDetail && (
      <Panel style={{ flex: 1, minWidth: 0 }}>
        {!isWide && (
          <TouchableOpacity
            style={styles.backRow}
            onPress={() => setDrilled(false)}
            testID="settings-back"
          >
            <Ionicons name="chevron-back" size={18} color={C.brand} />
            <Text style={styles.backText}>{tr("common.settings")}</Text>
          </TouchableOpacity>
        )}
        {active === "Self-Order QR" ? (
          <SelfOrderQrView branchId={branchId} branchName={branchName} />
        ) : active === "Store profile" && s ? (
          <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
            <Text style={styles.h2}>{tr("common.shop")}</Text>
            <Field label={tr("admin.shop_name")}>
              <TextInput style={styles.formInput} value={s.shop_name} onChangeText={(v) => update({ shop_name: v })} testID="set-shop-name" />
            </Field>
            <Field label={tr("admin.business_type")}>
              <View style={{ flexDirection: "row", gap: 8 }}>
                {["General", "Restaurant", "Hostel"].map((bt) => (
                  <TouchableOpacity
                    key={bt}
                    style={[styles.bizBtn, s.business_type === bt && styles.bizBtnActive]}
                    onPress={() => update({ business_type: bt })}
                    testID={`biz-${bt}`}
                  >
                    <Text style={[styles.bizBtnText, s.business_type === bt && { color: C.surface }]}>{bt}</Text>
                  </TouchableOpacity>
                ))}
              </View>
            </Field>
            <View style={{ flexDirection: "row", gap: 12 }}>
              <Field label={tr("admin.tax_id")} flex>
                <TextInput style={styles.formInput} value={s.tax_id || ""} onChangeText={(v) => update({ tax_id: v })} placeholder="—" />
              </Field>
              <Field label={tr("admin.branch")} flex>
                <TextInput style={styles.formInput} value={s.branch} onChangeText={(v) => update({ branch: v })} />
              </Field>
              <Field label={tr("admin.pos")} flex>
                <TextInput style={styles.formInput} value={s.pos_number} onChangeText={(v) => update({ pos_number: v })} />
              </Field>
            </View>
            <View style={{ flexDirection: "row", gap: 12 }}>
              <Field label={tr("admin.open_time")} flex>
                <TextInput style={styles.formInput} value={s.open_time} onChangeText={(v) => update({ open_time: v })} />
              </Field>
              <Field label={tr("admin.close_time")} flex>
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
                    <Text style={[styles.bizBtnText, s.tax_mode === m && { color: C.surface }]}>{m}</Text>
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
            <Field label={tr("admin.service_charge")}>
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
                <Text>{s.service_charge_enabled ? tr("admin.enabled") : tr("admin.disabled")}</Text>
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
              <Text style={styles.primaryBtnText}>{saving ? tr("admin.saving") : tr("admin.save_settings")}</Text>
            </TouchableOpacity>
          </ScrollView>
        ) : active === "Printers" && s ? (
          <PrintersSection s={s} update={update} save={save} saving={saving} />
        ) : active === "Cash drawer" ? (
          <DrawerCategoriesSection />
        ) : active === "Stock-out reasons" ? (
          <StockOutReasonsSection />
        ) : active === "Language" ? (
          <LanguageSection />
        ) : (
          <Empty
            icon="construct-outline"
            // `active` is the English dispatch key; show the section's own
            // translated label so the placeholder reads in the UI language.
            title={tr(sections.find((sec) => sec.key === active)?.labelKey ?? active)}
            note={tr("admin.not_built_yet_this_section_is")}
          />
        )}
      </Panel>
      )}
      </View>
    </Body>
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

// ── Settings → Language: pick the UI language for this tablet ──
//
// Device-scoped, not per-staff: a counter tablet is shared, and re-reading the
// preference on every PIN login would flip the language under whoever is
// mid-sale. Whoever sets it here sets it for the till.
//
// Receipts and the shift-close slip deliberately ignore this — they are Thai
// statutory documents (ใบกำกับภาษีอย่างย่อ) whose bilingual layout is fixed by
// what the Revenue Department requires, not by who is logged in.
function LanguageSection() {
  const { lang, setLang } = useT();
  return (
    <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
      <Text style={styles.h2}>{tr("language.title")}</Text>
      <View style={{ gap: 10 }}>
        {LANGUAGES.map((l) => {
          const on = lang === l.code;
          return (
            <TouchableOpacity
              key={l.code}
              style={[styles.langRow, on && styles.langRowOn]}
              onPress={() => setLang(l.code)}
              testID={`lang-${l.code}`}
            >
              <Ionicons
                name={on ? "checkmark-circle" : "ellipse-outline"}
                size={22}
                color={on ? C.brand : C.lineStrong}
              />
              <View style={{ flex: 1 }}>
                <Text style={styles.langNative}>{l.native}</Text>
                <Text style={styles.langSub}>
                  {tr(l.code === "th" ? "language.thai" : "language.english")}
                </Text>
              </View>
              {on && <Tag tone="info">{tr("language.current")}</Tag>}
            </TouchableOpacity>
          );
        })}
      </View>
      <Text style={styles.langNote}>{tr("language.note")}</Text>
    </ScrollView>
  );
}

// ── Settings → Drawer: manage Cash In / Cash Out reason codes ──
function DrawerCategoriesSection() {
  useT(); // re-render this screen when the language changes
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
        <Text style={styles.h2}>{tr("admin.drawer_categories")}</Text>
        <View style={{ flexDirection: "row", gap: 8 }}>
          {([["paid_in", "Cash In"], ["paid_out", "Cash Out"]] as const).map(([k, label]) => (
            <TouchableOpacity
              key={k}
              style={[styles.bizBtn, type === k && styles.bizBtnActive]}
              onPress={() => setType(k)}
              testID={`drawer-cat-tab-${k}`}
            >
              <Text style={[styles.bizBtnText, type === k && { color: C.surface }]}>{label}</Text>
            </TouchableOpacity>
          ))}
        </View>

        {loading ? (
          <ActivityIndicator color={C.brand} style={{ marginTop: 20 }} />
        ) : (
          rows.map((c) => (
            <View key={c.id} style={styles.moveRow} testID={`drawer-cat-row-${c.id}`}>
              <Text style={styles.moveRowValue}>{c.name}</Text>
              <View style={{ flexDirection: "row", gap: 16 }}>
                <TouchableOpacity onPress={() => openEdit(c)} testID={`drawer-cat-edit-${c.id}`}>
                  <Ionicons name="create-outline" size={20} color="#3B82F6" />
                </TouchableOpacity>
                <TouchableOpacity onPress={() => remove(c)} testID={`drawer-cat-del-${c.id}`}>
                  <Ionicons name="trash-outline" size={20} color={C.danger} />
                </TouchableOpacity>
              </View>
            </View>
          ))
        )}

        <TouchableOpacity style={[styles.primaryBtn, { marginTop: 6 }]} onPress={openNew} testID="drawer-cat-add">
          <Ionicons name="add" size={18} color={C.surface} style={{ marginRight: 4 }} />
          <Text style={styles.primaryBtnText}>{tr("admin.add_category")}</Text>
        </TouchableOpacity>
      </ScrollView>

      <Modal visible={!!editing} transparent animationType="fade" onRequestClose={() => setEditing(null)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setEditing(null)}><Ionicons name="close" size={24} color={C.ink2} /></TouchableOpacity>
              <Text style={styles.modalTitle}>{editing?.id ? tr("admin.edit_category") : tr("admin.new_category")}</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>{tr("admin.category_name")}</Text>
              <TextInput style={styles.formInput} value={name} onChangeText={setName} autoFocus testID="drawer-cat-name" />
              <TouchableOpacity style={styles.primaryBtn} onPress={submit} testID="drawer-cat-save">
                <Text style={styles.primaryBtnText}>{tr("common.save")}</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

// ── Settings → Stock-out reasons: manage the reason codes staff pick from ──
// Same shape as DrawerCategoriesSection: the two solve the same problem (a
// free-text box producing six spellings of one reason) and should not drift
// into two different interaction models.
function StockOutReasonsSection() {
  useT(); // re-render this screen when the language changes
  const [rows, setRows] = useState<StockOutReason[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<StockOutReason | null>(null);
  const [name, setName] = useState("");
  const [nameTh, setNameTh] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const r = await apiFetch(`${API}/stock-out-reasons`);
      const data = await r.json();
      setRows(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const openNew = () => { setEditing({ id: "", name: "" }); setName(""); setNameTh(""); };
  const openEdit = (r: StockOutReason) => {
    setEditing(r); setName(r.name); setNameTh(r.name_th || "");
  };

  const submit = async () => {
    const trimmed = name.trim();
    if (!editing || !trimmed) return;
    const payload = { name: trimmed, name_th: nameTh.trim() || trimmed };
    if (editing.id) {
      await apiFetch(`${API}/stock-out-reasons/${editing.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await apiFetch(`${API}/stock-out-reasons`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, sort_order: rows.length }),
      });
    }
    setEditing(null); setName(""); setNameTh(""); load();
  };

  // Deactivate rather than delete is the safer default, but keep delete for a
  // reason added by mistake.  Either way the documents that already recorded
  // it keep their text — the document stores the name, not a foreign key.
  const toggleActive = async (r: StockOutReason) => {
    await apiFetch(`${API}/stock-out-reasons/${r.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: !(r.active ?? true) }),
    });
    load();
  };

  const remove = async (r: StockOutReason) => {
    await apiFetch(`${API}/stock-out-reasons/${r.id}`, { method: "DELETE" });
    load();
  };

  return (
    <View style={{ flex: 1 }} testID="stock-out-reason-settings">
      <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
        <Text style={styles.h2}>{tr("admin.stock_out_reasons")}</Text>
        <Text style={{ color: C.ink3, fontSize: 13 }}>
          {tr("admin.staff_pick_one_of_these_when")}
        </Text>

        {loading ? (
          <ActivityIndicator color={C.brand} style={{ marginTop: 20 }} />
        ) : (
          rows.map((r) => {
            const on = r.active ?? true;
            return (
              <View key={r.id} style={styles.moveRow} testID={`stock-reason-row-${r.id}`}>
                <View style={{ flex: 1, minWidth: 0 }}>
                  <Text style={[styles.moveRowValue, !on && { color: C.ink3 }]}>
                    {r.name}{on ? "" : tr("admin.hidden")}
                  </Text>
                  {!!r.name_th && r.name_th !== r.name && (
                    <Text style={styles.catRowSub}>{r.name_th}</Text>
                  )}
                </View>
                <View style={{ flexDirection: "row", gap: 16 }}>
                  <TouchableOpacity onPress={() => toggleActive(r)} testID={`stock-reason-toggle-${r.id}`}>
                    <Ionicons
                      name={on ? "eye-outline" : "eye-off-outline"}
                      size={20}
                      color={on ? C.ok : C.ink3}
                    />
                  </TouchableOpacity>
                  <TouchableOpacity onPress={() => openEdit(r)} testID={`stock-reason-edit-${r.id}`}>
                    <Ionicons name="create-outline" size={20} color="#3B82F6" />
                  </TouchableOpacity>
                  <TouchableOpacity onPress={() => remove(r)} testID={`stock-reason-del-${r.id}`}>
                    <Ionicons name="trash-outline" size={20} color={C.danger} />
                  </TouchableOpacity>
                </View>
              </View>
            );
          })
        )}

        <TouchableOpacity style={[styles.primaryBtn, { marginTop: 6 }]} onPress={openNew} testID="stock-reason-add">
          <Ionicons name="add" size={18} color={C.surface} style={{ marginRight: 4 }} />
          <Text style={styles.primaryBtnText}>{tr("admin.add_reason")}</Text>
        </TouchableOpacity>
      </ScrollView>

      <Modal visible={!!editing} transparent animationType="fade" onRequestClose={() => setEditing(null)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setEditing(null)}>
                <Ionicons name="close" size={24} color={C.ink2} />
              </TouchableOpacity>
              <Text style={styles.modalTitle}>{editing?.id ? tr("admin.edit_reason") : tr("admin.new_reason")}</Text>
              <View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>{tr("admin.reason_english")}</Text>
              <TextInput
                style={styles.formInput} value={name} onChangeText={setName}
                autoFocus testID="stock-reason-name"
              />
              <Text style={styles.formLabel}>{tr("admin.thai_name_optional")}</Text>
              <TextInput
                style={styles.formInput} value={nameTh} onChangeText={setNameTh}
                testID="stock-reason-name-th"
              />
              <TouchableOpacity style={styles.primaryBtn} onPress={submit} testID="stock-reason-save">
                <Text style={styles.primaryBtnText}>{tr("common.save")}</Text>
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
  useT(); // re-render this screen when the language changes
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
          tr("admin.no_printers_found_hint"),
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
      setLocalResult(tr("admin.invalid_ipv4"));
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

  // Per-device receipt right margin.  Increase it if this printer clips the
  // right edge (pushes content further left); undefined = 170 (default, e.g.
  // biohouse, unchanged).
  const setRightPad = useCallback(async (v: number | undefined) => {
    if (!localCfg) return;
    const next = { ...localCfg, receiptRightPad: v };
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
      setLocalResult(r.ok ? tr("admin.sent_check_the_printer") : `Test failed: ${(r as any).error}`);
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
      if (r.ok) setTestResult(tr("admin.sent_check_the_printer"));
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
      ? C.ink3
      : status.connected
      ? C.ok
      : C.danger;
  const statusLabel = !status
    ? tr("admin.checking")
    : status.status === "disabled"
    ? "Disabled"
    : status.connected
    ? tr("admin.connected")
    : tr("admin.offline");

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
        <Text style={styles.h2}>{tr("admin.local_printer_this_tablet")}</Text>
        <Text style={styles.printerListMeta}>
          {tr("admin.connect_an_epson_tm_t82x_to")}
        </Text>

        <View style={styles.printerCard}>
          <View style={styles.printerHeader}>
            <Ionicons name="phone-portrait-outline" size={22} color={C.ok} />
            <View style={{ flex: 1 }}>
              <Text style={styles.printerName}>
                {localCfg?.identifier
                  ? (localCfg.model || "Epson TM-T82X")
                  : tr("admin.not_configured")}
              </Text>
              <Text style={styles.printerSub} numberOfLines={1}>
                {localCfg?.identifier ? localCfg.identifier : tr("admin.tap_scan_to_find_printer")}
              </Text>
            </View>
            {/* Status pill reflects three things in order of priority:
                1. No config       → grey "Off"
                2. Config disabled → grey "Disabled"
                3. Live ping (online/offline checked every 30s):
                     online      → green tr("admin.online")
                     offline     → red   tr("admin.offline")
                     unknown yet → amber tr("admin.checking") */}
            {(() => {
              const noConfig = !localCfg?.identifier;
              const disabled = !!localCfg && !localCfg.enabled;
              let dotColor = C.ink3;
              let label: string = "Off";
              if (noConfig) {
                dotColor = C.ink3; label = "Off";
              } else if (disabled) {
                dotColor = C.ink3; label = "Disabled";
              } else if (livePrinter.online === true) {
                dotColor = C.ok; label = tr("admin.online");
              } else if (livePrinter.online === false) {
                dotColor = C.danger; label = tr("admin.offline");
              } else {
                dotColor = C.warn; label = tr("admin.checking");
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
            <Ionicons name="search" size={16} color={C.ink} />
            <Text style={styles.secondaryBtnText}>
              {localScanning ? tr("admin.scanning") : tr("admin.scan")}
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
                <Ionicons name="document-text-outline" size={16} color={C.ink} />
                <Text style={styles.secondaryBtnText}>
                  {localTesting ? tr("admin.sending") : tr("admin.test_print")}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={styles.secondaryBtn}
                onPress={toggleLocalEnabled}
                testID="local-printer-toggle"
              >
                <Text style={styles.secondaryBtnText}>
                  {localCfg.enabled ? tr("admin.disable") : tr("admin.enable")}
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
            placeholder={tr("admin.or_enter_ip_e_g_192")}
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
            <Ionicons name="add" size={16} color={C.ink} />
            <Text style={styles.secondaryBtnText}>{tr("admin.add_by_ip")}</Text>
          </TouchableOpacity>
        </View>

        {/* Right margin — fixes right-edge clipping on printers whose printable
            area is narrower than the 576-dot head, by pushing content left.
            Higher = more trimmed from the right.  Print a receipt after each
            change (reprint any bill from Transactions). */}
        {localCfg?.identifier && (
          <View style={{ marginTop: 14 }}>
            <Text style={styles.formLabel}>{tr("admin.receipt_right_margin")}</Text>
            <Text style={{ color: C.ink2Soft, fontSize: 12, marginBottom: 8 }}>
              {tr("admin.if_the_right_side_of_the")}
            </Text>
            <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
              {[
                { label: tr("admin.default"), v: undefined as number | undefined },
                { label: "210", v: 210 },
                { label: "250", v: 250 },
                { label: "290", v: 290 },
                { label: "330", v: 330 },
              ].map((opt) => {
                const isActive = (localCfg.receiptRightPad ?? 170) === (opt.v ?? 170);
                return (
                  <TouchableOpacity
                    key={opt.label}
                    onPress={() => setRightPad(opt.v)}
                    style={[
                      styles.secondaryBtn,
                      isActive && { borderColor: C.ok, backgroundColor: C.brandTintSoft },
                    ]}
                    testID={`rightpad-${opt.label}`}
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
            <Text style={styles.formLabel}>{tr("admin.found")} {localFound.length} {tr("admin.printer_s")}</Text>
            {localFound.map((d) => (
              <TouchableOpacity
                key={d.identifier}
                style={[
                  styles.printerListRow,
                  localCfg?.identifier === d.identifier && {
                    borderColor: C.ok,
                    backgroundColor: C.brandTintSoft,
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
                  <Ionicons name="checkmark-circle" size={18} color={C.ok} />
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
        <Text style={[styles.h2, { marginTop: 8 }]}>{tr("admin.queued_receipts")}</Text>
        {queuedJobs.length === 0 ? (
          <Text style={styles.printerListMeta}>
            {tr("admin.no_receipts_waiting_to_print_new")}
          </Text>
        ) : (
          <View style={{ gap: 8 }}>
            <Text style={styles.printerListMeta}>
              {tr("admin.receipts_waiting", { count: queuedJobs.length, tail: tr("admin.waiting_will_print_automatically_when_the") })}
            </Text>
            {queuedJobs.map((j) => {
              const ageMin = Math.max(0, Math.round((Date.now() - j.createdAt) / 60000));
              return (
                <View key={j.id} style={styles.queuedRow} testID={`queued-job-${j.order.order_number}`}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.queuedOrder}>{j.order.order_number}</Text>
                    <Text style={styles.queuedMeta}>
                      {THB(j.order.total)} · {ageMin < 1 ? tr("admin.just_now") : tr("admin.min_ago", { count: ageMin })} · {tr("admin.attempts", { count: j.attempts })}
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
                    <Ionicons name="close" size={16} color={C.danger} />
                    <Text style={styles.queuedRemoveText}>{tr("admin.remove")}</Text>
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
          <Ionicons name="chevron-back" size={22} color={C.ink} />
        </TouchableOpacity>
        <Text style={styles.h2}>{tr("admin.receipt_printer")}</Text>
      </View>

      {/* ── Enable toggle ── */}
      <Field label={tr("admin.auto_print_receipt_on_every_order")}>
        <View style={{ flexDirection: "row", gap: 8, alignItems: "center" }}>
          <TouchableOpacity
            style={[styles.toggleBox, enabled && styles.toggleBoxOn]}
            onPress={() => update({ printer_enabled: !enabled })}
            testID="printer-enabled-toggle"
          >
            <View style={[styles.toggleKnob, enabled && styles.toggleKnobOn]} />
          </TouchableOpacity>
          <Text>{enabled ? tr("admin.enabled") : tr("admin.disabled")}</Text>
        </View>
      </Field>

      {/* ── Transport ── */}
      <Field label={tr("admin.connection_type")}>
        <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
          {[
            { k: "file", label: "USB" },
            { k: "network", label: tr("admin.network_tcp") },
          ].map((t) => (
            <TouchableOpacity
              key={t.k}
              style={[styles.bizBtn, transport === t.k && styles.bizBtnActive]}
              onPress={() => update({ printer_transport: t.k as any })}
              testID={`printer-transport-${t.k}`}
            >
              <Text style={[styles.bizBtnText, transport === t.k && { color: C.surface }]}>
                {t.label}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      </Field>

      {/* ── USB: detected devices only (no manual path input) ── */}
      {transport === "file" && (
        <Field label={tr("admin.detected_printers")}>
          <View style={{ flexDirection: "row", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {detected.length === 0 ? (
              <Text style={styles.printerListMeta}>
                {detecting ? tr("admin.scanning") : tr("admin.no_usb_printers_detected_plug_one")}
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
                      address === d.path && { color: C.surface },
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
              <Ionicons name="refresh" size={14} color={C.ink2} />
              <Text style={styles.printerListMeta}>{detecting ? "…" : tr("admin.rescan")}</Text>
            </TouchableOpacity>
          </View>
        </Field>
      )}

      {/* ── Network: address still needed (can't auto-detect) ── */}
      {transport === "network" && (
        <Field label={tr("admin.host_port_e_g_192_168")}>
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
          <Text style={styles.primaryBtnText}>{saving ? tr("admin.saving") : tr("admin.save_printer_settings")}</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[styles.secondaryBtn, (testing || transport === "disabled") && { opacity: 0.5 }]}
          onPress={runTest}
          disabled={testing || transport === "disabled"}
          testID="printer-test"
        >
          <Ionicons name="document-text-outline" size={16} color={C.ink} />
          <Text style={styles.secondaryBtnText}>{testing ? tr("admin.sending") : tr("admin.test_print")}</Text>
        </TouchableOpacity>
      </View>
      {testResult ? <Text style={styles.printerError}>{testResult}</Text> : null}
    </ScrollView>
  );
}

// =================== STYLES ===================
const styles = StyleSheet.create({
  // ── Back-office page furniture ─────────────────────────────────────────
  pager: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 20,
    paddingVertical: 12,
    borderTopWidth: 1,
    borderTopColor: C.line2,
  },
  pagerText: { fontSize: 13.5, fontWeight: "600", color: C.ink2Soft },

  // Settings
  setNav: { width: 262, flexGrow: 0, flexShrink: 0 },
  setRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingHorizontal: 16,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  setIco: {
    width: 40,
    height: 40,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
  },
  setLabel: { flex: 1, fontSize: 14.5, fontWeight: "600", color: C.ink2 },

  // Products
  catColAdmin: {
    width: 236,
    flexGrow: 0,
    flexShrink: 0,
    backgroundColor: C.surface,
    borderRadius: R.card,
    borderWidth: 1,
    borderColor: C.line2,
    paddingVertical: 10,
    paddingHorizontal: 8,
  },
  catRowAdmin: {
    height: 48,
    borderRadius: 10,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 12,
  },
  catRowAdminOn: { backgroundColor: C.brandTintSoft },
  catRowAdminText: { flex: 1, fontSize: 14.5, fontWeight: "600", color: C.ink2 },
  catRowAdminCount: { fontSize: 12, color: C.ink3 },
  catDot: { width: 10, height: 10, borderRadius: 5 },
  prodCount: { fontSize: 13.5, color: C.ink3 },
  prodSub: { fontSize: 12.5, color: C.ink3, marginTop: 3 },
  thumb: {
    width: 46,
    height: 46,
    borderRadius: 10,
    backgroundColor: C.sunk,
  },
  thumbEmpty: { alignItems: "center", justifyContent: "center" },
  prodFootNote: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 20,
    paddingVertical: 15,
    borderTopWidth: 1,
    borderTopColor: C.line2,
  },
  prodFootText: { fontSize: 13.5, color: C.ink2Soft },

  // Detail-pane furniture shared by Orders and Customers.
  avBig: {
    width: 76,
    height: 76,
    borderRadius: 38,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 14,
  },
  avBigText: { color: C.surface, fontSize: 27, fontWeight: "700" },
  kpis: {
    flexDirection: "row",
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  kpi: {
    flex: 1,
    paddingVertical: 18,
    paddingHorizontal: 8,
    alignItems: "center",
    borderRightWidth: 1,
    borderRightColor: C.line2,
  },
  kpiVal: { fontSize: 20, fontWeight: "800", color: C.ink, letterSpacing: -0.6 },
  kpiLbl: { marginTop: 5, fontSize: 12.5, color: C.ink2Soft },
  dsec: {
    padding: 18,
    paddingHorizontal: 22,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  dsecEmpty: { fontSize: 14, color: C.ink3 },

  filterRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  twoCol: { flex: 1, minHeight: 0, flexDirection: "row", gap: 16 },
  stackedCol: { flexDirection: "column" },
  detailCol: { width: 400, flexGrow: 0, flexShrink: 0 },
  takings: { fontSize: 15, fontWeight: "700", color: C.ink },
  takingsNote: { fontSize: 12, color: C.ink3 },

  billsHead: {
    paddingHorizontal: 14, paddingTop: 16, paddingBottom: 10,
    backgroundColor: C.surface,
  },
  h2: { fontSize: 18, fontWeight: "700", color: C.ink, marginBottom: 4 },
  helperText: { color: C.ink3, fontSize: 13, marginBottom: 14 },
  rangeCard: {
    width: "92%", maxWidth: 420, backgroundColor: C.surface,
    borderRadius: 16, overflow: "hidden",
  },
  root: { flex: 1, backgroundColor: C.bg },
  sectionHeader: {
    fontSize: 15, fontWeight: "700", color: C.ink,
    padding: 14, borderBottomWidth: 1, borderBottomColor: C.line,
    backgroundColor: C.surface,
  },
  txDetail: { flex: 1 },
  txList: { width: 320, backgroundColor: C.surface, borderRightWidth: 1, borderRightColor: C.line },
  // ── Reports ────────────────────────────────────────────────────────────
  bar: { backgroundColor: "#DDE4EE", borderTopLeftRadius: 5, borderTopRightRadius: 5 },
  baxis: { flexDirection: "row", gap: 9, paddingHorizontal: 22, paddingVertical: 10 },
  baxisText: { flex: 1, textAlign: "center", fontSize: 10, color: C.ink3 },
  chartNote: {
    borderTopWidth: 1,
    borderTopColor: C.line2,
    paddingHorizontal: 22,
    paddingVertical: 16,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  rankRow: { flexDirection: "row", alignItems: "center", gap: 12, flex: 1 },
  statRow: { flexDirection: "row", gap: 16 },
  reportCols: { flex: 1, minHeight: 0, flexDirection: "row", gap: 16 },
  bars: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: 9,
    height: 210,
    paddingHorizontal: 22,
    paddingTop: 20,
  },
  chartNoteKey: {
    width: 10,
    height: 10,
    borderRadius: 3,
    backgroundColor: C.brand,
  },
  chartNoteText: { flex: 1, fontSize: 14.5, color: C.ink2, lineHeight: 21 },
  avatarBox: { alignItems: "center", marginBottom: 20 },
  sideBadge: {
    width: 56, height: 56, borderRadius: 16,
    alignItems: "center", justifyContent: "center",
    overflow: "hidden",
  },
  sideBadgeLogo: { width: "100%", height: "100%" },
  avatarText: { fontSize: 14, color: C.surface, marginTop: 8, fontWeight: "700" },
  sideBranchChip: {
    flexDirection: "row", alignItems: "center", gap: 4,
    marginTop: 6, paddingHorizontal: 8, paddingVertical: 4,
    backgroundColor: "rgba(255,255,255,0.18)", borderRadius: 999, maxWidth: 180,
  },
  sideBranchChipText: { fontSize: 11, color: C.surface, fontWeight: "600" },
  sideItem: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, borderRadius: 10, marginBottom: 4,
  },
  sideItemActive: { backgroundColor: C.surface },
  sideLabel: { fontSize: 14, color: "rgba(255,255,255,0.85)", fontWeight: "500" },
  sideLabelActive: { color: C.brand, fontWeight: "700" },
  logoutSide: {
    flexDirection: "row", gap: 8, padding: 12, alignItems: "center",
    borderTopWidth: 1, borderTopColor: "rgba(255,255,255,0.18)",
  },
  logoutSideText: { color: C.surface, fontSize: 13, fontWeight: "600" },
  versionText: { fontSize: 10, color: "rgba(255,255,255,0.5)", textAlign: "center", marginTop: 4 },
  sideFooter: {
    flexDirection: "row", alignItems: "center", gap: 4,
    paddingHorizontal: 12, paddingTop: 6, paddingBottom: 2,
    borderTopWidth: 1, borderTopColor: C.bg,
  },
  sideFooterDate: { fontSize: 10, color: "rgba(255,255,255,0.6)" },

  mobileTop: {
    height: 56, backgroundColor: C.surface, flexDirection: "row",
    alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, borderBottomWidth: 1, borderBottomColor: C.line,
  },
  mobileTitle: { fontSize: 16, fontWeight: "700", color: C.ink },
  mobileSidebarOverlay: { flex: 1, flexDirection: "row", backgroundColor: C.scrim },

  content: { flex: 1 },

  // Text

  // Two-column layout — twoCol/stackedCol now live at the top of this sheet.
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
    borderBottomColor: C.line,
  },
  // Compact horizontal category strip used on narrow screens for Products /
  // Inventory.  Replaces the ~600px-tall vertical rail so the FlatList of
  // products below has enough vertical space to render.
  narrowCatBar: {
    width: "100%",
    backgroundColor: C.surface,
    borderBottomWidth: 1,
    borderBottomColor: C.line,
  },
  catChip: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
  },
  catChipText: { fontSize: 12, color: C.ink, fontWeight: "600" },
  backRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    padding: 14,
    backgroundColor: C.surface,
    borderBottomWidth: 1,
    borderBottomColor: C.line,
  },
  backText: { color: C.brand, fontSize: 14, fontWeight: "600" },
  leftNav: {
    width: 280, backgroundColor: C.surface,
    borderRightWidth: 1, borderRightColor: C.line,
  },
  leftNavRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 14, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  leftNavRowActive: { backgroundColor: C.bg },
  leftNavText: { flex: 1, fontSize: 13, color: C.ink2 },

  // Reports
  periodBtn: { paddingHorizontal: 16, paddingVertical: 8, borderRadius: 8 },
  periodBtnActive: { backgroundColor: C.brand },
  periodText: { fontSize: 13, color: C.ink2, fontWeight: "600" },
  periodTextActive: { color: C.surface },
  chartTitle: { fontSize: 13, fontWeight: "700", color: C.ink, marginBottom: 10 },
  billsTitle: { fontSize: 24, fontWeight: "800", color: C.ink, letterSpacing: -0.5 },
  billsSummary: { fontSize: 12, color: C.ink3, marginTop: 3, fontWeight: "500" },
  billsSummaryAmt: { color: C.ink, fontWeight: "700" },
  billsDay: {
    flexDirection: "row", alignItems: "center", gap: 10,
    paddingHorizontal: 14, paddingTop: 16, paddingBottom: 6,
  },
  billsDayLabel: {
    fontSize: 11, fontWeight: "800", color: C.ink2,
    letterSpacing: 0.6, textTransform: "uppercase",
  },
  billsDayRule: { flex: 1, height: 1, backgroundColor: C.line },
  billsDayTotal: { fontSize: 11, fontWeight: "700", color: C.ink3 },
  billRow: { flexDirection: "row", alignItems: "center", paddingRight: 14, paddingLeft: 14 },
  billRowActive: { backgroundColor: C.brandTintSoft },
  billTime: { width: 44, fontSize: 11, color: C.ink3, fontWeight: "600" },
  billRail: { width: 22, alignSelf: "stretch", alignItems: "center", justifyContent: "center" },
  billRailLine: {
    position: "absolute", top: 0, bottom: 0, width: 1, backgroundColor: C.line,
  },
  billDot: {
    width: 9, height: 9, borderRadius: 5,
    backgroundColor: C.surface, borderWidth: 2, borderColor: C.lineStrong,
  },
  billDotActive: { borderColor: C.brand, backgroundColor: C.brand },
  billDotVoided: { borderColor: C.danger, backgroundColor: C.dangerTint },
  billNum: { fontSize: 13, color: C.ink, fontWeight: "700", paddingVertical: 10 },
  billMeta: { fontSize: 11, color: C.ink3, marginTop: -8, marginBottom: 10 },
  billAmount: { fontSize: 14, color: C.ink, fontWeight: "700" },

  txDateChips: {
    flexDirection: "row", alignItems: "center", gap: 18,
    paddingHorizontal: 14, paddingBottom: 2,
    borderBottomWidth: 1, borderBottomColor: C.line,
  },
  txDateChip: { paddingBottom: 8, borderBottomWidth: 2, borderBottomColor: "transparent" },
  txDateChipActive: { borderBottomColor: C.brand },
  txDateChipText: { fontSize: 12, color: C.ink3, fontWeight: "600" },
  txDateChipTextActive: { color: C.brand, fontWeight: "800" },
  txEmpty: { padding: 24, alignItems: "center" },
  txVoided: { color: C.dangerDark, fontWeight: "700" },
  divider: { height: 1, backgroundColor: C.bg },
  voidedBy: { fontSize: 13, color: C.dangerDark, fontWeight: "700", marginTop: 4 },

  // ── Transaction detail (reference "Sale Transactions" layout) ──
  txDetailWrap: { flex: 1, backgroundColor: C.surface },
  txDetailScroll: { padding: 24, paddingBottom: 32 },
  tdHeadRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: 12,
    // Wrap rather than overlap: on a narrow detail pane the buttons drop to
    // their own line instead of sitting on top of the order number.
    flexWrap: "wrap",
  },
  tdEyebrowRow: { flexDirection: "row", alignItems: "center", gap: 8, flexShrink: 1, minWidth: 0 },
  tdOrderNo: { fontSize: 13, fontWeight: "700", color: C.ink3, letterSpacing: 0.3, flexShrink: 1 },
  tdStatus: {
    paddingHorizontal: 8, paddingVertical: 2, borderRadius: 999,
    backgroundColor: C.okTint,
    flexShrink: 0,
  },
  tdStatusVoid: { backgroundColor: C.dangerTint },
  tdStatusText: { fontSize: 10, fontWeight: "800", color: C.okDark, letterSpacing: 0.4 },
  tdStatusTextVoid: { color: C.danger },
  tdGrand: { fontSize: 34, fontWeight: "800", color: C.ink, letterSpacing: -1, marginTop: 2 },
  tdActions: { flexDirection: "row", alignItems: "center", gap: 8, paddingTop: 4, flexShrink: 0 },
  tdMetaGrid: { flexDirection: "row", flexWrap: "wrap", gap: 24, marginTop: 18 },
  tdFact: { minWidth: 110 },
  tdFactLabel: {
    fontSize: 10, fontWeight: "800", color: C.ink3,
    letterSpacing: 0.6, textTransform: "uppercase",
  },
  tdFactValue: { fontSize: 13, color: C.ink, fontWeight: "600", marginTop: 3 },
  tdFactValueDanger: { color: C.danger },
  tdHeading: {
    fontSize: 11, fontWeight: "800", color: C.ink2,
    letterSpacing: 0.6, textTransform: "uppercase",
    marginTop: 26, marginBottom: 8,
    borderBottomWidth: 1, borderBottomColor: C.line, paddingBottom: 8,
  },
  tdSectionRow: { flexDirection: "row", alignItems: "center", marginVertical: 16 },
  tdSectionLine: { flex: 1, height: 1, backgroundColor: C.line },
  tdSectionText: { fontSize: 13, color: C.ink2Soft, fontWeight: "600", marginHorizontal: 12 },
  tdItemRow: { flexDirection: "row", alignItems: "center", marginBottom: 16 },
  tdItemImg: { width: 44, height: 44, borderRadius: 8, backgroundColor: C.bg },
  tdItemImgEmpty: { alignItems: "center", justifyContent: "center" },
  tdItemMid: { flex: 1, marginLeft: 12 },
  tdItemName: { fontSize: 15, color: C.ink, fontWeight: "500" },
  tdItemSub: { fontSize: 12, color: C.ink3, marginTop: 3 },
  tdItemTotal: { fontSize: 15, color: C.ink, fontWeight: "600", marginLeft: 12 },
  tdTotalsBlock: { marginTop: 4 },
  tdLineRow: { flexDirection: "row", justifyContent: "flex-end", alignItems: "center", marginBottom: 8 },
  tdLineLabel: { fontSize: 14, color: C.ink2Soft, marginRight: 24 },
  tdLineValue: { fontSize: 14, color: C.ink, fontWeight: "500", minWidth: 80, textAlign: "right" },
  tdLineBold: { fontWeight: "700", color: C.ink },
  tdReprintBtn: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 14, height: 36, borderRadius: 10,
    backgroundColor: C.brand,
  },
  tdReprintText: { fontSize: 13, fontWeight: "700", color: C.surface },
  tdCancelBtn: {
    paddingHorizontal: 14, height: 36, borderRadius: 10,
    alignItems: "center", justifyContent: "center",
    borderWidth: 1, borderColor: C.lineStrong,
  },
  tdCancelBtnDisabled: { opacity: 0.4 },
  tdCancelText: { fontSize: 13, fontWeight: "700", color: C.danger },
  tdTaxIssued: { fontSize: 13, color: C.okDark, fontWeight: "600", marginTop: 4 },

  // ── Reprint document menu (reference POS's พิมพ์ซ้ำ popup) ──
  rpMenu: {
    width: 320, maxWidth: "90%", backgroundColor: C.surface,
    borderRadius: 16, padding: 16, gap: 10,
  },
  rpTitle: { fontSize: 15, fontWeight: "700", color: C.ink, textAlign: "center", marginBottom: 2 },
  rpItem: {
    minHeight: 48, borderRadius: 10, backgroundColor: C.bg,
    alignItems: "center", justifyContent: "center", paddingVertical: 8, paddingHorizontal: 12,
  },
  rpItemDisabled: { backgroundColor: C.bgSoft },
  rpItemText: { fontSize: 14, fontWeight: "600", color: C.ink, textAlign: "center" },
  rpItemTextDisabled: { color: C.lineStrong },
  rpSoon: { fontSize: 11, color: C.lineStrong, marginTop: 2 },

  // ── Full tax invoice flow ──
  tiBackdrop: {
    flex: 1, backgroundColor: C.scrim,
    alignItems: "center", justifyContent: "center", padding: 20,
  },
  tiCard: {
    width: 520, maxWidth: "100%", maxHeight: "90%",
    backgroundColor: C.surface, borderRadius: 16, overflow: "hidden",
  },
  tiHeader: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: C.line,
  },
  tiBackRow: { flexDirection: "row", alignItems: "center" },
  tiHeaderAction: { fontSize: 15, color: C.brand, fontWeight: "600" },
  tiTitle: { fontSize: 16, fontWeight: "700", color: C.ink, flexShrink: 1, textAlign: "center" },
  tiSearchBox: {
    flexDirection: "row", alignItems: "center", gap: 8,
    margin: 16, paddingHorizontal: 12, height: 40,
    backgroundColor: C.bg, borderRadius: 10,
  },
  tiSearchInput: { flex: 1, fontSize: 14, color: C.ink },
  tiEmpty: { padding: 40, alignItems: "center" },
  tiCustRow: { flexDirection: "row", alignItems: "center", gap: 12, paddingHorizontal: 16, paddingVertical: 12 },
  tiAvatar: { width: 36, height: 36, borderRadius: 18, alignItems: "center", justifyContent: "center" },
  tiAvatarText: { color: C.surface, fontWeight: "700", fontSize: 15 },
  tiCustName: { fontSize: 15, fontWeight: "600", color: C.ink },
  tiCustSub: { fontSize: 12, color: C.ink3, marginTop: 2 },
  tiCustVisit: { fontSize: 11, color: C.ink3, marginLeft: 8, maxWidth: 120, textAlign: "right" },
  tiForm: { padding: 16, gap: 12 },
  tiLabel: { fontSize: 13, color: C.ink2Soft, fontWeight: "600", marginBottom: 6 },
  tiRequired: { color: C.dangerDark },
  tiInputError: { borderColor: C.dangerDark },
  tiError: { fontSize: 12, color: C.dangerDark, marginTop: 4 },
  // Registered addresses run long, so the field is tall enough to read one
  // without scrolling inside the input.
  tiTextArea: { height: 96, paddingTop: 10, textAlignVertical: "top" },
  tiRadioRow: { flexDirection: "row", flexWrap: "wrap", gap: 16, marginBottom: 2 },
  tiRadio: { flexDirection: "row", alignItems: "center", gap: 6 },
  tiRadioText: { fontSize: 14, color: C.ink },
  langRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingVertical: 14, paddingHorizontal: 16,
    borderWidth: 1.5, borderColor: C.line, borderRadius: R.card,
    backgroundColor: C.surface,
  },
  langRowOn: { borderColor: C.brand, backgroundColor: C.brandTintSoft },
  // The endonym leads — someone looking for Thai scans for "ไทย", not "Thai".
  langNative: { fontSize: 16, fontWeight: "700", color: C.ink },
  langSub: { fontSize: 12, color: C.ink3, marginTop: 2 },
  langNote: { fontSize: 12, color: C.ink3, lineHeight: 18 },
  tiPrimaryBtn: {
    height: 48, borderRadius: 10, backgroundColor: C.brand,
    alignItems: "center", justifyContent: "center", marginTop: 8,
  },
  tiPrimaryText: { fontSize: 16, fontWeight: "700", color: C.surface },
  tiBusy: {
    position: "absolute", top: 0, right: 0, bottom: 0, left: 0,
    backgroundColor: "rgba(255,255,255,0.6)",
    alignItems: "center", justifyContent: "center",
  },
  tiBusyCard: {
    backgroundColor: C.surface, borderRadius: 12, paddingVertical: 20, paddingHorizontal: 28,
    alignItems: "center", gap: 10,
    // Matches the reference POS's floating spinner card.
    shadowColor: C.ink, shadowOpacity: 0.15, shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 }, elevation: 4,
  },
  tiBusyText: { fontSize: 13, color: C.ink2Soft },
  // Generic text input used by the "Add by IP" field in Local Printer.
  // Standard 40px height + rounded corners + slate border to match the
  // rest of the admin form fields.
  input: {
    height: 40,
    paddingHorizontal: 12,
    fontSize: 14,
    color: C.ink,
    backgroundColor: C.surface,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 10,
  },
  emptyBox: { flex: 1, alignItems: "center", justifyContent: "center", padding: 40, gap: 6 },
  emptyText: { color: C.ink3, fontSize: 14 },

  // Inventory
  invRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, backgroundColor: C.surface, borderRadius: 10,
    marginBottom: 8, borderWidth: 1, borderColor: C.bg,
  },
  invImg: { width: 48, height: 48, borderRadius: 8, backgroundColor: C.bg },
  invName: { fontSize: 13, fontWeight: "600", color: C.ink },
  invPrice: { fontSize: 12, color: C.brand, fontWeight: "700", marginTop: 2 },
  stockBox: { alignItems: "flex-end" },
  stockNum: { fontSize: 18, fontWeight: "700", color: C.brand },
  stockStatus: { fontSize: 10, color: C.ink3 },
  nonStockText: { fontSize: 11, color: C.ink3, fontWeight: "600" },
  invTabs: {
    flexDirection: "row", borderTopWidth: 1, borderTopColor: C.line,
    backgroundColor: C.surface,
  },
  invTab: {
    flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center",
    padding: 14, gap: 6,
  },
  invTabActive: { borderTopWidth: 2, borderTopColor: C.brand },
  invTabText: { fontSize: 12, color: C.ink2, fontWeight: "600" },
  sortRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 8,
    borderBottomWidth: 1, borderBottomColor: C.line,
    backgroundColor: C.surface,
  },
  sortLabel: { fontSize: 12, color: C.ink2, fontWeight: "600", marginRight: 4 },
  sortTab: {
    paddingHorizontal: 14, paddingVertical: 6,
    borderRadius: 6, borderWidth: 1, borderColor: C.line,
    backgroundColor: C.surface,
  },
  sortTabActive: { borderColor: C.brand, backgroundColor: C.surface },
  sortTabText: { fontSize: 12, color: C.ink2, fontWeight: "600" },
  sortTabTextActive: { color: C.brand },

  // Customers
  custHeader: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    padding: 14, borderBottomWidth: 1, borderBottomColor: C.line,
    backgroundColor: C.surface,
  },
  addLink: { color: C.brand, fontSize: 14, fontWeight: "700" },
  searchBoxRow: {
    flexDirection: "row", alignItems: "center", gap: 8,
    margin: 10, padding: 10, backgroundColor: C.bg, borderRadius: 8,
  },
  searchBoxInput: { flex: 1, fontSize: 13, color: C.ink, ...(Platform.OS === "web" ? { outlineStyle: "none" as any } : {}) },
  custAdminRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  custAdminActive: { backgroundColor: C.brandTint },
  custAv: {
    width: 40, height: 40, borderRadius: 20,
    alignItems: "center", justifyContent: "center",
  },
  custAvText: { color: C.surface, fontSize: 14, fontWeight: "700" },
  custAdminName: { fontSize: 13, fontWeight: "600", color: C.ink },
  custAdminPhone: { fontSize: 11, color: C.ink3, marginTop: 2 },
  custProfile: {
    alignItems: "center",
    paddingVertical: 26,
    paddingHorizontal: 22,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  custAvLarge: {
    width: 80, height: 80, borderRadius: 40,
    alignItems: "center", justifyContent: "center",
  },
  custAvLargeText: { color: C.surface, fontSize: 32, fontWeight: "700" },
  custProfileName: {
    fontSize: 22,
    fontWeight: "800",
    color: C.ink,
    letterSpacing: -0.55,
    textAlign: "center",
  },
  custProfileSub: { fontSize: 14, color: C.ink2Soft, marginTop: 6 },
  custPoints: { fontSize: 14, color: C.ink, marginTop: 6 },
  statsRow: {
    flexDirection: "row", gap: 12, padding: 20,
    backgroundColor: C.surface, borderRadius: 12,
  },
  statCell: { flex: 1, alignItems: "center", gap: 4 },
  statSub: { fontSize: 10, color: C.ink3, marginTop: 2 },
  topBox: {
    flex: 1, backgroundColor: C.surface, padding: 14, borderRadius: 12,
    minHeight: 160,
  },
  topRow: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingVertical: 5, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  topRank: { fontSize: 11, color: C.ink3, fontWeight: "600", width: 22 },
  topName: { flex: 1, fontSize: 12, color: C.ink },
  topValue: { fontSize: 12, fontWeight: "700", color: C.ink },

  // Products Management
  allCatsLabel: {
    padding: 14, fontSize: 12, color: C.ink3, fontWeight: "600",
    letterSpacing: 1, textTransform: "uppercase",
  },
  catMgmtRow: {
    flexDirection: "row", alignItems: "center", gap: 10,
    padding: 14, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  catMgmtName: { fontSize: 13, color: C.ink, fontWeight: "500" },
  catMgmtSource: { fontSize: 10, color: C.ink3, marginTop: 2 },
  prodHeader: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "center",
    padding: 14, backgroundColor: C.surface,
    borderBottomWidth: 1, borderBottomColor: C.line,
  },
  addProdBtn: {
    flexDirection: "row", alignItems: "center", gap: 4,
    backgroundColor: C.brand, paddingHorizontal: 14, paddingVertical: 8,
    borderRadius: 8,
  },
  addProdText: { color: C.surface, fontSize: 13, fontWeight: "700" },
  prodMgmtRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    padding: 12, backgroundColor: C.surface,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  prodPriceLabel: { fontSize: 12, color: C.ink3 },
  prodTags: { gap: 2 },
  tag: { fontSize: 10, color: C.ink2 },
  editBtn: { padding: 8 },
  sortGroup: {
    flexDirection: "row",
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 6,
    overflow: "hidden",
  },
  sortBtn: { paddingHorizontal: 14, paddingVertical: 6 },
  sortBtnActive: { borderWidth: 1, borderColor: C.brand, borderRadius: 5 },
  sortText: { fontSize: 12, color: C.ink3, fontWeight: "600" },
  linkText: { fontSize: 14, color: C.brand, fontWeight: "500" },
  linkTextBold: { fontSize: 14, color: C.brand, fontWeight: "700" },

  // Warning pill for ฿0 cost
  warnPill: {
    width: 16,
    height: 16,
    borderRadius: 8,
    backgroundColor: C.danger,
    alignItems: "center",
    justifyContent: "center",
  },
  warnPillText: { color: C.surface, fontSize: 11, fontWeight: "700" },

  // Shift
  shiftHeader: {
    fontSize: 16, fontWeight: "700", color: C.brand,
    padding: 16, backgroundColor: C.surface,
    borderBottomWidth: 1, borderBottomColor: C.line,
    textAlign: "center",
  },
  shiftCard: {
    backgroundColor: C.bgSoft, borderRadius: 10,
    padding: 16, marginBottom: 14,
  },
  shiftRow: {
    flexDirection: "row", justifyContent: "space-between",
    paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.line,
  },
  shiftLabel: { fontSize: 14, color: C.ink2 },
  shiftVal: { fontSize: 14, color: C.ink, fontWeight: "600" },
  inOutRow: { flexDirection: "row", gap: 14, marginBottom: 14 },
  inOutBtn: {
    flex: 1, padding: 16, borderRadius: 10,
    backgroundColor: C.surface, borderWidth: 1,
    borderColor: C.line, alignItems: "center",
  },
  inOutText: { fontSize: 15, fontWeight: "700" },
  closeShiftBtn: {
    backgroundColor: C.brand, padding: 18,
    borderRadius: 10, alignItems: "center", justifyContent: "center",
    flexDirection: "row",
  },
  closeShiftText: { color: C.surface, fontSize: 16, fontWeight: "700", letterSpacing: 1 },
  // Cash In / Cash Out form rows (label left, value/input right)
  moveRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    backgroundColor: C.bgSoft, borderRadius: 10,
    paddingHorizontal: 14, paddingVertical: 12,
    borderWidth: 1, borderColor: C.line,
  },
  moveRowLabel: { fontSize: 14, color: C.ink2, fontWeight: "500" },
  moveRowValue: { fontSize: 14, color: C.ink, fontWeight: "600" },
  moveRowInput: { fontSize: 16, color: C.ink, fontWeight: "600", minWidth: 120, padding: 0 },
  // Category picker rows
  catRow: {
    paddingHorizontal: 20, paddingVertical: 16,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  catRowText: { fontSize: 15, color: C.ink },
  catRowSub: { fontSize: 13, color: C.ink3, marginTop: 2 },
  printingBox: {
    backgroundColor: C.surface, borderRadius: 16,
    paddingVertical: 28, paddingHorizontal: 40,
    alignItems: "center", gap: 14, alignSelf: "center",
  },
  printingText: { fontSize: 15, fontWeight: "600", color: C.ink2 },

  // Shift History — split list + detail
  histLeftWide: { width: 340, borderRightWidth: 1, borderRightColor: C.line },
  histRightWide: { flex: 1 },
  histListPanel: { flex: 1 },
  histRangeRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 12, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  histRangeLabelBtn: {
    flex: 1, alignItems: "center",
    paddingVertical: 6, paddingHorizontal: 8,
    borderRadius: 999,
  },
  histRangeLabel: { fontSize: 13, fontWeight: "600", color: C.brand },
  histDateHeader: {
    fontSize: 13, fontWeight: "600", color: C.ink2,
    paddingHorizontal: 16, paddingVertical: 10,
    backgroundColor: C.bgSoft,
  },
  histListRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingHorizontal: 16, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: C.bg,
    backgroundColor: C.surface,
  },
  histListRowActive: { backgroundColor: C.brandTintSoft },
  histListRound: { fontSize: 14, fontWeight: "700", color: C.ink },
  histListSub: { fontSize: 11, color: C.ink3, marginTop: 2 },
  histCard: {
    backgroundColor: C.surface, borderRadius: 12,
    borderWidth: 1, borderColor: C.bg,
    paddingHorizontal: 16, paddingVertical: 4,
  },
  histReprintBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingVertical: 10, borderRadius: 10,
    borderWidth: 1, borderColor: C.brand, backgroundColor: C.brandTintSoft,
  },
  histReprintText: { fontSize: 13, fontWeight: "700", color: C.brand },

  catPick: {
    paddingHorizontal: 10, paddingVertical: 6, borderRadius: 14,
    borderWidth: 1, borderColor: C.line,
  },
  catPickActive: { backgroundColor: C.brand, borderColor: C.brand },
  catPickText: { fontSize: 11, color: C.ink2, fontWeight: "600" },

  // Product image picker
  imgThumb: {
    width: 88, height: 88, borderRadius: 12,
    borderWidth: 1, borderColor: C.line, backgroundColor: C.bgSoft,
    alignItems: "center", justifyContent: "center", overflow: "hidden",
  },
  imgThumbImage: { width: "100%", height: "100%" },
  imgPickBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
    paddingVertical: 12, paddingHorizontal: 14,
    borderRadius: 10, borderWidth: 1, borderColor: C.line,
    backgroundColor: C.surface,
  },
  imgPickBtnText: { fontSize: 13, fontWeight: "600", color: C.ink },
  imgClearText: { fontSize: 12, color: C.danger, textAlign: "center" },

  favToggle: {
    flexDirection: "row", alignItems: "center", gap: 8,
    padding: 12, backgroundColor: C.bgSoft, borderRadius: 8,
  },

  // Settings
  settingsRow: {
    flexDirection: "row", alignItems: "center",
    paddingVertical: 12, paddingHorizontal: 14,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  settingsLabel: { fontSize: 13, color: C.ink2 },
  bizBtn: {
    paddingHorizontal: 16, paddingVertical: 10, borderRadius: 8,
    borderWidth: 1, borderColor: C.line,
  },
  bizBtnActive: { backgroundColor: C.brand, borderColor: C.brand },
  bizBtnText: { fontSize: 13, fontWeight: "600", color: C.ink2 },

  // Beam payment settings card
  beamSettingsCard: {
    backgroundColor: C.bgSoft, borderRadius: 12, padding: 16, gap: 12,
    borderWidth: 1, borderColor: C.line,
  },
  beamSettingsHeader: {
    flexDirection: "row", alignItems: "center", gap: 10,
  },
  beamLogoBox: {
    width: 36, height: 36, borderRadius: 8, backgroundColor: C.brand,
    alignItems: "center", justifyContent: "center",
  },
  beamSettingsTitle: { fontSize: 15, fontWeight: "700", color: C.inkStrong },
  beamSettingsSub: { fontSize: 12, color: C.ink2Soft },
  beamSettingsHint: { fontSize: 11, color: C.ink3, marginTop: 4 },
  toggleBox: {
    width: 44, height: 24, borderRadius: 12, backgroundColor: C.lineStrong,
    padding: 2, justifyContent: "center",
  },
  toggleBoxOn: { backgroundColor: C.brand },
  toggleKnob: {
    width: 20, height: 20, borderRadius: 10, backgroundColor: C.surface,
  },
  toggleKnobOn: { transform: [{ translateX: 20 }] },

  // Forms
  formLabel: { fontSize: 12, color: C.ink2, fontWeight: "600" },
  formInput: {
    borderWidth: 1, borderColor: C.line, borderRadius: 8,
    padding: 12, fontSize: 14, color: C.ink,
    backgroundColor: C.surface,
  },
  primaryBtn: {
    backgroundColor: C.brand, padding: 14, borderRadius: 10,
    alignItems: "center", justifyContent: "center", flexDirection: "row", marginTop: 6,
  },
  primaryBtnText: { color: C.surface, fontSize: 15, fontWeight: "700" },
  secondaryBtn: {
    flexDirection: "row", gap: 6, alignItems: "center", justifyContent: "center",
    paddingVertical: 14, paddingHorizontal: 16,
    borderWidth: 1, borderColor: C.line, borderRadius: 10,
    backgroundColor: C.surface,
  },
  secondaryBtnText: { color: C.ink, fontSize: 14, fontWeight: "600" },

  // Printers
  printerCard: {
    backgroundColor: C.surface, borderRadius: 12,
    borderWidth: 1, borderColor: C.line, padding: 14, gap: 8,
  },
  printerHeader: { flexDirection: "row", alignItems: "center", gap: 10 },
  printerName: { fontSize: 15, fontWeight: "700", color: C.ink },
  printerSub: { fontSize: 12, color: C.ink2Soft, marginTop: 2 },
  printerStatusPill: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingHorizontal: 10, paddingVertical: 4,
    borderRadius: 999, backgroundColor: C.bg,
  },
  printerDot: { width: 8, height: 8, borderRadius: 4 },
  printerStatusText: { fontSize: 12, fontWeight: "600", color: C.ink2 },
  printerError: { fontSize: 12, color: C.danger, marginTop: 4 },
  printerListRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingVertical: 14, paddingHorizontal: 14,
    backgroundColor: C.surface, borderRadius: 10,
    borderWidth: 1, borderColor: C.line,
  },
  printerListName: { flex: 1, fontSize: 14, fontWeight: "600", color: C.ink },
  printerListMeta: { fontSize: 12, fontWeight: "400", color: C.ink2Soft },

  // Queued-receipts list (offline-print queue) — shown inside the
  // Local Printer settings page so cashiers can see what's pending.
  queuedRow: {
    flexDirection: "row", alignItems: "center", gap: 10,
    paddingVertical: 10, paddingHorizontal: 12,
    backgroundColor: C.warnTint, borderRadius: 10,
    borderWidth: 1, borderColor: "#FCD34D",
  },
  queuedOrder: { fontSize: 14, fontWeight: "700", color: C.ink },
  queuedMeta: { fontSize: 12, color: C.ink2Soft, marginTop: 2 },
  queuedError: { fontSize: 11, color: C.warnDark, marginTop: 2 },
  queuedRemoveBtn: {
    flexDirection: "row", alignItems: "center", gap: 4,
    paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 8, borderWidth: 1, borderColor: C.dangerSoft,
    backgroundColor: C.surface,
  },
  queuedRemoveText: { fontSize: 12, color: C.danger, fontWeight: "600" },

  addPrinterBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "center",
    gap: 6, paddingVertical: 12,
  },
  addPrinterText: { color: C.brand, fontSize: 14, fontWeight: "600" },
  detectChip: {
    paddingHorizontal: 10, paddingVertical: 6,
    borderRadius: 999,
    borderWidth: 1, borderColor: C.line,
    backgroundColor: C.surface,
  },
  detectChipActive: {
    backgroundColor: C.brand, borderColor: C.brand,
  },
  detectChipText: { fontSize: 12, fontWeight: "600", color: C.ink2 },
  detectRefresh: {
    flexDirection: "row", alignItems: "center", gap: 4,
    paddingHorizontal: 8, paddingVertical: 6,
  },
  dangerBtn: {
    flexDirection: "row", gap: 6, alignItems: "center", justifyContent: "center",
    padding: 12, borderWidth: 1, borderColor: C.danger, borderRadius: 10,
  },
  typeBtn: {
    flex: 1, padding: 10, borderRadius: 8, borderWidth: 1,
    borderColor: C.line, alignItems: "center",
  },
  typeBtnActive: { backgroundColor: C.brand, borderColor: C.brand },
  typeBtnText: { fontSize: 13, fontWeight: "600", color: C.ink2 },

  // Drawer / actions
  drawerAction: {
    flexDirection: "row", alignItems: "center", gap: 6,
    padding: 12, borderWidth: 1, borderColor: C.line,
    borderRadius: 10, backgroundColor: C.surface,
  },
  drawerActionText: { fontSize: 13, color: C.ink, fontWeight: "600" },

  // Modals
  modalOverlay: {
    flex: 1, backgroundColor: C.scrim,
    alignItems: "center", justifyContent: "center", padding: 20,
  },
  modalHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    padding: 16, borderBottomWidth: 1, borderBottomColor: C.line,
  },
  modalTitle: { fontSize: 16, fontWeight: "700", color: C.ink },
  smallModal: {
    width: "85%", maxWidth: 440, backgroundColor: C.surface,
    borderRadius: 16, overflow: "hidden",
  },
  editModal: {
    width: "88%", maxWidth: 560, maxHeight: "88%",
    backgroundColor: C.surface, borderRadius: 16, overflow: "hidden",
  },

  // ── Reports header / Reports button ──
  rangeSummary: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    backgroundColor: C.bgSoft, borderRadius: 10, padding: 12,
  },
  rangeSummaryCol: { flex: 1, gap: 2 },
  rangeSummaryVal: { fontSize: 15, fontWeight: "700", color: C.ink },
  calHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingVertical: 6 },
  calNavBtn: { padding: 8, borderRadius: 8 },
  calMonth: { fontSize: 15, fontWeight: "700", color: C.ink },
  calWeekRow: { flexDirection: "row" },
  calWeekday: { flex: 1, textAlign: "center", fontSize: 11, fontWeight: "600", color: C.ink3, paddingVertical: 4 },
  calGrid: { flexDirection: "row", flexWrap: "wrap" },
  calCell: {
    width: `${100 / 7}%`, aspectRatio: 1, alignItems: "center", justifyContent: "center",
    marginVertical: 1,
  },
  calCellText: { fontSize: 14, color: C.ink },
  calCellToday: { color: C.brand, fontWeight: "700" },
  calCellSel: { backgroundColor: C.brand, borderRadius: 8 },
  calCellTextSel: { color: C.surface, fontWeight: "700" },
  calCellInRange: { backgroundColor: C.okTint },
  chPeriodRow: {
    flexDirection: "row", flexWrap: "wrap", gap: 6, justifyContent: "center",
    paddingHorizontal: 12, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },

  // ── Full-screen document scaffolding ──
  docScreen: { flex: 1, backgroundColor: C.surface },
  docTopBar: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 14, paddingVertical: 12,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  docBackBtn: { flexDirection: "row", alignItems: "center", width: 70 },
  docBackText: { fontSize: 15, color: C.ink, fontWeight: "600" },
  docTopTitle: { fontSize: 16, fontWeight: "700", color: C.ink },
  docSaveText: { fontSize: 15, color: C.brand, fontWeight: "700", width: 70, textAlign: "right" },

  // ── Channel report ──
  docDateNav: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 24,
    paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  docDateNavText: { fontSize: 15, fontWeight: "700", color: C.brand },
  chTableHead: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: C.line,
  },
  chHeadCell: { fontSize: 12, fontWeight: "700", color: C.ink2Soft, textAlign: "center" },
  chRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  chCell: { fontSize: 13, color: C.ink, textAlign: "center" },
  chIcon: {
    width: 24, height: 24, borderRadius: 6, backgroundColor: C.brandTint,
    alignItems: "center", justifyContent: "center",
  },
  chName: { fontSize: 13, fontWeight: "600", color: C.ink },
  noGpBadge: { backgroundColor: C.brandTint, borderRadius: 6, paddingHorizontal: 8, paddingVertical: 2 },
  noGpText: { fontSize: 11, color: C.brand, fontWeight: "700" },

  // ── Inventory top tab bar ──
  invTabBar: {
    borderBottomWidth: 1, borderBottomColor: C.bg, paddingVertical: 8, backgroundColor: C.surface,
  },
  invTopTab: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 10 },
  invTopTabActive: { backgroundColor: "#EFF6FF" },
  invTopTabText: { fontSize: 14, fontWeight: "600", color: C.ink2 },
  invTopTabTextActive: { color: "#2563EB", fontWeight: "700" },
  invSearchRow: {
    flexDirection: "row", alignItems: "center", gap: 8, margin: 10,
    backgroundColor: C.bg, borderRadius: 10, paddingHorizontal: 10, paddingVertical: 8,
  },
  invSearchInput: { flex: 1, fontSize: 13, color: C.ink, padding: 0 },

  // ── Document list ──
  docListBar: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 14, paddingVertical: 10, gap: 10,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  docDateRange: { flexDirection: "row", alignItems: "center", gap: 10 },
  docDateRangeText: { fontSize: 13, fontWeight: "700", color: C.brand },
  docListTotal: { fontSize: 13, color: C.ink2Soft },
  createDocBtn: {
    flexDirection: "row", alignItems: "center", gap: 4,
    borderWidth: 1, borderColor: C.line, borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 8,
  },
  createDocBtnText: { color: C.ink, fontWeight: "600", fontSize: 13 },
  docColHead: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: C.line,
  },
  docColCell: { fontSize: 12, fontWeight: "600", color: C.ink2Soft },
  docRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  docCell: { fontSize: 13, color: C.ink2 },

  // ── Create document form ──
  docForm: { padding: 14, gap: 12, borderBottomWidth: 1, borderBottomColor: C.bg },
  docFormRow: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  docField: { flex: 1, minWidth: 160, gap: 4 },
  docFieldLabel: { fontSize: 12, color: C.ink2Soft, fontWeight: "600" },
  docFieldDate: { fontSize: 14, color: C.brand, fontWeight: "700", paddingVertical: 8 },
  docInput: {
    borderWidth: 1, borderColor: C.line, borderRadius: 8,
    paddingHorizontal: 10, paddingVertical: 8, fontSize: 14, color: C.ink,
  },
  // A picker dressed as docInput so the Reason field lines up with the text
  // inputs beside it; the chevron is what marks it as a choice, not typing.
  docSelect: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    gap: 8, minHeight: 36,
  },
  docSelectText: { flex: 1, fontSize: 14, color: C.ink },
  adjTypeBtn: {
    paddingHorizontal: 18, paddingVertical: 8, borderRadius: 8,
    borderWidth: 1, borderColor: C.line,
  },
  adjTypeBtnActive: { backgroundColor: C.brand, borderColor: C.brand },
  adjTypeText: { fontSize: 14, fontWeight: "700", color: C.ink2 },

  // ── Items table ──
  itemsHead: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: C.line,
  },
  itemsHeadCell: { fontSize: 12, fontWeight: "600", color: C.ink2Soft, textAlign: "center" },
  itemRow: {
    flexDirection: "row", alignItems: "center", paddingHorizontal: 14, paddingVertical: 10, gap: 6,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  itemCell: { fontSize: 13, color: C.ink, textAlign: "center" },
  itemInput: {
    borderWidth: 1, borderColor: C.line, borderRadius: 6,
    paddingVertical: 6, paddingHorizontal: 6, alignItems: "center",
  },
  itemInputText: { fontSize: 13, color: C.ink },
  itemsAddBar: {
    backgroundColor: C.brand, margin: 14, borderRadius: 8,
    paddingVertical: 14, alignItems: "center",
  },
  itemsAddBarText: { color: C.surface, fontWeight: "700", fontSize: 14 },

  // ── Create document footer ──
  docFooter: {
    flexDirection: "row", alignItems: "center", justifyContent: "flex-end", flexWrap: "wrap", gap: 18,
    paddingHorizontal: 16, paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.bg,
  },
  footToggle: { flexDirection: "row", alignItems: "center", gap: 6 },
  footToggleLabel: { fontSize: 12, color: C.ink2Soft, fontWeight: "600" },
  footStat: { alignItems: "center" },
  footStatLabel: { fontSize: 11, color: C.ink3 },
  footStatVal: { fontSize: 15, fontWeight: "700", color: C.ink },

  // ── Product picker popup ──
  pickerOverlay: { flex: 1, backgroundColor: C.scrim, alignItems: "center", justifyContent: "center" },
  pickerCard: {
    width: "92%", maxWidth: 560, height: "82%",
    backgroundColor: C.surface, borderRadius: 16, overflow: "hidden",
  },
  pickerHead: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    paddingHorizontal: 16, paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  pickerTitle: { fontSize: 16, fontWeight: "700", color: C.ink },
  pickerDone: { fontSize: 15, fontWeight: "700", color: C.ink },
  pickerCatRow: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    margin: 14, marginBottom: 8, borderWidth: 1, borderColor: C.line, borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 12,
  },
  pickerCatText: { fontSize: 14, color: C.ink },
  pickerCatList: {
    marginHorizontal: 14, marginTop: -4, marginBottom: 8,
    borderWidth: 1, borderColor: C.line, borderRadius: 10, overflow: "hidden",
  },
  pickerCatItem: { paddingHorizontal: 12, paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: C.bg },
  pickerCatItemText: { fontSize: 13, color: C.ink },
  pickerSearchRow: {
    flexDirection: "row", alignItems: "center", gap: 8, marginHorizontal: 14,
    backgroundColor: C.bg, borderRadius: 10, paddingHorizontal: 10, paddingVertical: 10,
  },
  pickerSearchInput: { flex: 1, fontSize: 14, color: C.ink, padding: 0 },
  pickerSortRow: { flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 14, paddingVertical: 12 },
  pickerRow: {
    flexDirection: "row", alignItems: "center", gap: 12, paddingHorizontal: 14, paddingVertical: 10,
    borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  pickerImg: { width: 44, height: 44, borderRadius: 8, backgroundColor: C.bg },
  pickerName: { fontSize: 14, fontWeight: "600", color: C.ink },
  pickerBarcode: { fontSize: 12, color: C.ink3 },

  // ── Amount keypad ──
  keypadOverlay: { flex: 1, backgroundColor: C.scrimSoft, alignItems: "center", justifyContent: "center" },
  keypadCard: { width: 300, backgroundColor: C.surface, borderRadius: 16, overflow: "hidden", paddingTop: 16 },
  keypadTitle: { fontSize: 15, fontWeight: "700", color: C.ink, textAlign: "center", marginBottom: 8 },
  keypadValue: {
    fontSize: 34, fontWeight: "700", color: C.ink, textAlign: "right",
    paddingHorizontal: 24, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.bg,
  },
  keypadGrid: { flexDirection: "row", flexWrap: "wrap" },
  keypadKey: {
    width: "33.333%", height: 60, alignItems: "center", justifyContent: "center",
    borderBottomWidth: 1, borderRightWidth: 1, borderColor: C.bg,
  },
  keypadKeyText: { fontSize: 22, fontWeight: "600", color: C.ink },
  keypadDone: { backgroundColor: C.brand, paddingVertical: 16, alignItems: "center" },
  keypadDoneText: { color: C.surface, fontSize: 16, fontWeight: "700" },

  // ── Reconcile (adjust/check) form ──
  importBtn: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    borderWidth: 1, borderColor: C.line, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 12,
  },
  importBtnText: { fontSize: 14, color: C.brand, fontWeight: "600" },
  itemCellRO: {
    fontSize: 13, color: C.ink2Soft, textAlign: "right",
    backgroundColor: C.bgSoft, borderRadius: 6, paddingVertical: 6, paddingHorizontal: 6,
  },

  // ── Select Documents popup ──
  loadDocsBtn: {
    borderWidth: 1, borderColor: C.line, borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 8,
  },
  loadDocsText: { fontSize: 14, fontWeight: "600", color: C.ink },
  selDateRow: { flexDirection: "row", gap: 16, paddingHorizontal: 16, paddingVertical: 14 },
  selDateField: { flexDirection: "row", alignItems: "center", gap: 8 },
  selDateVal: { fontSize: 14, color: C.brand, fontWeight: "600" },
  selCalPop: {
    marginHorizontal: 16, marginBottom: 8, padding: 8,
    borderWidth: 1, borderColor: C.line, borderRadius: 12, backgroundColor: C.surface,
  },
});
