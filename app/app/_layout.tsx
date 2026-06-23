import { useEffect } from "react";
import { Stack } from "expo-router";
import { useTheme } from "@/theme";
import { ConnectionLogProvider } from "@/contexts/ConnectionLogContext";
import { AppSettingsProvider } from "@/contexts/AppSettingsContext";
import ErrorBoundary from "@/components/ErrorBoundary";
import { initLogger, logInfo } from "@/utils/logger";

export default function RootLayout() {
  const { colors, isDark } = useTheme();

  useEffect(() => {
    initLogger().then(() => logInfo('RootLayout', 'app mounted'));
  }, []);

  return (
    <ErrorBoundary>
      <ConnectionLogProvider>
        <AppSettingsProvider>
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
            <Stack.Screen name="settings" options={{ title: 'Settings', presentation: 'card' }} />
          </Stack>
        </AppSettingsProvider>
      </ConnectionLogProvider>
    </ErrorBoundary>
  );
}
