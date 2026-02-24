import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator } from 'react-native';
import { useTheme, ThemeColors } from '../theme';
import { QRScanner } from './QRScanner';
import { httpUrlToWebSocketUrl } from '../utils/urlConversion';

interface BackendStatusProps {
  backendUrl: string;
  onBackendUrlChange: (url: string) => void;
  jwtToken: string | null;
}

interface HealthStatus {
  status: 'unknown' | 'checking' | 'healthy' | 'unhealthy' | 'auth_required';
  message: string;
  lastChecked?: Date;
}

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

  const checkBackendHealth = async (showAlert: boolean = false) => {
    if (!backendUrl.trim()) {
      setHealthStatus({ status: 'unhealthy', message: 'Backend URL not set' });
      return;
    }

    setHealthStatus({ status: 'checking', message: 'Checking connection...' });

    try {
      let baseUrl = backendUrl.trim();
      if (baseUrl.startsWith('ws://')) baseUrl = baseUrl.replace('ws://', 'http://');
      else if (baseUrl.startsWith('wss://')) baseUrl = baseUrl.replace('wss://', 'https://');
      baseUrl = baseUrl.split('/ws')[0];

      const healthUrl = `${baseUrl}/health`;
      console.log('[BackendStatus] Checking health at:', healthUrl);

      const response = await fetch(healthUrl, {
        method: 'GET',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
          ...(jwtToken ? { 'Authorization': `Bearer ${jwtToken}` } : {}),
        },
      });

      if (response.ok) {
        const healthData = await response.json();
        setHealthStatus({ status: 'healthy', message: `Connected (${healthData.status || 'OK'})`, lastChecked: new Date() });
        if (showAlert) Alert.alert('Connection Success', 'Successfully connected to backend!');
      } else if (response.status === 401 || response.status === 403) {
        setHealthStatus({ status: 'auth_required', message: 'Authentication required', lastChecked: new Date() });
        if (showAlert) Alert.alert('Authentication Required', 'Please login to access the backend.');
      } else {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
    } catch (error) {
      console.log('[BackendStatus] Health check error:', error);
      let errorMessage = 'Connection failed';
      if (error instanceof Error) {
        console.log('[BackendStatus] Error name:', error.name, 'message:', error.message);
        if (error.message.includes('Network request failed')) errorMessage = 'Network request failed - check URL and network connection';
        else if (error.name === 'AbortError') errorMessage = 'Request timeout';
        else errorMessage = error.message;
      }
      setHealthStatus({ status: 'unhealthy', message: errorMessage, lastChecked: new Date() });
      if (showAlert) {
        Alert.alert('Connection Failed', `Could not connect to backend: ${errorMessage}\n\nMake sure the backend is running and accessible.`);
      }
    }
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
      case 'unhealthy': return colors.danger;
      case 'auth_required': return colors.warning;
      default: return colors.disabled;
    }
  };

  return (
    <View style={s.section}>
      <Text style={s.sectionTitle}>Backend Connection</Text>

      <Text style={s.inputLabel}>Backend URL:</Text>
      <TextInput
        style={s.textInput}
        value={backendUrl}
        onChangeText={onBackendUrlChange}
        placeholder="ws://localhost:8000/ws"
        placeholderTextColor={colors.textTertiary}
        autoCapitalize="none"
        keyboardType="url"
        returnKeyType="done"
        autoCorrect={false}
      />

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
        {healthStatus.lastChecked && (
          <Text style={s.lastCheckedText}>Last checked: {healthStatus.lastChecked.toLocaleTimeString()}</Text>
        )}
      </View>

      <TouchableOpacity
        style={s.qrButton}
        onPress={() => setShowQRScanner(true)}
      >
        <Text style={s.qrButtonText}>Scan QR Code</Text>
      </TouchableOpacity>

      <TouchableOpacity
        style={[s.button, healthStatus.status === 'checking' ? s.buttonDisabled : null]}
        onPress={() => checkBackendHealth(true)}
        disabled={healthStatus.status === 'checking'}
      >
        <Text style={s.buttonText}>{healthStatus.status === 'checking' ? 'Checking...' : 'Test Connection'}</Text>
      </TouchableOpacity>

      <Text style={s.helpText}>
        Enter the WebSocket URL or scan a QR code from the Chronicle dashboard.
      </Text>

      <QRScanner
        visible={showQRScanner}
        onScanned={(httpUrl) => {
          const wsUrl = httpUrlToWebSocketUrl(httpUrl);
          onBackendUrlChange(wsUrl);
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
