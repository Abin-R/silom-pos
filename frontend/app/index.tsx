/**
 * Login screen — PIN-pad style:
 *   1. Fetch active branches; user picks one (default: first / last-used).
 *   2. Fetch the staff list for that branch (admin + the branch's cashier).
 *   3. Tap a user, enter a 4-digit PIN on the keypad → auto-submits.
 *   4. POST /api/auth/pin-login → backend replaces any existing
 *      BranchSession on this branch and returns a token + role.
 *   5. Both roles land on /pos; admin reaches admin screens via the sidebar.
 *
 * Layout: side-by-side panels on tablet/desktop; on phone we render two
 * sequential states (user picker → PIN pad) so the keypad isn't squeezed.
 */
import { useEffect, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  TouchableOpacity,
  ActivityIndicator,
  FlatList,
  Modal,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { setAuthToken } from "../lib/api";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const BRANCH_KEY = "bravepos:selected-branch:v1";
const AUTH_KEY = "bravepos:auth:v1";
const PIN_LENGTH = 4;

type Branch = { id: string; name: string; code?: string; active: boolean };
type BranchUser = { id: string; name: string; role: "admin" | "cashier" };

const THAI_MONTHS = [
  "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
  "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
];
const THAI_DAYS = [
  "วันอาทิตย์", "วันจันทร์", "วันอังคาร", "วันพุธ",
  "วันพฤหัสบดี", "วันศุกร์", "วันเสาร์",
];

function formatThaiDate(d: Date): string {
  return `${THAI_DAYS[d.getDay()]}ที่ ${d.getDate()} ${THAI_MONTHS[d.getMonth()]} ${d.getFullYear() + 543}`;
}

function formatClock(d: Date): string {
  const h = String(d.getHours()).padStart(2, "0");
  const m = String(d.getMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

export default function Login() {
  const router = useRouter();
  const { width, height } = useWindowDimensions();
  const isWide = width >= 700;
  const insets = useSafeAreaInsets();
  // Pack things tighter on shorter phones so the keypad never hits the
  // gesture-nav strip. Tested visually against ~640px-tall androids.
  const isShort = height < 760;

  // ── Session restore ────────────────────────────────────────────────────
  const [checkingSession, setCheckingSession] = useState(true);
  useEffect(() => {
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(AUTH_KEY);
        const saved = raw ? JSON.parse(raw) : null;
        if (saved?.token) {
          const res = await fetch(`${API}/auth/me`, {
            headers: { Authorization: `Bearer ${saved.token}` },
          });
          if (res.ok) {
            const me = await res.json();
            const role = me.staff?.role || "cashier";
            setAuthToken(saved.token);
            router.replace({
              pathname: "/pos",
              params: {
                staff: me.staff?.name || "",
                role,
                branch_id: me.branch?.id || "",
                branch_name: me.branch?.name || "",
              },
            });
            return;
          }
          await AsyncStorage.removeItem(AUTH_KEY);
        }
      } catch {
        // network down / corrupt JSON — fall through to login form.
      }
      setCheckingSession(false);
    })();
  }, [router]);

  // ── Clock ──────────────────────────────────────────────────────────────
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);

  // ── Branches ───────────────────────────────────────────────────────────
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branch, setBranch] = useState<Branch | null>(null);
  const [showBranchPicker, setShowBranchPicker] = useState(false);
  const [branchLoadError, setBranchLoadError] = useState<string>("");
  const [branchLoading, setBranchLoading] = useState(true);

  const loadBranches = async () => {
    setBranchLoading(true);
    setBranchLoadError("");
    try {
      const savedBranchRaw = await AsyncStorage.getItem(BRANCH_KEY);
      const r = await fetch(`${API}/branches?active=true`);
      if (!r.ok) {
        setBranchLoadError(`Server responded ${r.status}. URL: ${API}`);
        return;
      }
      const list: Branch[] = await r.json();
      setBranches(list);
      const saved = savedBranchRaw ? (JSON.parse(savedBranchRaw) as Branch) : null;
      const initial = (saved && list.find((b) => b.id === saved.id)) || list[0] || null;
      setBranch(initial);
    } catch (e: any) {
      setBranchLoadError(`${e?.message || String(e)}\nURL: ${API}`);
    } finally {
      setBranchLoading(false);
    }
  };
  useEffect(() => { loadBranches(); }, []);

  // ── Users for the chosen branch ────────────────────────────────────────
  const [users, setUsers] = useState<BranchUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [selectedUser, setSelectedUser] = useState<BranchUser | null>(null);
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!branch) return;
    let cancelled = false;
    (async () => {
      setUsersLoading(true);
      setError("");
      try {
        const r = await fetch(`${API}/auth/branch-users?branch_id=${branch.id}`);
        const body = await r.json().catch(() => ({} as any));
        if (cancelled) return;
        if (!r.ok) {
          setUsers([]);
          setError(body?.detail || "Couldn't load users for this branch.");
          return;
        }
        setUsers(Array.isArray(body.users) ? body.users : []);
        setSelectedUser(null);
        setPin("");
      } catch (e: any) {
        if (!cancelled) {
          setUsers([]);
          setError(e?.message || "Network error.");
        }
      } finally {
        if (!cancelled) setUsersLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [branch]);

  // ── PIN submit ─────────────────────────────────────────────────────────
  const submitPin = async (pinToSubmit: string) => {
    if (submitting || !branch || !selectedUser) return;
    setSubmitting(true);
    setError("");
    try {
      const res = await fetch(`${API}/auth/pin-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          branch_id: branch.id,
          staff_id: selectedUser.id,
          pin: pinToSubmit,
        }),
      });
      const body = await res.json().catch(() => ({} as any));
      if (!res.ok) {
        setError(body?.detail || "Invalid PIN.");
        setPin("");
        return;
      }
      await AsyncStorage.setItem(
        AUTH_KEY,
        JSON.stringify({ token: body.token, staff: body.staff, branch: body.branch }),
      );
      await AsyncStorage.setItem(BRANCH_KEY, JSON.stringify(branch));
      setAuthToken(body.token);

      const role = body.staff?.role || "cashier";
      router.replace({
        pathname: "/pos",
        params: {
          staff: body.staff?.name || "",
          role,
          branch_id: branch.id,
          branch_name: branch.name,
        },
      });
    } catch {
      setError("Network error. Please retry.");
      setPin("");
    } finally {
      setSubmitting(false);
    }
  };

  const onDigit = (d: string) => {
    if (submitting) return;
    if (!selectedUser) {
      setError("Please choose a user first.");
      return;
    }
    setError("");
    setPin((p) => {
      if (p.length >= PIN_LENGTH) return p;
      const next = p + d;
      if (next.length === PIN_LENGTH) {
        setTimeout(() => submitPin(next), 50);
      }
      return next;
    });
  };
  const onBackspace = () => {
    if (submitting) return;
    setError("");
    setPin((p) => p.slice(0, -1));
  };

  if (checkingSession) {
    return (
      <SafeAreaView style={{ flex: 1, backgroundColor: "#FFFFFF" }}>
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
          <ActivityIndicator color="#00B14F" />
        </View>
      </SafeAreaView>
    );
  }

  // ── Shared sub-renderers ───────────────────────────────────────────────
  const BranchButton = () => (
    <>
      <TouchableOpacity
        style={s.branchBtn}
        onPress={() => setShowBranchPicker(true)}
        disabled={branches.length <= 1}
        testID="branch-picker-btn"
      >
        <Ionicons name="storefront-outline" size={16} color="#0F172A" />
        <Text style={s.branchLabel} numberOfLines={1}>
          {branch?.name
            || (branchLoading ? "Loading branches…" : branchLoadError ? "Couldn't load branches" : "Select branch")}
        </Text>
        {branches.length > 1 && (
          <Ionicons name="chevron-down" size={14} color="#94A3B8" />
        )}
      </TouchableOpacity>
      {!!branchLoadError && (
        <View style={{ marginBottom: 12 }}>
          <Text style={s.branchLoadError}>{branchLoadError}</Text>
          <TouchableOpacity onPress={loadBranches} style={s.retryBtn} testID="branch-retry">
            <Text style={s.retryText}>Retry</Text>
          </TouchableOpacity>
        </View>
      )}
    </>
  );

  const UserRow = ({ item }: { item: BranchUser }) => {
    const active = selectedUser?.id === item.id;
    return (
      <TouchableOpacity
        style={[s.userRow, active && s.userRowActive]}
        onPress={() => {
          setSelectedUser(item);
          setPin("");
          setError("");
        }}
        testID={`user-${item.role}`}
      >
        <View style={s.avatar}>
          <Ionicons name="person-outline" size={20} color="#475569" />
        </View>
        <Text style={s.userName}>{item.name}</Text>
        <Ionicons name="chevron-forward" size={18} color="#94A3B8" />
      </TouchableOpacity>
    );
  };

  const UserList = () => (
    <>
      <Text style={s.staffHeading}>รายชื่อพนักงาน</Text>
      {usersLoading ? (
        <View style={{ paddingVertical: 24 }}>
          <ActivityIndicator color="#00B14F" />
        </View>
      ) : isWide ? (
        <FlatList
          data={users}
          keyExtractor={(u) => u.id}
          renderItem={({ item }) => <UserRow item={item} />}
          ListEmptyComponent={
            <Text style={s.empty}>No users assigned to this branch yet.</Text>
          }
        />
      ) : users.length === 0 ? (
        <Text style={s.empty}>No users assigned to this branch yet.</Text>
      ) : (
        users.map((u) => <UserRow key={u.id} item={u} />)
      )}
    </>
  );

  const keySize = isWide || !isShort ? 72 : 62;
  const keypadWidth = keySize * 3 + 24;
  const PinPad = () => (
    <View style={s.pinPad}>
      <Text style={s.rightTitle}>เข้า ใช้งาน</Text>
      <Text style={s.rightSubtitle} numberOfLines={1}>
        {selectedUser ? selectedUser.name : "เลือกพนักงาน"}
      </Text>
      <View style={s.dotsRow}>
        {Array.from({ length: PIN_LENGTH }).map((_, i) => (
          <View
            key={i}
            style={[s.dot, i < pin.length && s.dotFilled]}
            testID={`pin-dot-${i}`}
          />
        ))}
      </View>
      {error ? (
        <Text style={s.error} testID="login-error">{error}</Text>
      ) : (
        <View style={{ height: 18 }} />
      )}
      <View style={[s.keypad, { width: keypadWidth }]}>
        {["1", "2", "3", "4", "5", "6", "7", "8", "9"].map((d) => (
          <KeyButton
            key={d}
            label={d}
            size={keySize}
            onPress={() => onDigit(d)}
            disabled={submitting || !selectedUser}
          />
        ))}
        <View style={{ width: keySize, height: keySize }} />
        <KeyButton
          label="0"
          size={keySize}
          onPress={() => onDigit("0")}
          disabled={submitting || !selectedUser}
        />
        <KeyButton
          icon="backspace-outline"
          size={keySize}
          onPress={onBackspace}
          disabled={submitting || pin.length === 0}
        />
      </View>
      {submitting && (
        <View style={{ marginTop: 12 }}>
          <ActivityIndicator color="#00B14F" />
        </View>
      )}
    </View>
  );

  const BranchPickerModal = () => (
    <Modal
      visible={showBranchPicker}
      transparent
      animationType="fade"
      onRequestClose={() => setShowBranchPicker(false)}
    >
      <TouchableOpacity
        style={s.branchModalOverlay}
        activeOpacity={1}
        onPress={() => setShowBranchPicker(false)}
      >
        <View style={s.branchModalSheet}>
          <Text style={s.branchModalTitle}>Choose Branch</Text>
          <FlatList
            data={branches}
            keyExtractor={(b) => b.id}
            renderItem={({ item }) => (
              <TouchableOpacity
                style={[s.branchRow, branch?.id === item.id && s.branchRowActive]}
                onPress={() => {
                  setBranch(item);
                  setShowBranchPicker(false);
                }}
                testID={`branch-pick-${item.id}`}
              >
                <Ionicons name="storefront-outline" size={18} color="#0F172A" />
                <Text style={s.branchRowName}>{item.name}</Text>
                {branch?.id === item.id && (
                  <Ionicons name="checkmark-circle" size={18} color="#10B981" />
                )}
              </TouchableOpacity>
            )}
          />
        </View>
      </TouchableOpacity>
    </Modal>
  );

  // ── Tablet: side-by-side ───────────────────────────────────────────────
  if (isWide) {
    return (
      <SafeAreaView style={s.container} testID="login-screen">
        <View style={s.tabletLayout}>
          <View style={s.tabletLeft}>
            <Text style={s.clock}>{formatClock(now)}</Text>
            <Text style={s.date}>{formatThaiDate(now)}</Text>
            <BranchButton />
            <UserList />
          </View>
          <View style={s.tabletRight}>
            <PinPad />
          </View>
        </View>
        <BranchPickerModal />
      </SafeAreaView>
    );
  }

  // ── Phone: two-state flow (user picker → PIN pad) ──────────────────────
  return (
    <SafeAreaView
      style={s.container}
      edges={["top", "left", "right"]}
      testID="login-screen"
    >
      <View
        style={[
          s.phoneRoot,
          { paddingBottom: Math.max(insets.bottom, 12) + 8 },
        ]}
      >
        {!selectedUser ? (
          // State 1: pick a user
          <View style={{ flex: 1 }}>
            <Text style={[s.clock, isShort && { fontSize: 44 }]}>{formatClock(now)}</Text>
            <Text style={s.date}>{formatThaiDate(now)}</Text>
            <View style={{ marginTop: 12 }}>
              <BranchButton />
            </View>
            <View style={{ flex: 1 }}>
              <UserList />
            </View>
          </View>
        ) : (
          // State 2: enter PIN
          <View style={{ flex: 1 }}>
            <View style={s.phonePinHeader}>
              <TouchableOpacity
                style={s.backBtn}
                onPress={() => {
                  setSelectedUser(null);
                  setPin("");
                  setError("");
                }}
                testID="pin-back"
              >
                <Ionicons name="chevron-back" size={18} color="#0F172A" />
                <Text style={s.backText}>เปลี่ยนพนักงาน</Text>
              </TouchableOpacity>
              {!!branch && (
                <View style={s.phoneBranchTag}>
                  <Ionicons name="storefront-outline" size={12} color="#00B14F" />
                  <Text style={s.phoneBranchText} numberOfLines={1}>{branch.name}</Text>
                </View>
              )}
            </View>
            <View style={{ flex: 1, justifyContent: "center" }}>
              <PinPad />
            </View>
          </View>
        )}
      </View>
      <BranchPickerModal />
    </SafeAreaView>
  );
}

function KeyButton({
  label,
  icon,
  size,
  onPress,
  disabled,
}: {
  label?: string;
  icon?: any;
  size: number;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <TouchableOpacity
      style={[
        s.key,
        { width: size, height: size, borderRadius: size / 2 },
        disabled && { opacity: 0.4 },
      ]}
      onPress={onPress}
      disabled={disabled}
      testID={`key-${label ?? "backspace"}`}
    >
      {icon ? (
        <Ionicons name={icon} size={Math.round(size * 0.36)} color="#0F172A" />
      ) : (
        <Text style={[s.keyLabel, { fontSize: Math.round(size * 0.36) }]}>{label}</Text>
      )}
    </TouchableOpacity>
  );
}

const s = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#F8FAFC" },

  // ── Tablet layout ──
  tabletLayout: { flex: 1, flexDirection: "row" },
  tabletLeft: { flex: 1.1, padding: 32 },
  tabletRight: {
    flex: 1,
    padding: 32,
    alignItems: "center",
    justifyContent: "center",
    borderLeftWidth: 1,
    borderLeftColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
  },

  // ── Phone layout ──
  phoneRoot: { flex: 1, paddingHorizontal: 24, paddingTop: 16 },
  phonePinHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 8,
  },
  backBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingVertical: 6,
    paddingRight: 8,
  },
  backText: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  phoneBranchTag: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
    maxWidth: 160,
  },
  phoneBranchText: { fontSize: 12, fontWeight: "600", color: "#0F172A" },

  // ── Header / branch ──
  clock: { fontSize: 56, fontWeight: "700", color: "#0F172A", letterSpacing: -1 },
  date: { fontSize: 16, color: "#475569", marginTop: 4, marginBottom: 12 },
  branchBtn: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 999,
    borderWidth: 1, borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF", alignSelf: "flex-start",
    marginBottom: 12, maxWidth: "100%",
  },
  branchLabel: { fontSize: 13, fontWeight: "600", color: "#0F172A" },
  branchLoadError: { color: "#EF4444", fontSize: 11, marginBottom: 6 },
  retryBtn: {
    paddingHorizontal: 14, paddingVertical: 6, borderRadius: 999,
    borderWidth: 1, borderColor: "#EF4444", alignSelf: "flex-start",
  },
  retryText: { color: "#EF4444", fontSize: 12, fontWeight: "600" },

  // ── User list ──
  staffHeading: {
    fontSize: 13, fontWeight: "600", color: "#64748B",
    marginBottom: 8, marginTop: 8,
  },
  userRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingHorizontal: 14, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#E2E8F0",
  },
  userRowActive: { backgroundColor: "#F0FDF4" },
  avatar: {
    width: 36, height: 36, borderRadius: 18,
    backgroundColor: "#F1F5F9",
    alignItems: "center", justifyContent: "center",
  },
  userName: { flex: 1, fontSize: 16, color: "#0F172A", fontWeight: "500" },
  empty: { padding: 16, color: "#94A3B8", textAlign: "center" },

  // ── PIN pad ──
  pinPad: { alignItems: "center" },
  rightTitle: { fontSize: 22, fontWeight: "700", color: "#0F172A" },
  rightSubtitle: {
    fontSize: 14, color: "#64748B",
    marginTop: 4, marginBottom: 20,
    maxWidth: 280, textAlign: "center",
  },
  dotsRow: { flexDirection: "row", gap: 14, marginBottom: 4 },
  dot: {
    width: 14, height: 14, borderRadius: 7,
    borderWidth: 1.5, borderColor: "#CBD5E1", backgroundColor: "transparent",
  },
  dotFilled: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  error: { color: "#EF4444", fontSize: 13, marginTop: 4, marginBottom: 4, textAlign: "center" },
  keypad: {
    flexDirection: "row", flexWrap: "wrap",
    justifyContent: "space-between",
    marginTop: 4,
  },
  key: {
    alignItems: "center", justifyContent: "center",
    marginVertical: 4,
    backgroundColor: "#F8FAFC",
    borderWidth: 1, borderColor: "#E2E8F0",
  },
  keyLabel: { fontWeight: "500", color: "#0F172A" },

  // ── Branch picker modal ──
  branchModalOverlay: {
    flex: 1, backgroundColor: "rgba(15,23,42,0.45)",
    justifyContent: "center", padding: 24,
  },
  branchModalSheet: {
    backgroundColor: "#FFFFFF", borderRadius: 16,
    maxHeight: "70%", overflow: "hidden",
    maxWidth: 420, alignSelf: "center", width: "100%",
  },
  branchModalTitle: {
    fontSize: 15, fontWeight: "700", color: "#0F172A", padding: 16,
    borderBottomWidth: 1, borderBottomColor: "#F1F5F9",
  },
  branchRow: {
    flexDirection: "row", alignItems: "center", gap: 12,
    paddingHorizontal: 16, paddingVertical: 14,
    borderBottomWidth: 1, borderBottomColor: "#F8FAFC",
  },
  branchRowActive: { backgroundColor: "#F0FDF4" },
  branchRowName: { flex: 1, fontSize: 14, color: "#0F172A", fontWeight: "500" },
});
