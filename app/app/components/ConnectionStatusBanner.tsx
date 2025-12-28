import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import theme from '../theme/design-system';

interface ConnectionStatusBannerProps {
  bluetoothHealth: 'good' | 'poor' | 'lost' | 'disconnected';
  webSocketHealth: 'connected' | 'connecting' | 'disconnected' | 'error';
  minutesUntilTokenExpiration: number | null;
  onReconnect?: () => void;
}

/**
 * Banner component that displays connection health warnings.
 * Only shows when there are connection issues or token is expiring soon.
 */
export const ConnectionStatusBanner: React.FC<ConnectionStatusBannerProps> = ({
  bluetoothHealth,
  webSocketHealth,
  minutesUntilTokenExpiration,
  onReconnect,
}) => {
  // Determine if we should show a banner
  const hasBluetoothIssue = bluetoothHealth === 'poor' || bluetoothHealth === 'lost';
  const hasWebSocketIssue = webSocketHealth === 'disconnected' || webSocketHealth === 'error';
  const tokenExpiringSoon = minutesUntilTokenExpiration !== null && minutesUntilTokenExpiration <= 15 && minutesUntilTokenExpiration > 0;

  if (!hasBluetoothIssue && !hasWebSocketIssue && !tokenExpiringSoon) {
    return null; // All good, no banner needed
  }

  // Determine banner type and message
  let bannerStyle = styles.warningBanner;
  let icon = '⚠️';
  let message = '';

  if (hasWebSocketIssue) {
    bannerStyle = styles.errorBanner;
    icon = '❌';
    message = webSocketHealth === 'error'
      ? 'Backend connection error'
      : 'Backend connection lost';
  } else if (bluetoothHealth === 'lost') {
    bannerStyle = styles.errorBanner;
    icon = '❌';
    message = 'Bluetooth device disconnected';
  } else if (bluetoothHealth === 'poor') {
    bannerStyle = styles.warningBanner;
    icon = '⚠️';
    message = 'Weak Bluetooth signal';
  } else if (tokenExpiringSoon) {
    bannerStyle = styles.warningBanner;
    icon = '⏰';
    message = `Session expires in ${minutesUntilTokenExpiration} min`;
  }

  return (
    <View style={[styles.banner, bannerStyle]} testID="connection-status-banner">
      <View style={styles.bannerContent}>
        <Text style={styles.bannerIcon}>{icon}</Text>
        <Text style={styles.bannerText}>{message}</Text>
      </View>

      {onReconnect && (hasBluetoothIssue || hasWebSocketIssue) && (
        <TouchableOpacity
          testID="reconnect-button"
          accessibilityLabel="Reconnect"
          style={styles.reconnectButton}
          onPress={onReconnect}
        >
          <Text style={styles.reconnectButtonText}>Reconnect</Text>
        </TouchableOpacity>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: theme.spacing.md,
    marginBottom: theme.spacing.md,
    borderRadius: theme.borderRadius.md,
    borderWidth: 1,
    ...theme.shadows.sm,
  },
  warningBanner: {
    backgroundColor: theme.colors.warning.background,
    borderColor: theme.colors.warning.light,
  },
  errorBanner: {
    backgroundColor: theme.colors.error.background,
    borderColor: theme.colors.error.light,
  },
  bannerContent: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
  },
  bannerIcon: {
    fontSize: theme.typography.fontSize.lg,
    marginRight: theme.spacing.sm,
  },
  bannerText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.primary,
    fontWeight: theme.typography.fontWeight.medium,
    flex: 1,
  },
  reconnectButton: {
    backgroundColor: theme.colors.primary.main,
    paddingVertical: theme.spacing.xs + 2,
    paddingHorizontal: theme.spacing.md,
    borderRadius: theme.borderRadius.sm,
  },
  reconnectButtonText: {
    color: theme.colors.primary.contrast,
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
  },
});

export default ConnectionStatusBanner;
