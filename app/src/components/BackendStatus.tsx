import React, { useState } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator, Linking } from 'react-native';
import { useTheme, ThemeColors } from '../theme';
import { QRScanner } from './QRScanner';
import { httpUrlToWebSocketUrl } from '../utils/urlConversion';
import { saveServiceManagerUrl, saveServiceManagerToken } from '../utils/storage';
import { useBackendHealth, HealthStatus } from '../hooks/useBackendHealth';

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
  const { colors } = useTheme();
  const s = createStyles(colors);

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

  const getStatusColor = (status: HealthStatus['status']): string => {
    switch (status) {
      case 'healthy': return colors.success;
      case 'checking': return colors.warning;
      case 'auth_required': return colors.warning;
      case 'not_configured': return colors.warning;
      case 'offline': return colors.danger;
      case 'backend_down': return colors.danger;
      case 'unreachable': return colors.danger;
      case 'unhealthy': return colors.danger;
      default: return colors.disabled;
    }
  };

  return (
    <View style={s.section}>
      <Text style={s.sectionTitle}>Backend Connection</Text>

      {/* Primary path: scan the QR. */}
      <TouchableOpacity
        style={s.button}
        onPress={() => setShowQRScanner(true)}
      >
        <Text style={s.buttonText}>Scan QR Code</Text>
      </TouchableOpacity>
      <Text style={s.qrNote}>
        Find this QR on your Chronicle dashboard → System page (“Connect App”).
      </Text>

      <View style={s.statusContainer}>
        <View style={s.statusRow}>
          <Text style={s.statusLabel}>Status:</Text>
          <View style={s.statusValue}>
            <Text style={[s.statusText, { color: getStatusColor(healthStatus.status) }]}>
              {healthStatus.message}
            </Text>
            {healthStatus.status === 'checking' && (
              <ActivityIndicator size="small" color={getStatusColor(healthStatus.status)} style={{ marginLeft: 8 }} />
            )}
          </View>
        </View>
        {healthStatus.detail && (
          <Text style={s.detailText}>{healthStatus.detail}</Text>
        )}
        {healthStatus.lastChecked && (
          <Text style={s.lastCheckedText}>Last checked: {healthStatus.lastChecked.toLocaleTimeString()}</Text>
        )}
      </View>

      {(healthStatus.status === 'unreachable' || healthStatus.status === 'not_configured') && (
        <TouchableOpacity style={s.qrButton} onPress={openTailscale}>
          <Text style={s.qrButtonText}>Open Tailscale</Text>
        </TouchableOpacity>
      )}

      <TouchableOpacity
        style={[s.qrButton, healthStatus.status === 'checking' ? s.buttonDisabled : null]}
        onPress={() => checkBackendHealth(true)}
        disabled={healthStatus.status === 'checking'}
      >
        <Text style={s.qrButtonText}>{healthStatus.status === 'checking' ? 'Checking...' : 'Test Connection'}</Text>
      </TouchableOpacity>

      {/* Manual fallback: type the URL. */}
      <Text style={s.inputLabel}>Or enter the backend URL manually:</Text>
      <TextInput
        style={s.textInput}
        value={backendUrl}
        onChangeText={onBackendUrlChange}
        placeholder="wss://your-machine.ts.net/ws"
        placeholderTextColor={colors.textTertiary}
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
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: colors.card,
    borderRadius: 10,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.1,
    shadowRadius: 3,
    elevation: 2,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 15,
    color: colors.text,
  },
  inputLabel: {
    fontSize: 14,
    color: colors.text,
    marginBottom: 5,
    fontWeight: '500',
  },
  textInput: {
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: 6,
    padding: 10,
    fontSize: 14,
    width: '100%',
    marginBottom: 15,
    color: colors.text,
  },
  statusContainer: {
    marginBottom: 15,
    padding: 10,
    backgroundColor: colors.inputBackground,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: colors.inputBorder,
  },
  statusRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  statusLabel: {
    fontSize: 14,
    fontWeight: '500',
    color: colors.text,
  },
  statusValue: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
    justifyContent: 'flex-end',
  },
  statusText: {
    fontSize: 14,
    fontWeight: '500',
  },
  detailText: {
    fontSize: 13,
    color: colors.textSecondary,
    marginTop: 8,
  },
  qrNote: {
    fontSize: 12,
    color: colors.textTertiary,
    marginTop: 8,
    marginBottom: 15,
    textAlign: 'center',
  },
  lastCheckedText: {
    fontSize: 12,
    color: colors.textTertiary,
    marginTop: 5,
    textAlign: 'center',
    fontStyle: 'italic',
  },
  qrButton: {
    backgroundColor: colors.card,
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    marginBottom: 10,
    borderWidth: 1,
    borderColor: colors.primary,
  },
  qrButtonText: {
    color: colors.primary,
    fontSize: 16,
    fontWeight: '600',
  },
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    marginBottom: 10,
    elevation: 2,
  },
  buttonDisabled: {
    backgroundColor: colors.disabled,
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
  helpText: {
    fontSize: 12,
    color: colors.textTertiary,
    textAlign: 'center',
    fontStyle: 'italic',
  },
});

export default BackendStatus;
