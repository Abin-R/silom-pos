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
  ActivityIndicator,
  useWindowDimensions,
  Platform,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter, useLocalSearchParams } from "expo-router";
import { Ionicons, MaterialCommunityIcons } from "@expo/vector-icons";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const THB = (n: number) => `฿${(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// Mask prefix used by the backend to redact stored Beam API keys (••••<last4>).
// Kept in sync with backend/server.py BEAM_API_KEY_MASK_PREFIX.
const BEAM_API_KEY_MASK_PREFIX = "••••";

type Section = "transactions" | "reports" | "inventory" | "customers" | "products" | "drawer" | "settings";

type Category = { id: string; name: string; name_th?: string; color: string; source?: string; order: number };
type Product = {
  id: string; name: string; name_th?: string; price: number; cost: number;
  category_id: string; image_url: string; is_favorite: boolean;
  stock: number; tax_type: string; product_type: string;
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
  const { staff } = useLocalSearchParams<{ staff?: string }>();
  const { width } = useWindowDimensions();
  const isWide = width >= 900;
  const [section, setSection] = useState<Section>("reports");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const items: { key: Section | "shop"; label: string; icon: any }[] = [
    { key: "shop", label: "Shop", icon: "home-outline" },
    { key: "transactions", label: "Transactions", icon: "swap-horizontal-outline" },
    { key: "reports", label: "Reports", icon: "pie-chart-outline" },
    { key: "inventory", label: "Inventory", icon: "cube-outline" },
    { key: "customers", label: "Customers", icon: "people-outline" },
    { key: "products", label: "Products", icon: "gift-outline" },
    { key: "drawer", label: "Drawer", icon: "calculator-outline" },
    { key: "settings", label: "Settings", icon: "settings-outline" },
  ];

  const navigate = (k: Section | "shop") => {
    setSidebarOpen(false);
    if (k === "shop") {
      router.replace({ pathname: "/pos", params: { staff: staff || "Admin" } });
    } else {
      setSection(k);
    }
  };

  const Sidebar = (
    <View style={styles.sidebar} testID="admin-sidebar">
      <View style={styles.avatarBox}>
        <View style={styles.avatarCircle}>
          <Ionicons name="person" size={32} color="#475569" />
        </View>
        <Text style={styles.avatarText}>{staff || "Admin"}</Text>
      </View>
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
      <View style={{ flex: 1 }} />
      <TouchableOpacity
        style={styles.logoutSide}
        onPress={() => router.replace("/")}
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
      <Text style={styles.versionText}>Version 1.0.0</Text>
    </View>
  );

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
              <TouchableOpacity onPress={() => router.replace("/")}>
                <Ionicons name="log-out-outline" size={22} color="#EF4444" />
              </TouchableOpacity>
            </View>
            <Modal
              visible={sidebarOpen}
              animationType="slide"
              transparent
              onRequestClose={() => setSidebarOpen(false)}
            >
              <View style={styles.mobileSidebarOverlay}>
                {Sidebar}
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
          {section === "transactions" && <Transactions isWide={isWide} />}
          {section === "inventory" && <Inventory isWide={isWide} />}
          {section === "customers" && <Customers isWide={isWide} />}
          {section === "products" && <Products isWide={isWide} />}
          {section === "drawer" && <Drawer />}
          {section === "settings" && <SettingsView isWide={isWide} />}
        </View>
      </View>
    </SafeAreaView>
  );
}

// =================== REPORTS / DASHBOARD ===================
function Reports({ isWide }: { isWide: boolean }) {
  const [period, setPeriod] = useState("month");
  const [data, setData] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API}/dashboard?period=${period}`);
      setData(await res.json());
    } catch {}
    setLoading(false);
  }, [period]);
  useEffect(() => { load(); }, [load]);

  const periods = [
    { k: "today", l: "Today" },
    { k: "week", l: "This week" },
    { k: "month", l: "This month" },
    { k: "year", l: "This year" },
  ];

  const maxBar = Math.max(1, ...(data?.timeline || []).map((t) => t.value));
  const topProdTotal = (data?.top_products || []).reduce((s, p) => s + p.total, 0) || 1;
  const topCatTotal = (data?.top_categories || []).reduce((s, c) => s + c.total, 0) || 1;
  const palette = ["#00B14F", "#EF4444", "#F59E0B", "#3B82F6", "#8B5CF6"];

  return (
    <ScrollView style={{ flex: 1 }} contentContainerStyle={{ padding: 16 }} testID="reports-section">
      <Text style={styles.h1}>Sales Dashboard</Text>

      <View style={styles.periodRow}>
        {periods.map((p) => (
          <TouchableOpacity
            key={p.k}
            style={[styles.periodBtn, period === p.k && styles.periodBtnActive]}
            onPress={() => setPeriod(p.k)}
            testID={`period-${p.k}`}
          >
            <Text style={[styles.periodText, period === p.k && styles.periodTextActive]}>
              {p.l}
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

// =================== TRANSACTIONS ===================
function Transactions({ isWide }: { isWide: boolean }) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [selected, setSelected] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [showDetail, setShowDetail] = useState(false);

  useEffect(() => {
    (async () => {
      const res = await fetch(`${API}/orders`);
      const o: Order[] = await res.json();
      setOrders(o);
      if (o[0] && isWide) setSelected(o[0]);
      setLoading(false);
    })();
  }, [isWide]);

  if (loading) return <ActivityIndicator color="#00B14F" style={{ marginTop: 40 }} />;

  // Mobile drill-down: show list OR detail
  if (!isWide && showDetail && selected) {
    return (
      <View style={{ flex: 1 }}>
        <TouchableOpacity style={styles.backRow} onPress={() => setShowDetail(false)}>
          <Ionicons name="chevron-back" size={22} color="#00B14F" />
          <Text style={styles.backText}>Back to transactions</Text>
        </TouchableOpacity>
        <ScrollView contentContainerStyle={{ padding: 16 }}>
          <TransactionDetail order={selected} />
        </ScrollView>
      </View>
    );
  }

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="transactions-section">
      <View style={[styles.txList, !isWide && styles.fullCol]}>
        <Text style={styles.sectionHeader}>Sale Transactions</Text>
        <FlatList
          data={orders}
          keyExtractor={(i) => i.id}
          ItemSeparatorComponent={() => <View style={styles.divider} />}
          renderItem={({ item }) => (
            <TouchableOpacity
              style={[
                styles.txRow,
                selected?.id === item.id && isWide && styles.txRowActive,
              ]}
              onPress={() => { setSelected(item); if (!isWide) setShowDetail(true); }}
              testID={`tx-${item.order_number}`}
            >
              <Ionicons name="folder-outline" size={18} color="#94A3B8" />
              <View style={{ flex: 1 }}>
                <Text style={styles.txNum}>{item.order_number}</Text>
                <Text style={styles.txTime}>{item.created_time}</Text>
              </View>
              <Text style={styles.txAmount}>{THB(item.total)}</Text>
            </TouchableOpacity>
          )}
        />
      </View>
      {isWide && (
        <View style={styles.txDetail}>
          <Text style={styles.sectionHeader}>Description</Text>
          {!selected ? (
            <View style={styles.emptyBox}>
              <Text style={styles.emptyText}>Please select bill</Text>
            </View>
          ) : (
            <ScrollView contentContainerStyle={{ padding: 20 }}>
              <TransactionDetail order={selected} />
            </ScrollView>
          )}
        </View>
      )}
    </View>
  );
}

function TransactionDetail({ order }: { order: Order }) {
  return (
    <View style={styles.receipt}>
      <Text style={styles.receiptTitle}>{order.order_number}</Text>
      <Text style={styles.receiptSub}>{new Date(order.created_at).toLocaleString()}</Text>
      <View style={styles.divider2} />
      {order.items.map((it: any, i: number) => (
        <View key={i} style={styles.receiptRow}>
          <Text style={styles.receiptItem} numberOfLines={1}>
            {it.qty}× {it.name}
          </Text>
          <Text style={styles.receiptVal}>{THB(it.price * it.qty)}</Text>
        </View>
      ))}
      <View style={styles.divider2} />
      <View style={styles.receiptRow}>
        <Text style={styles.receiptLabel}>Method</Text>
        <Text style={styles.receiptVal}>{order.payment_method || "-"}</Text>
      </View>
      <View style={styles.receiptRow}>
        <Text style={styles.receiptLabel}>Source</Text>
        <Text style={styles.receiptVal}>{order.source}</Text>
      </View>
      {order.delivery_provider && (
        <View style={styles.receiptRow}>
          <Text style={styles.receiptLabel}>Delivery</Text>
          <Text style={styles.receiptVal}>
            {order.delivery_provider} · {order.delivery_status}
          </Text>
        </View>
      )}
      <View style={styles.divider2} />
      <View style={styles.receiptRow}>
        <Text style={styles.receiptTotal}>Total</Text>
        <Text style={styles.receiptTotal}>{THB(order.total)}</Text>
      </View>
    </View>
  );
}

// =================== INVENTORY ===================
function Inventory({ isWide }: { isWide: boolean }) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("");
  const [tab, setTab] = useState<"movement" | "in" | "out" | "adjust" | "check">("movement");
  const [stockModal, setStockModal] = useState<Product | null>(null);
  const [sortBy, setSortBy] = useState<"custom" | "name" | "inventory">("custom");

  const load = async () => {
    const [c, p] = await Promise.all([
      fetch(`${API}/categories`).then((r) => r.json()),
      fetch(`${API}/products`).then((r) => r.json()),
    ]);
    setCategories(c);
    setProducts(p);
    if (!activeCat && c.length) setActiveCat(c[1]?.id || c[0].id);
  };
  useEffect(() => { load(); }, []);

  const filtered = useMemo(() => {
    const list = products.filter((p) => p.category_id === activeCat);
    if (sortBy === "name") return [...list].sort((a, b) => a.name.localeCompare(b.name));
    if (sortBy === "inventory") return [...list].sort((a, b) => a.stock - b.stock);
    return list;
  }, [products, activeCat, sortBy]);
  const curCat = categories.find((c) => c.id === activeCat);

  const doMovement = async (type: "in" | "out" | "adjust", qty: number) => {
    if (!stockModal) return;
    await fetch(`${API}/stock-movements`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_id: stockModal.id, type, qty, note: `Admin ${type}` }),
    });
    setStockModal(null);
    load();
  };

  return (
    <View style={{ flex: 1 }} testID="inventory-section">
      <View style={[styles.twoCol, !isWide && styles.stackedCol, { flex: 1 }]}>
        <View style={styles.leftNav}>
          <Text style={styles.sectionHeader}>Stock Movement</Text>
          <ScrollView>
            {categories.filter((c) => c.name !== "Favorite").map((c) => (
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
            renderItem={({ item }) => (
              <TouchableOpacity
                style={styles.invRow}
                onPress={() => setStockModal(item)}
                testID={`inv-prod-${item.id}`}
              >
                <Image source={{ uri: item.image_url }} style={styles.invImg} />
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

      <View style={styles.invTabs}>
        {[
          { k: "movement", l: "Stock Movement", i: "list-outline" },
          { k: "in", l: "Stock-In", i: "arrow-down-outline" },
          { k: "out", l: "Stock-Out", i: "arrow-up-outline" },
          { k: "adjust", l: "Adjust Stock", i: "construct-outline" },
          { k: "check", l: "Check Stock", i: "checkmark-circle-outline" },
        ].map((t) => (
          <TouchableOpacity
            key={t.k}
            style={[styles.invTab, tab === t.k && styles.invTabActive]}
            onPress={() => setTab(t.k as any)}
            testID={`inv-tab-${t.k}`}
          >
            <Ionicons
              name={t.i as any}
              size={16}
              color={tab === t.k ? "#00B14F" : "#475569"}
            />
            <Text style={[styles.invTabText, tab === t.k && { color: "#00B14F" }]}>
              {t.l}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      <StockMovementModal
        product={stockModal}
        defaultType={tab === "in" ? "in" : tab === "out" ? "out" : tab === "adjust" ? "adjust" : "in"}
        onClose={() => setStockModal(null)}
        onSave={doMovement}
      />
    </View>
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

  const load = async () => {
    const r = await fetch(`${API}/customers`);
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
      const r = await fetch(`${API}/customers/${customerId}/stats`);
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
    const r = await fetch(`${API}/customers`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, phone: phone || null }),
    });
    const c = await r.json();
    setList((l) => [c, ...l]);
    setSel(c); setName(""); setPhone(""); setAddOpen(false);
  };

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="customers-section">
      <View style={styles.leftNav}>
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
              <TextInput placeholder="Phone (optional)" style={styles.formInput} value={phone} onChangeText={setPhone} keyboardType="phone-pad" testID="admin-cust-phone" />
              <TouchableOpacity style={styles.primaryBtn} onPress={save} testID="admin-cust-save">
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
function Products({ isWide }: { isWide: boolean }) {
  const [cats, setCats] = useState<Category[]>([]);
  const [prods, setProds] = useState<Product[]>([]);
  const [activeCat, setActiveCat] = useState<string>("");
  const [edit, setEdit] = useState<Product | "new" | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"custom" | "name">("custom");

  const load = async () => {
    const [c, p] = await Promise.all([
      fetch(`${API}/categories`).then((r) => r.json()),
      fetch(`${API}/products`).then((r) => r.json()),
    ]);
    setCats(c); setProds(p);
    if (!activeCat && c.length) setActiveCat(c[0].id);
  };
  useEffect(() => { load(); }, []);

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
    await fetch(`${API}/products/${p.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_favorite: !p.is_favorite }),
    });
    load();
  };

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="products-section">
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
            <TouchableOpacity testID="edit-products">
              <Text style={styles.linkText}>Edit</Text>
            </TouchableOpacity>
            <TouchableOpacity onPress={() => setEdit("new")} testID="add-product">
              <Text style={styles.linkTextBold}>Add Product</Text>
            </TouchableOpacity>
          </View>
        </View>
        <FlatList
          data={filtered}
          keyExtractor={(i) => i.id}
          contentContainerStyle={{ padding: 14 }}
          renderItem={({ item }) => (
            <View style={styles.prodMgmtRow} testID={`prod-${item.id}`}>
              <Image source={{ uri: item.image_url }} style={styles.invImg} />
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
              <TouchableOpacity onPress={() => toggleFav(item)} testID={`fav-${item.id}`}>
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
              <TouchableOpacity style={styles.editBtn} onPress={() => setEdit(item)} testID={`edit-${item.id}`}>
                <Ionicons name="create-outline" size={18} color="#475569" />
              </TouchableOpacity>
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
  const [fav, setFav] = useState(false);
  const isNew = product === "new";

  useEffect(() => {
    if (product && product !== "new") {
      setName(product.name); setPrice(String(product.price));
      setCost(String(product.cost)); setStock(String(product.stock));
      setCatId(product.category_id); setImg(product.image_url); setFav(product.is_favorite);
    } else if (product === "new") {
      setName(""); setPrice(""); setCost("0"); setStock("0");
      setCatId(defaultCat); setImg(""); setFav(false);
    }
  }, [product, defaultCat]);

  const save = async () => {
    if (!name.trim() || !price || !catId) return;
    const body = {
      name, price: parseFloat(price), cost: parseFloat(cost || "0"),
      stock: parseInt(stock || "0"), category_id: catId,
      image_url: img || "https://images.pexels.com/photos/36500580/pexels-photo-36500580.jpeg?w=400",
      is_favorite: fav, tax_type: "V", product_type: "P",
    };
    if (isNew) {
      await fetch(`${API}/products`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    } else if (product) {
      await fetch(`${API}/products/${(product as Product).id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    }
    onSaved();
  };

  const del = async () => {
    if (!product || product === "new") return;
    await fetch(`${API}/products/${(product as Product).id}`, { method: "DELETE" });
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
              <Text style={styles.formLabel}>Image URL</Text>
              <TextInput style={styles.formInput} value={img} onChangeText={setImg} placeholder="https://..." testID="prod-img" />
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

function Drawer() {
  const [current, setCurrent] = useState<ShiftType | null>(null);
  const [history, setHistory] = useState<ShiftType[]>([]);
  const [tab, setTab] = useState<"shift" | "history">("shift");
  const [openDlg, setOpenDlg] = useState(false);
  const [closeDlg, setCloseDlg] = useState(false);
  const [moveDlg, setMoveDlg] = useState<"paid_in" | "paid_out" | null>(null);
  const [startCash, setStartCash] = useState("0");
  const [actualCash, setActualCash] = useState("0");
  const [moveAmt, setMoveAmt] = useState("");
  const [moveNote, setMoveNote] = useState("");

  const load = async () => {
    const [cur, hist] = await Promise.all([
      fetch(`${API}/shifts/current`).then((r) => r.json()),
      fetch(`${API}/shifts`).then((r) => r.json()),
    ]);
    setCurrent(cur && cur.id ? cur : null);
    setHistory(hist || []);
  };
  useEffect(() => { load(); }, []);

  const openShift = async () => {
    await fetch(`${API}/shifts/open`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_cash: parseFloat(startCash) || 0, opened_by: "Admin" }),
    });
    setOpenDlg(false); setStartCash("0"); load();
  };
  const closeShift = async () => {
    await fetch(`${API}/shifts/close`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actual_in_drawer: parseFloat(actualCash) || 0, closed_by: "Admin" }),
    });
    setCloseDlg(false); setActualCash("0"); load();
  };
  const addMovement = async () => {
    if (!moveDlg || !moveAmt) return;
    await fetch(`${API}/shifts/movement`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: moveDlg, amount: parseFloat(moveAmt), note: moveNote }),
    });
    setMoveDlg(null); setMoveAmt(""); setMoveNote(""); load();
  };

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
        <FlatList
          data={history}
          keyExtractor={(i) => i.id}
          contentContainerStyle={{ padding: 16, gap: 10 }}
          ListEmptyComponent={<View style={styles.emptyBox}><Text style={styles.emptyText}>No shift history</Text></View>}
          renderItem={({ item }) => (
            <View style={styles.histRow} testID={`shift-hist-${item.id}`}>
              <View style={{ flex: 1 }}>
                <Text style={styles.histRound}>Round #{item.round_number} · {item.status === "open" ? "OPEN" : "CLOSED"}</Text>
                <Text style={styles.histTime}>
                  {fmtDT(item.opened_at)}{item.closed_at ? ` → ${fmtDT(item.closed_at)}` : ""}
                </Text>
              </View>
              <View style={{ alignItems: "flex-end" }}>
                <Text style={styles.histAmt}>{THB(item.total_sales_cash)}</Text>
                <Text style={styles.histSub}>Cash sales</Text>
              </View>
            </View>
          )}
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

      {/* Close Shift dialog */}
      <Modal visible={closeDlg} transparent animationType="fade" onRequestClose={() => setCloseDlg(false)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setCloseDlg(false)}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>Close Shift</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.helperText}>Expected: {THB(expected)}</Text>
              <Text style={styles.formLabel}>Actual Cash Count (THB)</Text>
              <TextInput style={styles.formInput} value={actualCash} onChangeText={setActualCash} keyboardType="decimal-pad" testID="actual-cash" />
              <TouchableOpacity style={[styles.primaryBtn, { backgroundColor: "#EF4444" }]} onPress={closeShift} testID="confirm-close-shift">
                <Text style={styles.primaryBtnText}>Close Shift</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* Paid-In/Out dialog */}
      <Modal visible={!!moveDlg} transparent animationType="fade" onRequestClose={() => setMoveDlg(null)}>
        <View style={styles.modalOverlay}>
          <View style={styles.smallModal}>
            <View style={styles.modalHead}>
              <TouchableOpacity onPress={() => setMoveDlg(null)}><Ionicons name="close" size={24} color="#475569" /></TouchableOpacity>
              <Text style={styles.modalTitle}>{moveDlg === "paid_in" ? "Paid In" : "Paid Out"}</Text><View style={{ width: 24 }} />
            </View>
            <View style={{ padding: 20, gap: 14 }}>
              <Text style={styles.formLabel}>Amount (THB)</Text>
              <TextInput style={styles.formInput} value={moveAmt} onChangeText={setMoveAmt} keyboardType="decimal-pad" testID="move-amt" />
              <Text style={styles.formLabel}>Note</Text>
              <TextInput style={styles.formInput} value={moveNote} onChangeText={setMoveNote} placeholder="Reason" testID="move-note" />
              <TouchableOpacity style={styles.primaryBtn} onPress={addMovement} testID="confirm-movement">
                <Text style={styles.primaryBtnText}>Save</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
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

// =================== OLD DRAWER (removed below, see new one above) ===================

// =================== SETTINGS ===================
function SettingsView({ isWide }: { isWide: boolean }) {
  const sections: { name: string; icon: any; color: string }[] = [
    { name: "Shop", icon: "home", color: "#EF4444" },
    { name: "Floor plan", icon: "grid", color: "#3B82F6" },
    { name: "Language", icon: "language", color: "#8B5CF6" },
    { name: "Receipt", icon: "receipt", color: "#EF4444" },
    { name: "Payment", icon: "card", color: "#F59E0B" },
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

  useEffect(() => {
    fetch(`${API}/settings`).then((r) => r.json()).then(setS);
  }, []);

  const update = (patch: Partial<Settings>) => setS((c) => (c ? { ...c, ...patch } : c));

  const save = async () => {
    if (!s) return;
    setSaving(true);
    await fetch(`${API}/settings`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s),
    });
    setSaving(false);
  };

  return (
    <View style={[styles.twoCol, !isWide && styles.stackedCol]} testID="settings-section">
      <View style={styles.leftNav}>
        <Text style={styles.sectionHeader}>Settings</Text>
        <ScrollView>
          {sections.map((sec) => (
            <TouchableOpacity
              key={sec.name}
              style={[styles.settingsRow, active === sec.name && styles.leftNavRowActive]}
              onPress={() => setActive(sec.name)}
              testID={`settings-${sec.name}`}
            >
              <Ionicons name={sec.icon} size={18} color={sec.color} style={{ marginRight: 10 }} />
              <Text style={[styles.settingsLabel, { flex: 1 }, active === sec.name && { color: "#00B14F", fontWeight: "700" }]}>
                {sec.name}
              </Text>
              <Ionicons name="chevron-forward" size={14} color="#94A3B8" />
            </TouchableOpacity>
          ))}
        </ScrollView>
      </View>
      <View style={{ flex: 1 }}>
        {active === "Shop" && s ? (
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
                  <Text style={styles.beamSettingsTitle}>Beam QR Payment</Text>
                  <Text style={styles.beamSettingsSub}>PromptPay QR via Beam Checkout</Text>
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
            </View>

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
        ) : (
          <View style={styles.emptyBox}>
            <Ionicons name="construct-outline" size={40} color="#CBD5E1" />
            <Text style={styles.emptyText}>{active}</Text>
            <Text style={[styles.emptyText, { color: "#CBD5E1" }]}>Coming soon</Text>
          </View>
        )}
      </View>
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

  const transport = (s.printer_transport ?? "disabled") as "disabled" | "file" | "network";
  const enabled = s.printer_enabled ?? false;
  const address = s.printer_address ?? "";
  const configured = transport !== "disabled";

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${API}/printers/status`);
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
      const r = await fetch(`${API}/printers/detect`);
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
      const r = await fetch(`${API}/print-test`, { method: "POST" });
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

  // ── List view (default) ── matches the Silom POS reference: just a row + add link.
  if (!editing) {
    return (
      <ScrollView contentContainerStyle={{ padding: 20, gap: 14 }}>
        <Text style={styles.h2}>Printers</Text>

        {configured && (
          <TouchableOpacity
            style={styles.printerListRow}
            onPress={() => setEditing(true)}
            testID="printer-row"
          >
            <Text style={styles.printerListName}>
              Receipt Printer{" "}
              <Text style={styles.printerListMeta}>
                ({transport.toUpperCase()} · {address || "—"})
              </Text>
            </Text>
            <View style={styles.printerStatusPill}>
              <View style={[styles.printerDot, { backgroundColor: dotColor }]} />
              <Text style={styles.printerStatusText}>{statusLabel}</Text>
            </View>
            <Ionicons name="chevron-forward" size={18} color="#94A3B8" />
          </TouchableOpacity>
        )}

        <TouchableOpacity
          style={styles.addPrinterBtn}
          onPress={() => setEditing(true)}
          testID="add-printer"
        >
          <Ionicons name="add" size={18} color="#00B14F" />
          <Text style={styles.addPrinterText}>
            {configured ? "Edit Printer" : "Add Printer"}
          </Text>
        </TouchableOpacity>
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

  // Sidebar
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
  txRow: { flexDirection: "row", alignItems: "center", gap: 10, padding: 12 },
  txRowActive: { backgroundColor: "#E5F7ED" },
  txNum: { fontSize: 13, color: "#3B82F6", fontWeight: "600" },
  txTime: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  txAmount: { fontSize: 14, color: "#0F172A", fontWeight: "700" },
  divider: { height: 1, backgroundColor: "#F1F5F9" },
  divider2: { height: 1, backgroundColor: "#E2E8F0", marginVertical: 10 },
  receipt: {
    backgroundColor: "#FFFFFF", padding: 20, borderRadius: 12,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  receiptTitle: { fontSize: 18, fontWeight: "700", color: "#0F172A" },
  receiptSub: { fontSize: 12, color: "#94A3B8", marginTop: 4 },
  receiptRow: { flexDirection: "row", justifyContent: "space-between", marginBottom: 6 },
  receiptItem: { flex: 1, fontSize: 13, color: "#475569" },
  receiptLabel: { fontSize: 12, color: "#94A3B8" },
  receiptVal: { fontSize: 13, color: "#0F172A", fontWeight: "500" },
  receiptTotal: { fontSize: 15, color: "#0F172A", fontWeight: "700" },
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
    borderRadius: 10, alignItems: "center",
  },
  closeShiftText: { color: "#FFFFFF", fontSize: 16, fontWeight: "700", letterSpacing: 1 },
  histRow: {
    flexDirection: "row", padding: 14, gap: 10,
    backgroundColor: "#FFFFFF", borderRadius: 10,
    borderWidth: 1, borderColor: "#F1F5F9",
  },
  histRound: { fontSize: 13, fontWeight: "700", color: "#0F172A" },
  histTime: { fontSize: 11, color: "#94A3B8", marginTop: 2 },
  histAmt: { fontSize: 14, fontWeight: "700", color: "#00B14F" },
  histSub: { fontSize: 10, color: "#94A3B8" },

  catPick: {
    paddingHorizontal: 10, paddingVertical: 6, borderRadius: 14,
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  catPickActive: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  catPickText: { fontSize: 11, color: "#475569", fontWeight: "600" },
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
    alignItems: "center", marginTop: 6,
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
});
