import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator, Platform, ScrollView } from 'react-native';
import theme from '../theme/design-system';

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

// URL Presets for quick connection
const URL_PRESETS = [
  { label: 'Local Simple Backend', value: 'ws://localhost:8000/ws', description: 'No auth required' },
  { label: 'Local Advanced Backend', value: 'ws://localhost:8000/ws_pcm', description: 'Requires login' },
  { label: 'Tailscale (Advanced)', value: 'wss://100.x.x.x/ws_pcm', description: 'Replace with your Tailscale IP' },
  { label: 'Custom URL', value: '', description: 'Enter manually below' },
];

export const BackendStatus: React.FC<BackendStatusProps> = ({
  backendUrl,
  onBackendUrlChange,
  jwtToken,
}) => {
  const [healthStatus, setHealthStatus] = useState<HealthStatus>({
    status: 'unknown',
    message: 'Not checked',
  });

  const [selectedPreset, setSelectedPreset] = useState<string>('');
  const [customUrl, setCustomUrl] = useState<string>(backendUrl);

  // Initialize preset selection based on current URL
  useEffect(() => {
    const matchingPreset = URL_PRESETS.find(preset => preset.value === backendUrl);
    if (matchingPreset) {
      setSelectedPreset(matchingPreset.value);
    } else {
      setSelectedPreset(''); // Custom
      setCustomUrl(backendUrl);
    }
  }, []);

  const checkBackendHealth = async (showAlert: boolean = false) => {
    if (!backendUrl.trim()) {
      setHealthStatus({
        status: 'unhealthy',
        message: 'Backend URL not set',
      });
      return;
    }

    setHealthStatus({
      status: 'checking',
      message: 'Checking connection...',
    });

    try {
      // Convert WebSocket URL to HTTP URL for health check
      let baseUrl = backendUrl.trim();

      // Handle different URL formats
      if (baseUrl.startsWith('ws://')) {
        baseUrl = baseUrl.replace('ws://', 'http://');
      } else if (baseUrl.startsWith('wss://')) {
        baseUrl = baseUrl.replace('wss://', 'https://');
      }

      // Remove any WebSocket path if present
      baseUrl = baseUrl.split('/ws')[0];

      // Try health endpoint first
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

      console.log('[BackendStatus] Health check response status:', response.status);

      if (response.ok) {
        const healthData = await response.json();
        setHealthStatus({
          status: 'healthy',
          message: `Connected (${healthData.status || 'OK'})`,
          lastChecked: new Date(),
        });

        if (showAlert) {
          Alert.alert('Connection Success', 'Successfully connected to backend!');
        }
      } else if (response.status === 401 || response.status === 403) {
        setHealthStatus({
          status: 'auth_required',
          message: 'Authentication required',
          lastChecked: new Date(),
        });

        if (showAlert) {
          Alert.alert('Authentication Required', 'Please login to access the backend.');
        }
      } else {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
    } catch (error) {
      console.error('[BackendStatus] Health check error:', error);

      let errorMessage = 'Connection failed';
      if (error instanceof Error) {
        if (error.message.includes('Network request failed')) {
          errorMessage = 'Network request failed - check URL and network connection';
        } else if (error.name === 'AbortError') {
          errorMessage = 'Request timeout';
        } else {
          errorMessage = error.message;
        }
      }

      setHealthStatus({
        status: 'unhealthy',
        message: errorMessage,
        lastChecked: new Date(),
      });

      if (showAlert) {
        Alert.alert(
          'Connection Failed',
          `Could not connect to backend: ${errorMessage}\n\nMake sure the backend is running and accessible.`
        );
      }
    }
  };

  // Debounced health check - now waits 1.5 seconds after typing stops
  useEffect(() => {
    if (backendUrl.trim()) {
      const timer = setTimeout(() => {
        checkBackendHealth(false);
      }, 1500); // Increased from 500ms to 1.5s for better UX

      return () => clearTimeout(timer);
    }
  }, [backendUrl, jwtToken]);

  const handlePresetChange = (value: string) => {
    setSelectedPreset(value);
    if (value) {
      // Preset selected
      onBackendUrlChange(value);
      setCustomUrl(value);
    }
  };

  const handleCustomUrlChange = (text: string) => {
    setCustomUrl(text);
    onBackendUrlChange(text);
    setSelectedPreset(''); // Switch to custom mode
  };

  const getStatusColor = (status: HealthStatus['status']): string => {
    switch (status) {
      case 'healthy':
        return theme.colors.status.healthy;
      case 'checking':
        return theme.colors.status.checking;
      case 'unhealthy':
        return theme.colors.status.unhealthy;
      case 'auth_required':
        return theme.colors.status.checking;
      default:
        return theme.colors.status.unknown;
    }
  };

  const getStatusIcon = (status: HealthStatus['status']): string => {
    switch (status) {
      case 'healthy':
        return '✅';
      case 'checking':
        return '🔄';
      case 'unhealthy':
        return '❌';
      case 'auth_required':
        return '🔐';
      default:
        return '❓';
    }
  };

  const currentPresetDescription = URL_PRESETS.find(p => p.value === selectedPreset)?.description;

  return (
    <View style={styles.section} testID="backend-status-section">
      <Text style={styles.sectionTitle} testID="backend-status-title">Backend Connection</Text>

      {/* Quick Connect Presets */}
      <Text style={styles.inputLabel}>Quick Connect:</Text>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        style={styles.presetsScrollView}
        contentContainerStyle={styles.presetsContainer}
      >
        {URL_PRESETS.map(preset => (
          <TouchableOpacity
            key={preset.value || 'custom'}
            testID={`preset-${preset.label.toLowerCase().replace(/\s+/g, '-')}`}
            style={[
              styles.presetButton,
              selectedPreset === preset.value && styles.presetButtonActive
            ]}
            onPress={() => handlePresetChange(preset.value)}
          >
            <Text style={[
              styles.presetButtonText,
              selectedPreset === preset.value && styles.presetButtonTextActive
            ]}>
              {preset.label}
            </Text>
            <Text style={styles.presetDescription}>
              {preset.description}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* Custom URL Input */}
      <Text style={styles.inputLabel}>Backend URL:</Text>
      <TextInput
        testID="backend-url-input"
        style={styles.textInput}
        value={customUrl}
        onChangeText={handleCustomUrlChange}
        placeholder="ws://192.168.1.100:8000/ws_pcm"
        autoCapitalize="none"
        keyboardType="url"
        returnKeyType="done"
        autoCorrect={false}
        autoComplete="off"
        spellCheck={false}
        clearButtonMode="while-editing"
        enablesReturnKeyAutomatically={true}
      />

      {/* Connection Status */}
      <View style={styles.statusContainer}>
        <View style={styles.statusRow}>
          <Text style={styles.statusLabel}>Status:</Text>
          <View style={styles.statusValue}>
            <Text style={styles.statusIcon}>{getStatusIcon(healthStatus.status)}</Text>
            <Text style={[styles.statusText, { color: getStatusColor(healthStatus.status) }]}>
              {healthStatus.message}
            </Text>
            {healthStatus.status === 'checking' && (
              <ActivityIndicator size="small" color={getStatusColor(healthStatus.status)} style={{ marginLeft: 8 }} />
            )}
          </View>
        </View>

        {healthStatus.lastChecked && (
          <Text style={styles.lastCheckedText}>
            Last checked: {healthStatus.lastChecked.toLocaleTimeString()}
          </Text>
        )}
      </View>

      {/* Test Connection Button */}
      <TouchableOpacity
        testID="test-connection-button"
        accessibilityLabel="Test backend connection"
        style={[styles.button, healthStatus.status === 'checking' ? styles.buttonDisabled : null]}
        onPress={() => checkBackendHealth(true)}
        disabled={healthStatus.status === 'checking'}
      >
        <Text style={styles.buttonText}>
          {healthStatus.status === 'checking' ? 'Checking...' : 'Test Connection'}
        </Text>
      </TouchableOpacity>

      <Text style={styles.helpText}>
        Select a quick connect option above, or enter a custom URL. Connection is automatically tested after typing stops.
      </Text>
    </View>
  );
};

const styles = StyleSheet.create({
  section: {
    marginBottom: theme.spacing.lg,
    padding: theme.spacing.md,
    backgroundColor: theme.colors.background.primary,
    borderRadius: theme.borderRadius.md,
    ...theme.shadows.sm,
  },
  sectionTitle: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.semibold,
    marginBottom: theme.spacing.md,
    color: theme.colors.text.primary,
  },
  inputLabel: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
    marginBottom: theme.spacing.xs,
    marginTop: theme.spacing.sm,
    fontWeight: theme.typography.fontWeight.medium,
  },
  presetsScrollView: {
    marginBottom: theme.spacing.md,
  },
  presetsContainer: {
    flexDirection: 'row',
    gap: theme.spacing.sm,
    paddingVertical: theme.spacing.xs,
  },
  presetButton: {
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
    backgroundColor: theme.colors.gray[100],
    borderRadius: theme.borderRadius.sm,
    borderWidth: 2,
    borderColor: theme.colors.border.light,
    minWidth: 140,
  },
  presetButtonActive: {
    backgroundColor: theme.colors.primary.dark + '30',  // Dark mode primary tint
    borderColor: theme.colors.primary.main,
    ...theme.shadows.sm,
  },
  presetButtonText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
    marginBottom: 2,
  },
  presetButtonTextActive: {
    color: theme.colors.primary.main,
  },
  presetDescription: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
    fontStyle: 'italic',
  },
  textInput: {
    ...theme.components.input,
    marginBottom: theme.spacing.md,
  },
  statusContainer: {
    marginBottom: theme.spacing.md,
    padding: theme.spacing.sm,
    backgroundColor: theme.colors.background.secondary,
    borderRadius: theme.borderRadius.sm,
    borderWidth: 1,
    borderColor: theme.colors.border.light,
  },
  statusRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  statusLabel: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.primary,
  },
  statusValue: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
    justifyContent: 'flex-end',
  },
  statusIcon: {
    fontSize: theme.typography.fontSize.md,
    marginRight: theme.spacing.xs + 2,
  },
  statusText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.medium,
  },
  lastCheckedText: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.secondary,
    marginTop: theme.spacing.xs,
    textAlign: 'center',
    fontStyle: 'italic',
  },
  button: {
    ...theme.components.button.primary,
    alignItems: 'center',
    marginBottom: theme.spacing.sm,
  },
  buttonDisabled: {
    backgroundColor: theme.colors.gray[300],
    borderWidth: 1,
    borderColor: theme.colors.border.medium,
  },
  buttonText: {
    color: theme.colors.primary.contrast,
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
  },
  helpText: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
    textAlign: 'center',
    fontStyle: 'italic',
    lineHeight: theme.typography.lineHeight.relaxed * theme.typography.fontSize.xs,
  },
});

export default BackendStatus;
