/**
 * Login screen — email + password, with branch picker.
 *
 * Auth flow:
 *   1. Fetch active branches; user picks one (default: first / last-used).
 *   2. User enters email + password.
 *   3. POST /api/auth/login → backend deletes any existing BranchSession
 *      for this branch and creates a fresh one.  Returns a token + role.
 *   4. We save { token, role, staff_name, branch_id, branch_name } to
 *      AsyncStorage and route to /admin or /pos based on role.
 *
 * "One login per branch" is enforced server-side: the previous user's
 * token simply stops working on the next request.
 */
import { useEffect, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  TextInput,
  TouchableOpacity,
  ActivityIndicator,
  FlatList,
  Modal,
  KeyboardAvoidingView,
  Platform,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { setAuthToken } from "../lib/api";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;
const BRANCH_KEY = "bravepos:selected-branch:v1";
const AUTH_KEY = "bravepos:auth:v1";

type Branch = { id: string; name: string; code?: string; active: boolean };

export default function Login() {
  const router = useRouter();
  const { width } = useWindowDimensions();
  const isWide = width >= 700;

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [branches, setBranches] = useState<Branch[]>([]);
  const [branch, setBranch] = useState<Branch | null>(null);
  const [showBranchPicker, setShowBranchPicker] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const [r, savedBranchRaw] = await Promise.all([
          fetch(`${API}/branches?active=true`),
          AsyncStorage.getItem(BRANCH_KEY),
        ]);
        const list: Branch[] = r.ok ? await r.json() : [];
        setBranches(list);
        const saved = savedBranchRaw ? (JSON.parse(savedBranchRaw) as Branch) : null;
        const initial =
          (saved && list.find((b) => b.id === saved.id)) || list[0] || null;
        setBranch(initial);
      } catch {
        // backend unreachable — login form still appears so the user gets
        // a clearer "Network error" when they actually submit
      }
    })();
  }, []);

  const submit = async () => {
    if (loading) return;
    setError("");
    if (!email.trim() || !password) {
      setError("Email and password are required.");
      return;
    }
    if (!branch) {
      setError("Please pick a branch first.");
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim().toLowerCase(),
          password,
          branch_id: branch.id,
        }),
      });
      const body = await res.json().catch(() => ({} as any));
      if (!res.ok) {
        setError(body?.detail || "Invalid credentials.");
        setPassword("");
        return;
      }

      // Persist session locally so future API calls can send the Bearer token
      // and so a reopened app remembers who's logged in.
      await AsyncStorage.setItem(
        AUTH_KEY,
        JSON.stringify({
          token: body.token,
          staff: body.staff,
          branch: body.branch,
        }),
      );
      await AsyncStorage.setItem(BRANCH_KEY, JSON.stringify(branch));
      // Prime the in-memory token cache so the next API call has it instantly
      // (instead of waiting for AsyncStorage to be read on first use).
      setAuthToken(body.token);

      const role = body.staff?.role || "cashier";
      const destination = role === "admin" ? "/admin" : "/pos";
      router.replace({
        pathname: destination,
        params: {
          staff: body.staff?.name || "",
          role,
          branch_id: branch.id,
          branch_name: branch.name,
        },
      });
    } catch {
      setError("Network error. Please retry.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <SafeAreaView
      style={[styles.container, !isWide && styles.containerNarrow]}
      testID="login-screen"
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
            <Text style={styles.hintLabel}>Demo accounts</Text>
            <Text style={styles.hintText}>admin@rollingpinn.com · admin1234</Text>
            <Text style={styles.hintText}>cashier@rollingpinn.com · cashier1234</Text>
          </View>
        )}
      </View>

      <KeyboardAvoidingView
        style={[styles.right, !isWide && styles.rightNarrow]}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <Text style={styles.title}>Sign in</Text>
        <Text style={styles.subtitle}>Staff login required</Text>

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

        <View style={styles.field}>
          <Ionicons name="mail-outline" size={18} color="#94A3B8" style={{ marginRight: 8 }} />
          <TextInput
            style={styles.fieldInput}
            value={email}
            onChangeText={setEmail}
            placeholder="Email"
            placeholderTextColor="#94A3B8"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="email-address"
            testID="login-email"
          />
        </View>

        <View style={styles.field}>
          <Ionicons name="lock-closed-outline" size={18} color="#94A3B8" style={{ marginRight: 8 }} />
          <TextInput
            style={styles.fieldInput}
            value={password}
            onChangeText={setPassword}
            placeholder="Password"
            placeholderTextColor="#94A3B8"
            secureTextEntry={!showPassword}
            onSubmitEditing={submit}
            testID="login-password"
          />
          <TouchableOpacity onPress={() => setShowPassword((s) => !s)}>
            <Ionicons
              name={showPassword ? "eye-off-outline" : "eye-outline"}
              size={18}
              color="#94A3B8"
            />
          </TouchableOpacity>
        </View>

        {error ? <Text style={styles.error}>{error}</Text> : <View style={{ height: 18 }} />}

        <TouchableOpacity
          style={[styles.submitBtn, (loading || !email.trim() || !password) && { opacity: 0.5 }]}
          onPress={submit}
          disabled={loading || !email.trim() || !password}
          testID="login-submit"
        >
          {loading ? (
            <ActivityIndicator color="#FFFFFF" />
          ) : (
            <Text style={styles.submitBtnText}>Sign in</Text>
          )}
        </TouchableOpacity>

        {!isWide && (
          <View style={styles.hintBoxMobile}>
            <Text style={styles.hintLabelMobile}>Demo</Text>
            <Text style={styles.hintTextMobile}>admin@rollingpinn.com / admin1234</Text>
            <Text style={styles.hintTextMobile}>cashier@rollingpinn.com / cashier1234</Text>
          </View>
        )}
      </KeyboardAvoidingView>

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
    width: 96, height: 96, borderRadius: 48,
    backgroundColor: "#FFFFFF", alignItems: "center", justifyContent: "center",
    marginBottom: 20,
  },
  logoCircleNarrow: { width: 72, height: 72, borderRadius: 36, marginBottom: 12 },
  brand: { fontSize: 48, fontWeight: "700", color: "#FFFFFF", letterSpacing: -1 },
  brandNarrow: { fontSize: 32 },
  brandSub: {
    fontSize: 14, color: "#E5F7ED", marginTop: 4,
    letterSpacing: 2, textTransform: "uppercase",
  },
  hintBox: {
    backgroundColor: "rgba(255,255,255,0.12)", padding: 16,
    borderRadius: 12, alignSelf: "flex-start",
  },
  hintLabel: {
    color: "#E5F7ED", fontSize: 11, fontWeight: "600",
    letterSpacing: 1.5, textTransform: "uppercase", marginBottom: 6,
  },
  hintText: { color: "#FFFFFF", fontSize: 13, fontFamily: "monospace" },

  right: {
    flex: 1, padding: 48, alignItems: "center", justifyContent: "center",
  },
  rightNarrow: { padding: 24 },
  title: { fontSize: 28, fontWeight: "700", color: "#0F172A" },
  subtitle: { fontSize: 14, color: "#94A3B8", marginTop: 6, marginBottom: 24 },

  branchBtn: {
    flexDirection: "row", alignItems: "center", gap: 8,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 999,
    borderWidth: 1, borderColor: "#E2E8F0",
    backgroundColor: "#FFFFFF", marginBottom: 16,
  },
  branchLabel: { fontSize: 13, fontWeight: "600", color: "#0F172A" },

  field: {
    flexDirection: "row", alignItems: "center",
    width: "100%", maxWidth: 360,
    borderWidth: 1, borderColor: "#E2E8F0", borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 4,
    backgroundColor: "#FFFFFF", marginBottom: 12,
  },
  fieldInput: { flex: 1, paddingVertical: 12, fontSize: 14, color: "#0F172A" },

  error: { color: "#EF4444", fontSize: 13, marginBottom: 6, textAlign: "center" },

  submitBtn: {
    width: "100%", maxWidth: 360,
    backgroundColor: "#00B14F", paddingVertical: 14, borderRadius: 10,
    alignItems: "center", marginTop: 4,
  },
  submitBtnText: { color: "#FFFFFF", fontSize: 15, fontWeight: "700" },

  hintBoxMobile: {
    marginTop: 28, paddingHorizontal: 16, paddingVertical: 12,
    borderRadius: 10, backgroundColor: "#F8FAFC", alignItems: "center",
  },
  hintLabelMobile: {
    color: "#94A3B8", fontSize: 10, fontWeight: "700",
    letterSpacing: 1.5, textTransform: "uppercase", marginBottom: 4,
  },
  hintTextMobile: { fontSize: 11, color: "#475569", fontFamily: "monospace" },

  // Branch picker modal
  branchModalOverlay: {
    flex: 1, backgroundColor: "rgba(15,23,42,0.45)",
    justifyContent: "center", padding: 24,
  },
  branchModalSheet: {
    backgroundColor: "#FFFFFF", borderRadius: 16,
    maxHeight: "70%", overflow: "hidden",
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
