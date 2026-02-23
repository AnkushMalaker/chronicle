import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { OmiDevice } from 'friend-lite-react-native';
import { useTheme, ThemeColors } from '../theme';
import SignalStrength from './SignalStrength';

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
  const { colors } = useTheme();
  const s = createStyles(colors);
  const isThisDeviceConnected = connectedDeviceId === device.id;
  const isAnotherDeviceConnected = connectedDeviceId !== null && connectedDeviceId !== device.id;

  return (
    <View style={s.deviceItem}>
      <View style={s.deviceInfoContainer}>
        <View style={{ flexDirection: 'row', alignItems: 'center' }}>
          <Text style={s.deviceName}>{device.name || 'Unknown Device'}</Text>
          <SignalStrength rssi={device.rssi} />
        </View>
        <Text style={s.deviceInfo}>ID: {device.id}</Text>
        {device.rssi != null && <Text style={s.deviceInfo}>RSSI: {device.rssi} dBm</Text>}
      </View>
      {
        isThisDeviceConnected ? (
          <TouchableOpacity
            style={[s.button, s.smallButton, { backgroundColor: colors.danger }]}
            onPress={onDisconnect}
            disabled={isConnecting}
          >
            <Text style={s.buttonText}>{isConnecting ? 'Disconnecting...' : 'Disconnect'}</Text>
          </TouchableOpacity>
        ) : (
          <TouchableOpacity
            style={[
              s.button,
              s.smallButton,
              (isConnecting || isAnotherDeviceConnected) ? s.buttonDisabled : null
            ]}
            onPress={() => onConnect(device.id)}
            disabled={isConnecting || isAnotherDeviceConnected}
          >
            <Text style={s.buttonText}>{isConnecting && connectedDeviceId === device.id ? 'Connecting...' : 'Connect'}</Text>
          </TouchableOpacity>
        )
      }
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  deviceItem: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingVertical: 12,
    paddingHorizontal: 5,
    borderBottomWidth: 1,
    borderBottomColor: colors.separator,
  },
  deviceInfoContainer: {
    flex: 1,
    marginRight: 10,
  },
  deviceName: {
    fontSize: 16,
    fontWeight: '500',
    color: colors.text,
  },
  deviceInfo: {
    fontSize: 12,
    color: colors.textSecondary,
    marginTop: 2,
  },
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    elevation: 1,
  },
  smallButton: {
    paddingVertical: 8,
    paddingHorizontal: 12,
  },
  buttonDisabled: {
    backgroundColor: colors.disabled,
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 14,
    fontWeight: '600',
  },
});

export default DeviceListItem;
