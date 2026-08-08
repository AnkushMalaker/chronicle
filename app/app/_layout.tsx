import { useEffect } from "react";
import { Stack, useRouter } from "expo-router";
import { useShareIntent } from "expo-share-intent";
import { useTheme } from "@/theme";
import { ConnectionLogProvider } from "@/contexts/ConnectionLogContext";
import { AppSettingsProvider } from "@/contexts/AppSettingsContext";
import ErrorBoundary from "@/components/ErrorBoundary";
import { initLogger, logInfo } from "@/utils/logger";

export default function RootLayout() {
  const { colors, isDark } = useTheme();
  const router = useRouter();
  // Listening here rather than on the home screen so a share opens the confirm
  // sheet even when the app was launched cold straight into another route.
  const { hasShareIntent } = useShareIntent({ resetOnBackground: true });

  useEffect(() => {
    initLogger().then(() => logInfo('RootLayout', 'app mounted'));
  }, []);

  useEffect(() => {
    if (hasShareIntent) {
      logInfo('RootLayout', 'share intent received');
      router.push('/share');
    }
  }, [hasShareIntent, router]);

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
            <Stack.Screen name="share" options={{ title: 'Add to Chronicle', presentation: 'modal' }} />
          </Stack>
        </AppSettingsProvider>
      </ConnectionLogProvider>
    </ErrorBoundary>
  );
}
