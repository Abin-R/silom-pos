/**
 * Login screen — PIN-pad style:
 *   1. Fetch active branches; user picks one (default: first / last-used).
 *   2. Fetch the staff list for that branch (admin + the branch's cashier).
 *   3. Tap a user, enter a 4-digit PIN on the keypad → auto-submits.
 *   4. POST /api/auth/pin-login → backend replaces any existing
 *      BranchSession on this branch and returns a token + role.
 *   5. Both roles land on /pos; admin reaches admin screens via the rail.
 *
 * Layout: this is the one screen with no navigation, so the rail's navy takes
 * the whole left panel and carries the identity. The clock is the largest
 * element on it because that is what someone opening a shift actually checks.
 *
 * On phone the navy becomes a header band and the two halves stack: name
 * first, then PIN, so the keypad is never squeezed.
 */
import { useEffect, useState } from "react";
import {
  View,
  Text,
  Image,
  StyleSheet,
  TouchableOpacity,
  ActivityIndicator,
  FlatList,
  Modal,
  ScrollView,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { setAuthToken, setSentryContext } from "../lib/api";
import { C, MONO, R } from "../lib/theme";
import { Btn, Tag } from "../lib/ui";
import { t as tr, useT, formatLongDate, formatClock } from "../lib/i18n";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const BRANCH_KEY = "bravepos:selected-branch:v1";
const AUTH_KEY = "bravepos:auth:v1";
const PIN_LENGTH = 4;

type Branch = { id: string; name: string; code?: string; active: boolean };
type BranchUser = { id: string; name: string; role: "admin" | "cashier" };

const EN_DAYS = [
  "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];
const EN_MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// The long date follows the UI language (Thai adds the Buddhist year) — see
// formatLongDate in lib/i18n. This English form stays for the second line: it
// exists so a shift-opener can cross-check the till against a delivery note,
// which is printed in English whatever the tablet is set to.
function formatEnDate(d: Date): string {
  return `${EN_DAYS[d.getDay()]} ${d.getDate()} ${EN_MONTHS[d.getMonth()]}`;
}

function initial(name: string): string {
  const t = (name || "").trim();
  return t ? Array.from(t)[0].toUpperCase() : "?";
}

export default function Login() {
  // `lang` gates the English second line below; useT also re-renders on change.
  const { lang } = useT();
  const router = useRouter();
  const { width, height } = useWindowDimensions();
  const isWide = width >= 900;
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
  const [showStaffPicker, setShowStaffPicker] = useState(false);
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
      const initialBranch =
        (saved && list.find((b) => b.id === saved.id)) || list[0] || null;
      setBranch(initialBranch);
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
          setError(body?.detail || tr("login.couldnt_load_users"));
          return;
        }
        setUsers(Array.isArray(body.users) ? body.users : []);
        setSelectedUser(null);
        setPin("");
      } catch (e: any) {
        if (!cancelled) {
          setUsers([]);
          setError(e?.message || tr("login.network_error"));
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
        setError(body?.detail || tr("login.invalid_pin"));
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
      // Tag every later error report with who was on the till and where, so an
      // alert identifies the tablet instead of just saying something broke.
      setSentryContext({
        staffId: body.staff?.id,
        staffName: body.staff?.name,
        role,
        branchName: branch.name,
      });
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
      setError(tr("login.network_error_retry"));
      setPin("");
    } finally {
      setSubmitting(false);
    }
  };

  const onDigit = (d: string) => {
    if (submitting) return;
    setShowStaffPicker(false);
    if (!selectedUser) {
      setError(tr("login.choose_user_first"));
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
      <SafeAreaView style={{ flex: 1, backgroundColor: C.nav }}>
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
          <ActivityIndicator color={C.surface} />
        </View>
      </SafeAreaView>
    );
  }

  // ── Pieces ─────────────────────────────────────────────────────────────

  // The branch chip doubles as the picker — on the navy panel it reads as a
  // fact about this terminal, and taps through when a shop has more than one.
  const BranchChip = () => (
    <View>
      <TouchableOpacity
        style={s.branchChip}
        onPress={() => setShowBranchPicker(true)}
        disabled={branches.length <= 1}
        testID="branch-picker-btn"
      >
        <Ionicons name="location-outline" size={17} color={C.navMuted} />
        <Text style={s.branchChipText} numberOfLines={1}>
          {branch?.name
            || (branchLoading ? tr("login.loading_branches") : branchLoadError ? tr("login.couldnt_load_branches") : tr("login.select_branch"))}
        </Text>
        {branches.length > 1 && (
          <Ionicons name="chevron-down" size={15} color={C.navMuted} />
        )}
      </TouchableOpacity>
      {!!branchLoadError && (
        <View style={{ marginTop: 10, gap: 8, alignItems: "flex-start" }}>
          <Text style={s.branchErr}>{branchLoadError}</Text>
          <Btn
            label={tr("login.retry")}
            height={38}
            onPress={loadBranches}
            style={{
              backgroundColor: "rgba(255,255,255,0.12)",
              borderColor: "rgba(255,255,255,0.3)",
            }}
            textStyle={{ color: C.surface }}
            testID="branch-retry"
          />
        </View>
      )}
    </View>
  );

  // A dropdown should open where it is, not throw a sheet over the middle of
  // the screen. The list drops directly under the control, anchored to it.
  const StaffSelect = () => {
  useT(); // re-render this screen when the language changes
    const open = showStaffPicker;
    const disabled = usersLoading || users.length === 0;
    return (
      <View style={s.selectWrap}>
        <TouchableOpacity
          style={[s.select, open && s.selectOpen, !!selectedUser && !open && s.selectOn]}
          onPress={() => setShowStaffPicker((v) => !v)}
          activeOpacity={0.8}
          disabled={disabled}
          testID="staff-select"
        >
          {selectedUser ? (
            <>
              <View style={[s.sAv, s.sAvOn]}>
                <Text style={[s.sAvText, { color: C.surface }]}>
                  {initial(selectedUser.name)}
                </Text>
              </View>
              <View style={{ flex: 1, minWidth: 0 }}>
                <Text style={s.selectName} numberOfLines={1}>
                  {selectedUser.name}
                </Text>
                <Text style={s.selectRole} numberOfLines={1}>
                  {selectedUser.role === "admin" ? tr("common.admin") : tr("common.cashier")}
                </Text>
              </View>
            </>
          ) : (
            <>
              <View style={s.sAv}>
                <Ionicons name="people-outline" size={19} color={C.ink2Soft} />
              </View>
              <View style={{ flex: 1, minWidth: 0 }}>
                <Text style={s.selectPlaceholder}>
                  {usersLoading
                    ? tr("login.loading_staff")
                    : users.length === 0
                      ? tr("login.no_staff_at_this_branch")
                      : tr("login.select_your_name")}
                </Text>
                {!disabled && (
                  <Text style={s.selectHint}>
                    {`${users.length} on this branch`}
                  </Text>
                )}
              </View>
            </>
          )}
          <Ionicons
            name={open ? "chevron-up" : "chevron-down"}
            size={20}
            color={open ? C.brand : C.ink3}
          />
        </TouchableOpacity>

        {open && (
          <View style={s.popover}>
            <ScrollView
              showsVerticalScrollIndicator={false}
              keyboardShouldPersistTaps="handled"
            >
              {users.map((u, i) => {
                const on = selectedUser?.id === u.id;
                return (
                  <TouchableOpacity
                    key={u.id}
                    style={[
                      s.sRow,
                      on && s.sRowOn,
                      i === users.length - 1 && { borderBottomWidth: 0 },
                    ]}
                    onPress={() => {
                      setSelectedUser(u);
                      setPin("");
                      setError("");
                      setShowStaffPicker(false);
                    }}
                    testID={`user-${u.role}`}
                  >
                    <View style={[s.sAv, on && s.sAvOn]}>
                      <Text style={[s.sAvText, on && { color: C.surface }]}>
                        {initial(u.name)}
                      </Text>
                    </View>
                    <View style={{ flex: 1, minWidth: 0 }}>
                      <Text
                        style={[s.sName, on && { color: C.brand }]}
                        numberOfLines={1}
                      >
                        {u.name}
                      </Text>
                      <Text style={s.sRole} numberOfLines={1}>
                        {u.role === "admin" ? tr("common.admin") : tr("common.cashier")}
                      </Text>
                    </View>
                    {on && <Ionicons name="checkmark" size={19} color={C.brand} />}
                  </TouchableOpacity>
                );
              })}
            </ScrollView>
          </View>
        )}
      </View>
    );
  };

  const keyH = isWide
    ? height < 780
      ? 58
      : height < 900
        ? 68
        : 84
    : isShort
      ? 60
      : 72;
  // Below ~780 the staff grid and the pad are competing for the same pixels,
  // so the cards give up their padding first — the pad has to stay tappable.
  const compact = isWide && height < 780;

  const Pad = () => (
    <View style={[s.pad, compact && { maxWidth: 380 }]}>
      {["1", "2", "3", "4", "5", "6", "7", "8", "9"].map((d) => (
        <Key
          key={d}
          label={d}
          h={keyH}
          onPress={() => onDigit(d)}
          disabled={submitting || !selectedUser}
        />
      ))}
      <Key
        label={tr("login.switch_staff")}
        secondary
        h={keyH}
        onPress={() => {
          setSelectedUser(null);
          setPin("");
          setError("");
        }}
        disabled={submitting || !selectedUser}
      />
      <Key
        label="0"
        h={keyH}
        onPress={() => onDigit("0")}
        disabled={submitting || !selectedUser}
      />
      <Key
        icon="backspace-outline"
        h={keyH}
        onPress={onBackspace}
        disabled={submitting || pin.length === 0}
      />
    </View>
  );

  const Dots = () => (
    <View style={[s.dots, compact && { marginTop: 18, marginBottom: 8 }]}>
      {Array.from({ length: PIN_LENGTH }).map((_, i) => (
        <View
          key={i}
          style={[s.dot, i < pin.length && s.dotOn]}
          testID={`pin-dot-${i}`}
        />
      ))}
    </View>
  );

  const Status = () =>
    error ? (
      <Text style={s.error} testID="login-error">{error}</Text>
    ) : submitting ? (
      <View style={{ height: 20, justifyContent: "center" }}>
        <ActivityIndicator color={C.brand} size="small" />
      </View>
    ) : (
      <View style={{ height: 20 }} />
    );

  const BranchPickerModal = () => (
    <Modal
      visible={showBranchPicker}
      transparent
      animationType="fade"
      onRequestClose={() => setShowBranchPicker(false)}
    >
      <TouchableOpacity
        style={s.modalOverlay}
        activeOpacity={1}
        onPress={() => setShowBranchPicker(false)}
      >
        <View style={s.modalSheet}>
          <Text style={s.modalTitle}>{tr("login.choose_branch")}</Text>
          <FlatList
            data={branches}
            keyExtractor={(b) => b.id}
            renderItem={({ item }) => {
              const on = branch?.id === item.id;
              return (
                <TouchableOpacity
                  style={[s.branchRow, on && { backgroundColor: C.brandTintSoft }]}
                  onPress={() => {
                    setBranch(item);
                    setShowBranchPicker(false);
                  }}
                  testID={`branch-pick-${item.id}`}
                >
                  <Ionicons
                    name="storefront-outline"
                    size={18}
                    color={on ? C.brand : C.ink2}
                  />
                  <Text style={[s.branchRowName, on && { color: C.brand }]}>
                    {item.name}
                  </Text>
                  {on && <Tag tone="info">{tr("login.current")}</Tag>}
                </TouchableOpacity>
              );
            }}
          />
        </View>
      </TouchableOpacity>
    </Modal>
  );

  // ── Tablet: navy identity panel + sign-in half ─────────────────────────
  if (isWide) {
    return (
      <SafeAreaView style={s.root} testID="login-screen">
        <View style={s.split}>
          <View style={s.navyPanel}>
            <View>
              <View style={s.mark}>
                <Image
                  source={require("../assets/images/icon.png")}
                  style={s.markImg}
                  resizeMode="cover"
                />
              </View>
              <Text style={s.brand}>The Rolling Pinn</Text>
              <View style={{ marginTop: 16, alignItems: "flex-start" }}>
                <BranchChip />
              </View>
            </View>

            <View>
              <Text style={s.clock}>{formatClock(now)}</Text>
              <Text style={s.date}>{formatLongDate(now)}</Text>
              {lang === "th" && <Text style={s.dateEn}>{formatEnDate(now)}</Text>}
            </View>
          </View>

          <View style={[s.padCol, compact && { paddingVertical: 24 }]}>
            {showStaffPicker && (
              <TouchableOpacity
                style={s.popoverBackdrop}
                activeOpacity={1}
                onPress={() => setShowStaffPicker(false)}
              />
            )}
            <Text style={[s.h1, compact && { fontSize: 24 }]} numberOfLines={1}>
              {selectedUser ? `Hi, ${selectedUser.name}` : tr("login.whos_on_the_till")}
            </Text>
            <Text style={s.h1sub}>
              {selectedUser
                ? tr("login.pin_hint")
                : tr("login.staff_list_pin_hint")}
            </Text>
            <StaffSelect />
            <Dots />
            <Status />
            <Pad />
          </View>
        </View>
        <BranchPickerModal />
      </SafeAreaView>
    );
  }

  // ── Phone: navy band, then name → PIN in two states ────────────────────
  return (
    <SafeAreaView
      style={s.root}
      edges={["top", "left", "right"]}
      testID="login-screen"
    >
      <View style={s.phoneBand}>
        <View style={s.phoneBandTop}>
          <View style={s.markSm}>
            <Image
              source={require("../assets/images/icon.png")}
              style={s.markImg}
              resizeMode="cover"
            />
          </View>
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text style={s.brandSm} numberOfLines={1}>The Rolling Pinn</Text>
            <Text style={s.dateSm} numberOfLines={1}>{formatLongDate(now)}</Text>
          </View>
          <Text style={s.clockSm}>{formatClock(now)}</Text>
        </View>
        <View style={{ marginTop: 12, alignItems: "flex-start" }}>
          <BranchChip />
        </View>
      </View>

      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={[
          s.phoneBody,
          { paddingBottom: Math.max(insets.bottom, 12) + 16 },
        ]}
        keyboardShouldPersistTaps="handled"
      >
        {!selectedUser ? (
          <>
            <Text style={s.h1}>{tr("login.whos_on_the_till")}</Text>
            <Text style={s.h1sub}>{tr("login.staff_list_hint")}</Text>
            <StaffSelect />
          </>
        ) : (
          <>
            <TouchableOpacity
              style={s.backRow}
              onPress={() => {
                setSelectedUser(null);
                setPin("");
                setError("");
              }}
              testID="pin-back"
            >
              <Ionicons name="chevron-back" size={18} color={C.ink2} />
              <Text style={s.backText}>{tr("login.switch_staff_hint")}</Text>
            </TouchableOpacity>
            <Text style={s.h1}>{selectedUser.name}</Text>
            <Text style={s.h1sub}>{tr("login.enter_your_4_digit_pin")}</Text>
            <Dots />
            <Status />
            <Pad />
          </>
        )}
      </ScrollView>
      <BranchPickerModal />
    </SafeAreaView>
  );
}

function Key({
  label,
  icon,
  h,
  secondary,
  onPress,
  disabled,
}: {
  label?: string;
  icon?: any;
  h: number;
  secondary?: boolean;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <View style={s.keySlot}>
      <TouchableOpacity
        style={[
          s.key,
          { height: h },
          secondary && s.keySec,
          disabled && { opacity: 0.4 },
        ]}
        onPress={onPress}
        disabled={disabled}
        activeOpacity={0.7}
        testID={`key-${label ?? "backspace"}`}
      >
        {icon ? (
          <Ionicons name={icon} size={26} color={C.ink2} />
        ) : (
          <Text
            style={secondary ? s.keySecText : [s.keyText, { fontSize: Math.round(h * 0.36) }]}
            numberOfLines={1}
          >
            {label}
          </Text>
        )}
      </TouchableOpacity>
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.surface },
  split: { flex: 1, flexDirection: "row" },

  // ── Navy identity panel ──
  navyPanel: {
    width: 430,
    maxWidth: "40%",
    backgroundColor: C.nav,
    paddingHorizontal: 36,
    paddingVertical: 44,
    justifyContent: "space-between",
  },
  mark: {
    width: 64,
    height: 64,
    borderRadius: 17,
    overflow: "hidden",
    backgroundColor: C.brand,
  },
  markImg: { width: "100%", height: "100%" },
  brand: {
    fontSize: 31,
    fontWeight: "800",
    color: C.surface,
    letterSpacing: -1,
    marginTop: 26,
  },
  branchChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 9,
    backgroundColor: "rgba(255,255,255,0.11)",
    borderRadius: 11,
    paddingHorizontal: 16,
    paddingVertical: 11,
    maxWidth: "100%",
  },
  branchChipText: {
    fontSize: 15,
    color: "#C9D6EE",
    fontWeight: "600",
    flexShrink: 1,
  },
  branchErr: { color: "#FFC9C9", fontSize: 12, lineHeight: 17 },
  clock: {
    ...MONO,
    fontSize: 80,
    fontWeight: "300",
    color: C.surface,
    letterSpacing: -3.4,
    lineHeight: 84,
  },
  date: { fontSize: 16, color: C.navMuted, marginTop: 14 },
  dateEn: { fontSize: 14, color: "rgba(159,178,214,0.7)", marginTop: 3 },

  // ── Sign-in half ──
  signPanel: { flex: 1, minWidth: 0, backgroundColor: C.surface },
  // Centred while it fits, scrollable the moment it doesn't — with five staff
  // the fixed-height column used to push the heading off the top and cut the
  // bottom row of the keypad.
  signPanelInner: {
    flexGrow: 1,
    justifyContent: "center",
    paddingHorizontal: 48,
    paddingVertical: 44,
  },
  h1: { fontSize: 28, fontWeight: "800", color: C.ink, letterSpacing: -0.78 },
  h1sub: { fontSize: 15, color: C.ink2Soft, marginTop: 7, lineHeight: 21 },

  // ── Staff select ──
  // The wrapper owns the stacking context so the popover paints over the dots
  // and keypad below it rather than being clipped by them.
  selectWrap: { marginTop: 24, zIndex: 20 },
  select: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 14,
    paddingVertical: 12,
    borderRadius: R.card,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
  },
  selectOn: {
    borderWidth: 2,
    borderColor: C.brand,
    backgroundColor: C.brandTintSoft,
    paddingHorizontal: 13,
    paddingVertical: 11,
  },
  // Open: square off the bottom corners so the control and the list read as
  // one object rather than two stacked cards.
  selectOpen: {
    borderWidth: 2,
    borderColor: C.brand,
    borderBottomLeftRadius: 0,
    borderBottomRightRadius: 0,
    paddingHorizontal: 13,
    paddingVertical: 11,
  },
  selectName: { fontSize: 16, fontWeight: "700", color: C.ink },
  selectRole: { fontSize: 12.5, color: C.ink2Soft, marginTop: 2 },
  selectPlaceholder: { fontSize: 16, fontWeight: "600", color: C.ink3 },
  selectHint: { fontSize: 12, color: C.ink3, marginTop: 2 },

  popover: {
    position: "absolute",
    top: "100%",
    left: 0,
    right: 0,
    marginTop: -2,
    maxHeight: 250,
    backgroundColor: C.surface,
    borderWidth: 2,
    borderTopWidth: 0,
    borderColor: C.brand,
    borderBottomLeftRadius: R.card,
    borderBottomRightRadius: R.card,
    overflow: "hidden",
    shadowColor: "#0B2050",
    shadowOffset: { width: 0, height: 12 },
    shadowOpacity: 0.14,
    shadowRadius: 24,
    elevation: 10,
  },
  sRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 14,
    paddingVertical: 11,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  sRowOn: { backgroundColor: C.brandTintSoft },
  sAv: {
    width: 38,
    height: 38,
    borderRadius: 19,
    backgroundColor: "#E8EDF5",
    alignItems: "center",
    justifyContent: "center",
  },
  sAvOn: { backgroundColor: C.brand },
  sAvText: { fontSize: 15, fontWeight: "700", color: C.ink2 },
  sName: { fontSize: 15, fontWeight: "700", color: C.ink },
  sRole: { fontSize: 12, color: C.ink2Soft, marginTop: 1 },

  // ── Keypad column ──
  popoverBackdrop: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    zIndex: 10,
  },
  padCol: {
    flex: 1,
    minWidth: 0,
    paddingHorizontal: 56,
    paddingVertical: 40,
    justifyContent: "center",
    backgroundColor: C.surface,
  },

  emptyStaff: {
    marginTop: 24,
    padding: 20,
    borderRadius: R.control,
    backgroundColor: C.sunk,
    color: C.ink2Soft,
    fontSize: 14,
    textAlign: "center",
  },

  dots: {
    flexDirection: "row",
    gap: 16,
    justifyContent: "center",
    marginTop: 30,
    marginBottom: 14,
  },
  dot: {
    width: 17,
    height: 17,
    borderRadius: 9,
    borderWidth: 2,
    borderColor: C.line,
  },
  dotOn: { backgroundColor: C.brand, borderColor: C.brand },
  error: {
    color: C.danger,
    fontSize: 13.5,
    textAlign: "center",
    height: 20,
  },

  pad: {
    flexDirection: "row",
    flexWrap: "wrap",
    maxWidth: 440,
    width: "100%",
    alignSelf: "center",
    marginTop: 12,
    marginHorizontal: -6,
  },
  keySlot: { width: "33.333%", padding: 6 },
  key: {
    borderRadius: R.card,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
    alignItems: "center",
    justifyContent: "center",
  },
  keySec: { borderColor: "transparent", backgroundColor: "transparent" },
  keyText: { ...MONO, fontWeight: "500", color: C.ink },
  keySecText: { fontSize: 14, fontWeight: "700", color: C.ink2Soft },

  // ── Phone ──
  phoneBand: {
    backgroundColor: C.nav,
    paddingHorizontal: 20,
    paddingTop: 16,
    paddingBottom: 18,
  },
  phoneBandTop: { flexDirection: "row", alignItems: "center", gap: 12 },
  markSm: {
    width: 44,
    height: 44,
    borderRadius: 12,
    overflow: "hidden",
    backgroundColor: C.brand,
  },
  brandSm: { fontSize: 17, fontWeight: "800", color: C.surface, letterSpacing: -0.4 },
  dateSm: { fontSize: 12, color: C.navMuted, marginTop: 2 },
  clockSm: { ...MONO, fontSize: 30, fontWeight: "300", color: C.surface, letterSpacing: -1.4 },
  phoneBody: { paddingHorizontal: 20, paddingTop: 22 },
  backRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingVertical: 6,
    marginBottom: 8,
    alignSelf: "flex-start",
  },
  backText: { fontSize: 13.5, fontWeight: "600", color: C.ink2 },

  // ── Branch picker ──
  modalOverlay: {
    flex: 1,
    backgroundColor: C.scrim,
    justifyContent: "center",
    padding: 24,
  },
  modalSheet: {
    backgroundColor: C.surface,
    borderRadius: R.modal,
    maxHeight: "70%",
    overflow: "hidden",
    maxWidth: 440,
    alignSelf: "center",
    width: "100%",
  },
  modalTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: C.ink,
    padding: 20,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  branchRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 20,
    paddingVertical: 16,
    borderBottomWidth: 1,
    borderBottomColor: C.line2,
  },
  branchRowName: { flex: 1, fontSize: 15, color: C.ink, fontWeight: "600" },
});
