import { useState, useEffect } from "react";
import {
  View,
  Text,
  StyleSheet,
  TouchableOpacity,
  ActivityIndicator,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";

const API = `${process.env.EXPO_PUBLIC_BACKEND_URL}/api`;

export default function PinLogin() {
  const router = useRouter();
  const [pin, setPin] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

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
        router.replace({ pathname: "/pos", params: { staff: data.staff_name } });
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
    <SafeAreaView style={styles.container} testID="pin-login-screen">
      <View style={styles.left}>
        <View style={styles.logoBox}>
          <View style={styles.logoCircle}>
            <Ionicons name="storefront" size={48} color="#00B14F" />
          </View>
          <Text style={styles.brand}>BakePOS</Text>
          <Text style={styles.brandSub}>Point of Sale</Text>
        </View>
        <View style={styles.hintBox}>
          <Text style={styles.hintLabel}>Demo PINs</Text>
          <Text style={styles.hintText}>1234 · Admin</Text>
          <Text style={styles.hintText}>0000 · Cashier</Text>
        </View>
      </View>

      <View style={styles.right}>
        <Text style={styles.title}>Enter your PIN</Text>
        <Text style={styles.subtitle}>Staff login required</Text>

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
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    flexDirection: "row",
    backgroundColor: "#FFFFFF",
  },
  left: {
    flex: 1,
    backgroundColor: "#00B14F",
    padding: 48,
    justifyContent: "space-between",
  },
  logoBox: {
    flex: 1,
    justifyContent: "center",
    alignItems: "flex-start",
  },
  logoCircle: {
    width: 96,
    height: 96,
    borderRadius: 48,
    backgroundColor: "#FFFFFF",
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 20,
  },
  brand: {
    fontSize: 48,
    fontWeight: "700",
    color: "#FFFFFF",
    letterSpacing: -1,
  },
  brandSub: {
    fontSize: 18,
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
  right: {
    flex: 1,
    padding: 48,
    alignItems: "center",
    justifyContent: "center",
  },
  title: { fontSize: 28, fontWeight: "700", color: "#0F172A" },
  subtitle: { fontSize: 14, color: "#94A3B8", marginTop: 6, marginBottom: 40 },
  dots: { flexDirection: "row", gap: 20 },
  dot: {
    width: 20,
    height: 20,
    borderRadius: 10,
    borderWidth: 2,
    borderColor: "#CBD5E1",
  },
  dotFilled: { backgroundColor: "#00B14F", borderColor: "#00B14F" },
  error: { color: "#EF4444", fontSize: 13, marginTop: 16, fontWeight: "500" },
  keypad: {
    marginTop: 32,
    width: 320,
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "space-between",
    gap: 12,
  },
  key: {
    width: 96,
    height: 72,
    borderRadius: 16,
    backgroundColor: "#F1F5F9",
    alignItems: "center",
    justifyContent: "center",
  },
  keyEmpty: { width: 96, height: 72 },
  keyText: { fontSize: 28, fontWeight: "600", color: "#0F172A" },
});
