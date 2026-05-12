import { useState, useEffect } from "react";
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
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import AsyncStorage from "@react-native-async-storage/async-storage";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const BRANCH_KEY = "bravepos:selected-branch:v1";

type Branch = { id: string; name: string; code?: string; active: boolean };

export default function PinLogin() {
  const router = useRouter();
  const { width } = useWindowDimensions();
  const isWide = width >= 700;

  const [pin, setPin] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [branches, setBranches] = useState<Branch[]>([]);
  const [branch, setBranch] = useState<Branch | null>(null);
  const [showBranchPicker, setShowBranchPicker] = useState(false);

  // Load active branches once + restore previously selected branch from storage.
  useEffect(() => {
    (async () => {
      try {
        const [r, savedRaw] = await Promise.all([
          fetch(`${API}/branches?active=true`),
          AsyncStorage.getItem(BRANCH_KEY),
        ]);
        const list: Branch[] = r.ok ? await r.json() : [];
        setBranches(list);
        const saved = savedRaw ? (JSON.parse(savedRaw) as Branch) : null;
        const initial =
          (saved && list.find((b) => b.id === saved.id)) || list[0] || null;
        setBranch(initial);
      } catch {
        // offline / unreachable — leave branches empty; PIN entry still works
      }
    })();
  }, []);

  useEffect(() => {
    if (pin.length === 4) {
      verify(pin);
    }
  }, [pin]);

  const verify = async (p: string) => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${API}/auth/verify-pin`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: p }),
      });
      if (res.ok) {
        const data = await res.json();
        // Remember the selected branch for the rest of the session
        if (branch) {
          await AsyncStorage.setItem(BRANCH_KEY, JSON.stringify(branch));
        }
        const staff = data.name || data.staff_name || "";
        const role = data.role || (staff === "Admin" ? "admin" : "cashier");
        const destination = role === "admin" ? "/admin" : "/pos";
        router.replace({
          pathname: destination,
          params: {
            staff,
            role,
            branch_id: branch?.id || "",
            branch_name: branch?.name || "",
          },
        });
      } else {
        setError("Invalid PIN. Try 1234 or 0000.");
        setPin("");
      }
    } catch {
      setError("Network error. Please retry.");
      setPin("");
    } finally {
      setLoading(false);
    }
  };

  const onKey = (k: string) => {
    if (loading) return;
    if (k === "del") setPin((p) => p.slice(0, -1));
    else if (pin.length < 4) setPin((p) => p + k);
  };

  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "del"];

  return (
    <SafeAreaView
      style={[styles.container, !isWide && styles.containerNarrow]}
      testID="pin-login-screen"
    >
      <View style={[styles.left, !isWide && styles.leftNarrow]}>
        <View style={[styles.logoBox, !isWide && styles.logoBoxNarrow]}>
          <View style={[styles.logoCircle, !isWide && styles.logoCircleNarrow]}>
            <Ionicons name="storefront" size={isWide ? 48 : 36} color="#00B14F" />
          </View>
          <Text style={[styles.brand, !isWide && styles.brandNarrow]}>Brave POS</Text>
          <Text style={styles.brandSub}>Point of Sale</Text>
        </View>
        {isWide && (
          <View style={styles.hintBox}>
            <Text style={styles.hintLabel}>Demo PINs</Text>
            <Text style={styles.hintText}>1234 · Admin</Text>
            <Text style={styles.hintText}>0000 · Cashier</Text>
          </View>
        )}
      </View>

      <View style={[styles.right, !isWide && styles.rightNarrow]}>
        <Text style={styles.title}>Enter your PIN</Text>
        <Text style={styles.subtitle}>Staff login required</Text>

        {/* Branch picker — defaults to first available, persists across logins */}
        <TouchableOpacity
          style={styles.branchBtn}
          onPress={() => setShowBranchPicker(true)}
          disabled={branches.length <= 1}
          testID="branch-picker-btn"
        >
          <Ionicons name="storefront-outline" size={16} color="#0F172A" />
          <Text style={styles.branchLabel}>
            {branch?.name || (branches.length === 0 ? "Loading branches…" : "Select branch")}
          </Text>
          {branches.length > 1 && (
            <Ionicons name="chevron-down" size={14} color="#94A3B8" />
          )}
        </TouchableOpacity>

        <View style={styles.dots}>
          {[0, 1, 2, 3].map((i) => (
            <View
              key={i}
              style={[styles.dot, pin.length > i && styles.dotFilled]}
              testID={`pin-dot-${i}`}
            />
          ))}
        </View>

        {error ? <Text style={styles.error}>{error}</Text> : <View style={{ height: 20 }} />}

        <View style={styles.keypad}>
          {keys.map((k, idx) => {
            if (k === "") return <View key={idx} style={styles.keyEmpty} />;
            return (
              <TouchableOpacity
                key={idx}
                style={styles.key}
                onPress={() => onKey(k)}
                activeOpacity={0.7}
                testID={`pin-key-${k}`}
              >
                {k === "del" ? (
                  <Ionicons name="backspace-outline" size={26} color="#0F172A" />
                ) : (
                  <Text style={styles.keyText}>{k}</Text>
                )}
              </TouchableOpacity>
            );
          })}
        </View>

        {loading && <ActivityIndicator color="#00B14F" style={{ marginTop: 12 }} />}

        {!isWide && (
          <View style={styles.hintBoxMobile}>
            <Text style={styles.hintLabelMobile}>Demo PINs</Text>
            <Text style={styles.hintTextMobile}>1234 · Admin  ·  0000 · Cashier</Text>
          </View>
        )}
      </View>

      {/* Branch picker modal */}
      <Modal
        visible={showBranchPicker}
        transparent
        animationType="fade"
        onRequestClose={() => setShowBranchPicker(false)}
      >
        <TouchableOpacity
          style={styles.branchModalOverlay}
          activeOpacity={1}
          onPress={() => setShowBranchPicker(false)}
        >
          <View style={styles.branchModalSheet}>
            <Text style={styles.branchModalTitle}>Choose Branch</Text>
            <FlatList
              data={branches}
              keyExtractor={(b) => b.id}
              renderItem={({ item }) => (
                <TouchableOpacity
                  style={[
                    styles.branchRow,
                    branch?.id === item.id && styles.branchRowActive,
                  ]}
                  onPress={() => {
                    setBranch(item);
                    setShowBranchPicker(false);
                  }}
                  testID={`branch-pick-${item.id}`}
                >
                  <Ionicons name="storefront-outline" size={18} color="#0F172A" />
                  <Text style={styles.branchRowName}>{item.name}</Text>
                  {branch?.id === item.id && (
                    <Ionicons name="checkmark-circle" size={18} color="#10B981" />
                  )}
                </TouchableOpacity>
              )}
            />
          </View>
        </TouchableOpacity>
      </Modal>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, flexDirection: "row", backgroundColor: "#FFFFFF" },
  containerNarrow: { flexDirection: "column" },
  left: {
    flex: 1,
    backgroundColor: "#00B14F",
    padding: 48,
    justifyContent: "space-between",
  },
  leftNarrow: {
    flex: 0,
    padding: 32,
    paddingTop: 48,
    paddingBottom: 32,
    alignItems: "center",
  },
  logoBox: { flex: 1, justifyContent: "center", alignItems: "flex-start" },
  logoBoxNarrow: { flex: 0, alignItems: "center" },
  logoCircle: {
    width: 96,
    height: 96,
    borderRadius: 48,
    backgroundColor: "#FFFFFF",
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 20,
  },
  logoCircleNarrow: { width: 72, height: 72, borderRadius: 36, marginBottom: 12 },
  brand: { fontSize: 48, fontWeight: "700", color: "#FFFFFF", letterSpacing: -1 },
  brandNarrow: { fontSize: 32 },
  brandSub: {
    fontSize: 14,
    color: "#E5F7ED",
    marginTop: 4,
    letterSpacing: 2,
    textTransform: "uppercase",
  },
  hintBox: {
    backgroundColor: "rgba(255,255,255,0.12)",
    padding: 16,
    borderRadius: 12,
    alignSelf: "flex-start",
  },
  hintLabel: {
    color: "#E5F7ED",
    fontSize: 11,
    fontWeight: "600",
    letterSpacing: 1.5,
    textTransform: "uppercase",
    marginBottom: 6,
  },
  hintText: { color: "#FFFFFF", fontSize: 14, fontFamily: "monospace" },
  right: { flex: 1, padding: 48, alignItems: "center", justifyContent: "center" },
  rightNarrow: { flex: 1, padding: 24, paddingTop: 32 },
  title: { fontSize: 26, fontWeight: "700", color: "#0F172A" },
  subtitle: { fontSize: 14, color: "#94A3B8", marginTop: 6, marginBottom: 32 },
  dots: { flexDirection: "row", gap: 20 },
  dot: {
    width: 18,
    height: 18,
    borderRadius: 9,
    borderWidth: 2,
    borderColor: "#CBD5E1",
  },
  dotFilled: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  error: { color: "#EF4444", fontSize: 13, marginTop: 16, fontWeight: "500" },
  keypad: {
    marginTop: 28,
    width: 280,
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "space-between",
    gap: 12,
  },
  key: {
    width: 80,
    height: 64,
    borderRadius: 14,
    backgroundColor: "#F1F5F9",
    alignItems: "center",
    justifyContent: "center",
  },
  keyEmpty: { width: 80, height: 64 },
  keyText: { fontSize: 26, fontWeight: "600", color: "#0F172A" },
  hintBoxMobile: {
    marginTop: 24,
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderRadius: 10,
    backgroundColor: "#F1F5F9",
    alignItems: "center",
  },
  hintLabelMobile: {
    fontSize: 10,
    color: "#94A3B8",
    fontWeight: "700",
    letterSpacing: 1.2,
    textTransform: "uppercase",
    marginBottom: 4,
  },
  hintTextMobile: { fontSize: 12, color: "#475569", fontFamily: "monospace" },

  // Branch picker on the PIN screen
  branchBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF",
    marginTop: 14,
    marginBottom: 6,
  },
  branchLabel: { fontSize: 13, fontWeight: "600", color: "#0F172A" },

  // Branch picker modal
  branchModalOverlay: {
    flex: 1,
    backgroundColor: "rgba(15,23,42,0.45)",
    justifyContent: "center",
    padding: 24,
  },
  branchModalSheet: {
    backgroundColor: "#FFFFFF",
    borderRadius: 16,
    maxHeight: "70%",
    overflow: "hidden",
  },
  branchModalTitle: {
    fontSize: 15,
    fontWeight: "700",
    color: "#0F172A",
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: "#F1F5F9",
  },
  branchRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 16,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: "#F8FAFC",
  },
  branchRowActive: { backgroundColor: "#F0FDF4" },
  branchRowName: { flex: 1, fontSize: 14, color: "#0F172A", fontWeight: "500" },
});
