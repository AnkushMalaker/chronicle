import React from 'react';
import { SafeAreaView, ScrollView, KeyboardAvoidingView, Platform, StyleSheet, Text } from 'react-native';
import { useTheme, ThemeColors } from '@/theme';
import { useSharedAppSettings } from '@/contexts/AppSettingsContext';

import BackendStatus from '@/components/BackendStatus';
import AuthSection from '@/components/AuthSection';
import SystemAdminControls from '@/components/SystemAdminControls';
import NetworkOverview from '@/components/NetworkOverview';

export default function SettingsScreen() {
  const { colors } = useTheme();
  const s = createStyles(colors);
  const settings = useSharedAppSettings();

  return (
    <SafeAreaView style={s.container}>
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={Platform.OS === 'ios' ? 100 : 0}
      >
        <ScrollView contentContainerStyle={s.content} keyboardShouldPersistTaps="handled">
          <Text style={s.sectionLabel}>Connection</Text>
          <BackendStatus
            backendUrl={settings.webSocketUrl}
            onBackendUrlChange={settings.handleSetAndSaveWebSocketUrl}
            jwtToken={settings.jwtToken}
          />
          <AuthSection
            backendUrl={settings.webSocketUrl}
            isAuthenticated={settings.isAuthenticated}
            currentUserEmail={settings.currentUserEmail}
            onAuthStatusChange={settings.handleAuthStatusChange}
          />

          <Text style={s.sectionLabel}>Administration</Text>
          <SystemAdminControls backendUrl={settings.webSocketUrl} jwtToken={settings.jwtToken} />
          <NetworkOverview backendUrl={settings.webSocketUrl} />
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  content: {
    padding: 16,
    paddingBottom: 40,
  },
  sectionLabel: {
    fontSize: 12,
    color: colors.textTertiary,
    textTransform: 'uppercase',
    letterSpacing: 0.8,
    marginBottom: 8,
    marginTop: 2,
    fontWeight: '700',
  },
});
