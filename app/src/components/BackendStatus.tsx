import React, { useState } from 'react';
import { View, Text, StyleSheet, Alert, ActivityIndicator, Linking } from 'react-native';

import { QRScanner } from './QRScanner';
import { Body, Button, Caption, Card, CardWell, TextField } from '@/components/ui';
import { useBackendHealth, HealthStatus } from '@/hooks/useBackendHealth';
import { useTheme, type Theme } from '@/theme';
import { saveServiceManagerUrl, saveServiceManagerToken } from '@/utils/storage';
import { httpUrlToWebSocketUrl } from '@/utils/urlConversion';

interface BackendStatusProps {
  backendUrl: string;
  onBackendUrlChange: (url: string) => void;
  jwtToken: string | null;
}

export const BackendStatus: React.FC<BackendStatusProps> = ({
  backendUrl,
  onBackendUrlChange,
  jwtToken,
}) => {
  const t = useTheme();
  const s = createStyles(t);

  const { healthStatus, checkBackendHealth } = useBackendHealth(backendUrl, jwtToken);
  const [showQRScanner, setShowQRScanner] = useState(false);

  const openTailscale = async () => {
    // Guard with canOpenURL: an un-whitelisted scheme makes iOS reject the deep
    // link with a scary "could not verify" error. If we can't open it, just guide.
    try {
      if (await Linking.canOpenURL('tailscale://')) {
        await Linking.openURL('tailscale://');
        return;
      }
    } catch {
      // fall through to guidance
    }
    Alert.alert(
      'Open Tailscale',
      'Open the Tailscale app from your home screen and make sure it shows "Connected", then come back and tap Test Connection.'
    );
  };

  // These land on text and on a spinner, never on a fill, so every branch reads
  // from the `.fg` end of the status ramp.
  const getStatusColor = (status: HealthStatus['status']): string => {
    switch (status) {
      case 'healthy': return t.color.status.success.fg;
      case 'checking': return t.color.status.warning.fg;
      case 'auth_required': return t.color.status.warning.fg;
      case 'not_configured': return t.color.status.warning.fg;
      case 'offline': return t.color.status.danger.fg;
      case 'backend_down': return t.color.status.danger.fg;
      case 'unreachable': return t.color.status.danger.fg;
      case 'unhealthy': return t.color.status.danger.fg;
      default: return t.color.text.muted;
    }
  };

  return (
    <Card title="Backend Connection">
      {/* Primary path: scan the QR. */}
      <Button
        variant="primary"
        size="lg"
        fullWidth
        onPress={() => setShowQRScanner(true)}
      >
        Scan QR Code
      </Button>
      <Caption style={s.qrNote}>
        Find this QR on your Chronicle dashboard → System page (“Connect App”).
      </Caption>

      <CardWell style={s.statusContainer}>
        <View style={s.statusRow}>
          <Text style={s.statusLabel}>Status:</Text>
          <View style={s.statusValue}>
            <Text style={[s.statusText, { color: getStatusColor(healthStatus.status) }]}>
              {healthStatus.message}
            </Text>
            {healthStatus.status === 'checking' && (
              <ActivityIndicator size="small" color={getStatusColor(healthStatus.status)} style={s.statusSpinner} />
            )}
          </View>
        </View>
        {healthStatus.detail && (
          <Body style={s.detailText}>{healthStatus.detail}</Body>
        )}
        {healthStatus.lastChecked && (
          <Caption style={s.lastCheckedText}>Last checked: {healthStatus.lastChecked.toLocaleTimeString()}</Caption>
        )}
      </CardWell>

      {(healthStatus.status === 'unreachable' || healthStatus.status === 'not_configured') && (
        <Button variant="outline" size="lg" fullWidth onPress={openTailscale} style={s.stackedButton}>
          Open Tailscale
        </Button>
      )}

      <Button
        variant="outline"
        size="lg"
        fullWidth
        onPress={() => checkBackendHealth(true)}
        disabled={healthStatus.status === 'checking'}
        style={s.stackedButton}
      >
        {healthStatus.status === 'checking' ? 'Checking...' : 'Test Connection'}
      </Button>

      {/* Manual fallback: type the URL. */}
      <TextField
        label="Or enter the backend URL manually:"
        value={backendUrl}
        onChangeText={onBackendUrlChange}
        placeholder="wss://your-machine.ts.net/ws/audio"
        autoCapitalize="none"
        keyboardType="url"
        returnKeyType="done"
        autoCorrect={false}
      />

      <QRScanner
        visible={showQRScanner}
        onScanned={(config) => {
          const wsUrl = httpUrlToWebSocketUrl(config.backendUrl);
          onBackendUrlChange(wsUrl);
          // Persist service-manager config (if the QR bundle carried it) so the
          // app can start a down backend directly.
          if (config.serviceManagerUrl !== undefined) {
            saveServiceManagerUrl(config.serviceManagerUrl || null);
          }
          if (config.smToken !== undefined) {
            saveServiceManagerToken(config.smToken || null);
          }
        }}
        onClose={() => setShowQRScanner(false)}
      />
    </Card>
  );
};

const createStyles = (t: Theme) =>
  StyleSheet.create({
    statusContainer: {
      marginBottom: t.space[4],
    },
    statusRow: {
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
    },
    statusLabel: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      fontWeight: t.weight.medium,
      color: t.color.text.primary,
    },
    statusValue: {
      flexDirection: 'row',
      alignItems: 'center',
      flex: 1,
      justifyContent: 'flex-end',
    },
    statusText: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      fontWeight: t.weight.medium,
    },
    statusSpinner: {
      marginLeft: t.space[2],
    },
    detailText: {
      marginTop: t.space[2],
    },
    qrNote: {
      marginTop: t.space[2],
      marginBottom: t.space[4],
      textAlign: 'center',
    },
    lastCheckedText: {
      marginTop: t.space[1],
      textAlign: 'center',
      fontStyle: 'italic',
    },
    stackedButton: {
      marginBottom: t.space[2],
    },
  });

export default BackendStatus;
