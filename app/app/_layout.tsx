import { useEffect } from 'react';
import { Stack, usePathname, useRouter } from 'expo-router';
import { ShareIntentProvider, useShareIntentContext } from 'expo-share-intent';

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

/**
 * Opens the confirm sheet for a share that arrived without a deep link.
 *
 * Android delivers the share as an Intent straight to the native module, so
 * there is no URL for `+native-intent` to rewrite and nothing navigates on its
 * own. iOS does arrive by URL and is already on `/share` by the time the intent
 * surfaces here, hence the guard — without it the modal would be pushed twice.
 */
function ShareIntentNavigator() {
  const router = useRouter();
  const pathname = usePathname();
  const { hasShareIntent } = useShareIntentContext();

  useEffect(() => {
    if (hasShareIntent && pathname !== '/share') {
      logInfo('RootLayout', 'share intent received');
      router.push('/share');
    }
  }, [hasShareIntent, pathname, router]);

  return <ThemedStack />;
}

export default function RootLayout() {
  useEffect(() => {
    initLogger().then(() => logInfo('RootLayout', 'app mounted'));
  }, []);

  return (
    // One provider rather than a hook per screen: each `useShareIntent` call
    // holds its own copy of the intent and clears the shared native state when
    // it resets, so two of them race to consume the same payload.
    <ShareIntentProvider options={{ resetOnBackground: true }}>
      {/* The theme provider sits outside the boundary so the crash screen is themed too. */}
      <ThemeProvider>
        <ErrorBoundary>
          <ConnectionLogProvider>
            <AppSettingsProvider>
              <ShareIntentNavigator />
            </AppSettingsProvider>
          </ConnectionLogProvider>
        </ErrorBoundary>
      </ThemeProvider>
    </ShareIntentProvider>
  );
}
