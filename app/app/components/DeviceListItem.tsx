import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { OmiDevice } from 'friend-lite-react-native';
import theme from '../theme/design-system';

interface DeviceListItemProps {
  device: OmiDevice;
  onConnect: (deviceId: string) => void;
  onDisconnect: () => void;
  isConnecting: boolean;
  connectedDeviceId: string | null;
}

export const DeviceListItem: React.FC<DeviceListItemProps> = ({ 
  device, 
  onConnect, 
  onDisconnect,
  isConnecting,
  connectedDeviceId
}) => {
  const isThisDeviceConnected = connectedDeviceId === device.id;
  const isAnotherDeviceConnected = connectedDeviceId !== null && connectedDeviceId !== device.id;

  return (
    <View style={styles.deviceItem}>
      <View style={styles.deviceInfoContainer}>
        <Text style={styles.deviceName}>{device.name || 'Unknown Device'}</Text>
        <Text style={styles.deviceInfo}>ID: {device.id}</Text>
        {device.rssi != null && <Text style={styles.deviceInfo}>RSSI: {device.rssi} dBm</Text>}
      </View>
      {
        isThisDeviceConnected ? (
          <TouchableOpacity
            style={[styles.button, styles.smallButton, styles.buttonDanger]}
            onPress={onDisconnect}
            disabled={isConnecting} // Disable if any connection process is ongoing
          >
            <Text style={styles.buttonText}>{isConnecting ? 'Disconnecting...' : 'Disconnect'}</Text>
          </TouchableOpacity>
        ) : (
          <TouchableOpacity
            style={[
              styles.button, 
              styles.smallButton, 
              (isConnecting || isAnotherDeviceConnected) ? styles.buttonDisabled : null
            ]}
            onPress={() => onConnect(device.id)}
            disabled={isConnecting || isAnotherDeviceConnected} // Disable if connecting to this/another device or another device is connected
          >
            <Text style={styles.buttonText}>{isConnecting && connectedDeviceId === device.id ? 'Connecting...' : 'Connect'}</Text>
          </TouchableOpacity>
        )
      }
    </View>
  );
};

const styles = StyleSheet.create({
  deviceItem: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingVertical: theme.spacing.md - 4,
    paddingHorizontal: theme.spacing.xs,
    borderBottomWidth: 1,
    borderBottomColor: theme.colors.border.light,
  },
  deviceInfoContainer: {
    flex: 1,
    marginRight: theme.spacing.sm,
  },
  deviceName: {
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.primary,
  },
  deviceInfo: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.secondary,
    marginTop: 2,
  },
  button: {
    backgroundColor: theme.colors.primary.main,
    paddingVertical: theme.spacing.md - 4,
    paddingHorizontal: theme.spacing.lg + 4,
    borderRadius: theme.borderRadius.sm,
    alignItems: 'center',
    elevation: 1,
  },
  smallButton: {
    paddingVertical: theme.spacing.sm,
    paddingHorizontal: theme.spacing.md - 4,
  },
  buttonDanger: {
    backgroundColor: theme.colors.error.main,
  },
  buttonDisabled: {
    backgroundColor: theme.colors.gray[300],
    borderWidth: 1,
    borderColor: theme.colors.border.medium,
  },
  buttonText: {
    color: theme.colors.primary.contrast,  // Dark text for WCAG AA on emerald
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
  },
});

export default DeviceListItem; 