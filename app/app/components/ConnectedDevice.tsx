import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Alert } from 'react-native';
import type { Device } from 'react-native-ble-plx';
import DeviceListItem from './DeviceListItem';
import DeviceDetails from './DeviceDetails';
import theme from '../theme/design-system';

interface ConnectedDeviceProps {
  connectedDeviceId: string;
  device: Device | undefined;
  isConnecting: boolean;
  onDisconnect: () => Promise<void>;
  onClearLastKnownDevice: () => Promise<void>;

  // Device details props
  onGetAudioCodec: () => Promise<void>;
  currentCodec: string | null;
  onGetBatteryLevel: () => Promise<void>;
  batteryLevel: number | null;
  isListeningAudio: boolean;
  onStartAudioListener: () => Promise<void>;
  onStopAudioListener: () => Promise<void>;
  audioPacketsReceived: number;
  webSocketUrl: string;
  onSetWebSocketUrl: (url: string) => Promise<void>;
  isAudioStreaming: boolean;
  isConnectingAudioStreamer: boolean;
  audioStreamerError: string | null;
  userId: string;
  onSetUserId: (id: string) => Promise<void>;
  isAudioListenerRetrying: boolean;
  audioListenerRetryAttempts: number;
}

/**
 * Component to display connected device information and controls.
 * Shows device list item, disconnect button, and detailed device information.
 */
export const ConnectedDevice: React.FC<ConnectedDeviceProps> = ({
  connectedDeviceId,
  device,
  isConnecting,
  onDisconnect,
  onClearLastKnownDevice,
  onGetAudioCodec,
  currentCodec,
  onGetBatteryLevel,
  batteryLevel,
  isListeningAudio,
  onStartAudioListener,
  onStopAudioListener,
  audioPacketsReceived,
  webSocketUrl,
  onSetWebSocketUrl,
  isAudioStreaming,
  isConnectingAudioStreamer,
  audioStreamerError,
  userId,
  onSetUserId,
  isAudioListenerRetrying,
  audioListenerRetryAttempts,
}) => {
  const handleDisconnect = async () => {
    console.log('[ConnectedDevice] Manual disconnect initiated');

    // Prevent auto-reconnection by clearing the last known device ID
    await onClearLastKnownDevice();

    try {
      await onDisconnect();
      console.log('[ConnectedDevice] Manual disconnect successful');
    } catch (error) {
      console.error('[ConnectedDevice] Error during disconnect:', error);
      Alert.alert('Error', 'Failed to disconnect from the device.');
    }
  };

  return (
    <>
      {/* Show device in list if available */}
      {device && (
        <View style={styles.section} testID="connected-device-section">
          <Text style={styles.sectionTitle} testID="connected-device-title">Connected Device</Text>
          <DeviceListItem
            device={device}
            onConnect={() => {}}
            onDisconnect={handleDisconnect}
            isConnecting={isConnecting}
            connectedDeviceId={connectedDeviceId}
          />
        </View>
      )}

      {/* Show standalone disconnect button if device not in list */}
      {!device && (
        <View style={styles.section} testID="connected-device-section">
          <View style={styles.disconnectContainer}>
            <Text style={styles.connectedText} testID="connected-device-id">
              Connected to device: {connectedDeviceId.substring(0, 15)}...
            </Text>
            <TouchableOpacity
              testID="disconnect-button"
              accessibilityLabel="Disconnect from device"
              style={[styles.button, styles.buttonDanger]}
              onPress={handleDisconnect}
              disabled={isConnecting}
            >
              <Text style={styles.buttonText}>
                {isConnecting ? 'Disconnecting...' : 'Disconnect'}
              </Text>
            </TouchableOpacity>
          </View>
        </View>
      )}

      {/* Device details section */}
      <DeviceDetails
        connectedDeviceId={connectedDeviceId}
        onGetAudioCodec={onGetAudioCodec}
        currentCodec={currentCodec}
        onGetBatteryLevel={onGetBatteryLevel}
        batteryLevel={batteryLevel}
        isListeningAudio={isListeningAudio}
        onStartAudioListener={onStartAudioListener}
        onStopAudioListener={onStopAudioListener}
        audioPacketsReceived={audioPacketsReceived}
        webSocketUrl={webSocketUrl}
        onSetWebSocketUrl={onSetWebSocketUrl}
        isAudioStreaming={isAudioStreaming}
        isConnectingAudioStreamer={isConnectingAudioStreamer}
        audioStreamerError={audioStreamerError}
        userId={userId}
        onSetUserId={onSetUserId}
        isAudioListenerRetrying={isAudioListenerRetrying}
        audioListenerRetryAttempts={audioListenerRetryAttempts}
      />
    </>
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
    color: theme.colors.text.primary,
    marginBottom: theme.spacing.sm,
  },
  disconnectContainer: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: theme.spacing.xs,
  },
  connectedText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
    flex: 1,
    marginRight: theme.spacing.sm,
  },
  button: {
    backgroundColor: theme.colors.primary.main,
    paddingVertical: theme.spacing.sm,
    paddingHorizontal: theme.spacing.md,
    borderRadius: theme.borderRadius.sm,
    alignItems: 'center',
  },
  buttonDanger: {
    backgroundColor: theme.colors.error.main,
  },
  buttonText: {
    color: theme.colors.primary.contrast,
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
  },
});

export default ConnectedDevice;
