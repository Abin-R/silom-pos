import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { useFonts } from "expo-font";
import { Text, TextInput } from "react-native";

// Set Sarabun as the default font for all Text and TextInput components
// so Thai characters render correctly on web and native.
(Text as any).defaultProps = (Text as any).defaultProps || {};
(Text as any).defaultProps.style = { fontFamily: "Sarabun" };
(TextInput as any).defaultProps = (TextInput as any).defaultProps || {};
(TextInput as any).defaultProps.style = { fontFamily: "Sarabun" };

export default function RootLayout() {
  const [fontsLoaded] = useFonts({
    Sarabun: require("../assets/fonts/Sarabun-Regular.ttf"),
    "Sarabun-Bold": require("../assets/fonts/Sarabun-Bold.ttf"),
  });

  // Render layout even if fonts haven't loaded yet — Text falls back to system font
  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      <Stack
        screenOptions={{
          headerShown: false,
          animation: "fade",
          contentStyle: { backgroundColor: "#F1F5F9" },
        }}
      />
    </SafeAreaProvider>
  );
}
