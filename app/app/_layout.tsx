import { Stack } from "expo-router";
import { useTheme } from "@/theme";
import { ConnectionLogProvider } from "@/contexts/ConnectionLogContext";

export default function RootLayout() {
  const { colors, isDark } = useTheme();

  return (
    <ConnectionLogProvider>
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: colors.card },
          headerTintColor: colors.text,
          headerTitleStyle: { fontWeight: '600' },
          contentStyle: { backgroundColor: colors.background },
        }}
      >
        <Stack.Screen name="index" options={{ title: 'Chronicle', headerShown: false }} />
        <Stack.Screen name="diagnostics" options={{ title: 'Diagnostics', presentation: 'card' }} />
      </Stack>
    </ConnectionLogProvider>
  );
}
