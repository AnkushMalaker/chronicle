import { useEffect } from 'react';
import { Stack, useRouter } from 'expo-router';
import { useShareIntent } from 'expo-share-intent';

import ErrorBoundary from '@/components/ErrorBoundary';
import { AppSettingsProvider } from '@/contexts/AppSettingsContext';
import { ConnectionLogProvider } from '@/contexts/ConnectionLogContext';
import { ThemeProvider, useTheme } from '@/theme';
import { initLogger, logInfo } from '@/utils/logger';

/**
 * The navigator, split out because `useTheme()` requires a `ThemeProvider`
 * above it — the component that renders the provider cannot also consume it.
 */
function ThemedStack() {
  const t = useTheme();

  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: t.color.surface.raised },
        headerTintColor: t.color.text.primary,
        headerTitleStyle: { fontWeight: t.weight.semibold },
        contentStyle: { backgroundColor: t.color.surface.page },
      }}
    >
      <Stack.Screen name="index" options={{ title: 'Chronicle', headerShown: false }} />
      <Stack.Screen name="diagnostics" options={{ title: 'Diagnostics', presentation: 'card' }} />
      <Stack.Screen name="settings" options={{ title: 'Settings', presentation: 'card' }} />
      <Stack.Screen name="share" options={{ title: 'Add to Chronicle', presentation: 'modal' }} />
      <Stack.Screen name="theme-preview" options={{ title: 'Design System', presentation: 'card' }} />
    </Stack>
  );
}

export default function RootLayout() {
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
    // The provider sits outside the boundary so the crash screen is themed too.
    <ThemeProvider>
      <ErrorBoundary>
        <ConnectionLogProvider>
          <AppSettingsProvider>
            <ThemedStack />
          </AppSettingsProvider>
        </ConnectionLogProvider>
      </ErrorBoundary>
    </ThemeProvider>
  );
}
