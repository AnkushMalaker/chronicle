import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator, Linking } from 'react-native';
import NetInfo from '@react-native-community/netinfo';
import { useTheme, ThemeColors } from '../theme';
import { QRScanner } from './QRScanner';
import { httpUrlToWebSocketUrl } from '../utils/urlConversion';
import { saveServiceManagerUrl, saveServiceManagerToken } from '../utils/storage';
import { isServiceManagerReachable } from '../services/serviceManager';

interface BackendStatusProps {
  backendUrl: string;
  onBackendUrlChange: (url: string) => void;
  jwtToken: string | null;
}

// Connection Doctor classifications, from a layered probe ladder.
type HealthState =
  | 'unknown'
  | 'checking'
  | 'healthy'
  | 'auth_required'
  | 'not_configured' // no real backend set yet (fresh install / localhost)
  | 'offline'        // device has no network at all
  | 'backend_down'   // host reachable (SM answers) but the backend isn't
  | 'unreachable'    // couldn't reach the host at all
  | 'unhealthy';     // backend host reachable but returned an error

interface HealthStatus {
  status: HealthState;
  message: string;
  detail?: string;   // actionable next step
  lastChecked?: Date;
}

// A fresh install points at localhost, which on a phone is the phone itself and
// can never be a real backend — treat that (and empty) as "not configured".
const isNotConfigured = (url: string): boolean => {
  const trimmed = (url || '').trim();
  if (!trimmed) return true;
  try {
    const base = trimmed
      .replace('ws://', 'http://')
      .replace('wss://', 'https://')
      .split('/ws')[0];
    const host = new URL(base).hostname;
    return host === 'localhost' || host === '127.0.0.1' || host === '::1';
  } catch {
    return false;
  }
};

// A tailnet host: Tailscale CGNAT range (100.64.0.0/10) or a *.ts.net MagicDNS name.
const isTailnetHost = (baseUrl: string): boolean => {
  try {
    const host = new URL(baseUrl).hostname;
    if (host.endsWith('.ts.net')) return true;
    const m = host.match(/^(\d+)\.(\d+)\.\d+\.\d+$/);
    if (m) {
      const a = Number(m[1]);
      const b = Number(m[2]);
      return a === 100 && b >= 64 && b <= 127;
    }
    return false;
  } catch {
    return false;
  }
};

export const BackendStatus: React.FC<BackendStatusProps> = ({
  backendUrl,
  onBackendUrlChange,
  jwtToken,
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);

  const [healthStatus, setHealthStatus] = useState<HealthStatus>({
    status: 'unknown',
    message: 'Not checked',
  });
  const [showQRScanner, setShowQRScanner] = useState(false);

  // Connection Doctor: a layered probe ladder that classifies *why* a connection
  // failed and what to do next, instead of a single fetch that collapses every
  // failure into "Network request failed".
  const checkBackendHealth = async (showAlert: boolean = false) => {
    // Fresh install / localhost: not an error, just not set up yet.
    if (isNotConfigured(backendUrl)) {
      setHealthStatus({
        status: 'not_configured',
        message: 'Not connected yet',
        detail: 'Make sure Tailscale is connected, then scan the QR code from your Chronicle dashboard (System page).',
      });
      if (showAlert) {
        Alert.alert('Not Connected', 'Scan the QR code from your Chronicle dashboard to connect this app.');
      }
      return;
    }

    setHealthStatus({ status: 'checking', message: 'Checking connection...' });

    let baseUrl = backendUrl.trim();
    if (baseUrl.startsWith('ws://')) baseUrl = baseUrl.replace('ws://', 'http://');
    else if (baseUrl.startsWith('wss://')) baseUrl = baseUrl.replace('wss://', 'https://');
    baseUrl = baseUrl.split('/ws')[0];

    // Rung 1: is the device online at all?
    const netState = await NetInfo.fetch();
    if (!netState.isConnected) {
      setHealthStatus({ status: 'offline', message: "You're offline", detail: 'Check Wi-Fi or cellular data.', lastChecked: new Date() });
      if (showAlert) Alert.alert('Offline', 'Your device has no network connection.');
      return;
    }

    // Rung 2: can we reach the backend host, and is it healthy?
    const healthUrl = `${baseUrl}/health`;
    console.log('[BackendStatus] Checking health at:', healthUrl);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);

    try {
      const response = await fetch(healthUrl, {
        method: 'GET',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
          ...(jwtToken ? { 'Authorization': `Bearer ${jwtToken}` } : {}),
        },
        signal: controller.signal,
      });

      if (response.ok) {
        const healthData = await response.json();
        setHealthStatus({ status: 'healthy', message: `Connected (${healthData.status || 'OK'})`, lastChecked: new Date() });
        if (showAlert) Alert.alert('Connection Success', 'Successfully connected to backend!');
      } else if (response.status === 401 || response.status === 403) {
        setHealthStatus({ status: 'auth_required', message: 'Authentication required', detail: 'Log in below to access the backend.', lastChecked: new Date() });
        if (showAlert) Alert.alert('Authentication Required', 'Please login to access the backend.');
      } else {
        // Host reachable (it answered) but returned an error.
        setHealthStatus({
          status: 'unhealthy',
          message: `Backend reachable but unhealthy (HTTP ${response.status})`,
          detail: 'The server is up but not ready. Check its logs.',
          lastChecked: new Date(),
        });
        if (showAlert) Alert.alert('Backend Unhealthy', `The backend responded with HTTP ${response.status}.`);
      }
    } catch (error) {
      // Couldn't reach the backend. Don't blame Tailscale on a guess — probe the
      // service-manager on the SAME host (port 8775). If it answers, the host
      // (and tailnet) are reachable and it's the backend specifically that's
      // down. If it doesn't, the host itself is unreachable.
      console.log('[BackendStatus] Health check error:', error);
      const timedOut = error instanceof Error && error.name === 'AbortError';
      const tailnet = isTailnetHost(baseUrl);
      const hostReachable = await isServiceManagerReachable(baseUrl);

      let host = baseUrl;
      try { host = new URL(baseUrl).host; } catch {}

      if (hostReachable) {
        // Tailnet/host is fine; the backend just isn't up.
        setHealthStatus({
          status: 'backend_down',
          message: `Backend is down at ${host}`,
          detail: 'The machine is reachable but the backend isn’t running. Start it (Network Overview below) or check its logs.',
          lastChecked: new Date(),
        });
        if (showAlert) Alert.alert('Backend Down', `${host} is reachable but the backend isn’t responding.`);
      } else {
        const message = `Can't reach ${host}${timedOut ? ' (timed out)' : ''}`;
        const detail = tailnet
          ? 'The machine isn’t reachable. Check that Tailscale is connected on BOTH this phone and the backend machine, and that the backend machine is online. If you used a raw 100.x IP over HTTPS, try the machine’s …ts.net name instead (a self-signed cert on an IP is rejected by the app).'
          : 'Confirm the backend machine is online and the URL is correct, or scan a QR code.';
        setHealthStatus({ status: 'unreachable', message, detail, lastChecked: new Date() });
        if (showAlert) Alert.alert('Connection Failed', `${message}\n\n${detail}`);
      }
    } finally {
      clearTimeout(timeout);
    }
  };

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

  useEffect(() => {
    if (backendUrl.trim()) {
      const timer = setTimeout(() => { checkBackendHealth(false); }, 500);
      return () => clearTimeout(timer);
    }
  }, [backendUrl, jwtToken]);

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
