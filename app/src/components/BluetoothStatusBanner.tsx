import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Linking } from 'react-native';
import { State as BluetoothState } from 'react-native-ble-plx';
import { useTheme, ThemeColors } from '../theme';

interface BluetoothStatusBannerProps {
  bluetoothState: BluetoothState;
  isPermissionsLoading: boolean;
  permissionGranted: boolean;
  onRequestPermission: () => void;
}

export const BluetoothStatusBanner: React.FC<BluetoothStatusBannerProps> = ({
  bluetoothState,
  isPermissionsLoading,
  permissionGranted,
  onRequestPermission
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);

  if (isPermissionsLoading && bluetoothState === BluetoothState.Unknown) {
    return (
      <View style={[s.statusBanner, { backgroundColor: colors.primary }]}>
        <Text style={s.statusText}>Initializing Bluetooth...</Text>
      </View>
    );
  }

  if (bluetoothState === BluetoothState.PoweredOn && permissionGranted) {
    return null;
  }

  let bannerMessage = 'Bluetooth status is unknown.';
  let buttonText = 'Check Status';
  let onButtonPress: (() => void) | undefined = undefined;
  let isWarning = false;

  switch (bluetoothState) {
    case BluetoothState.PoweredOff:
      bannerMessage = 'Bluetooth is turned off. Please enable Bluetooth to use this app.';
      buttonText = 'Open Settings';
      onButtonPress = () => Linking.openSettings().catch(err => console.warn("Couldn't open settings:", err));
      isWarning = true;
      break;
    case BluetoothState.Unauthorized:
      bannerMessage = 'Bluetooth permission not granted. Please allow Bluetooth access.';
      buttonText = 'Grant Permission';
      onButtonPress = onRequestPermission;
      isWarning = true;
      break;
    case BluetoothState.Unsupported:
      bannerMessage = 'Bluetooth is not supported on this device.';
      break;
    case BluetoothState.Resetting:
      bannerMessage = 'Bluetooth is resetting. Please wait.';
      break;
    case BluetoothState.PoweredOn:
      if (!permissionGranted) {
        bannerMessage = 'Bluetooth is on, but permission is needed.';
        buttonText = 'Grant Permission';
        onButtonPress = onRequestPermission;
      }
      break;
    default:
      bannerMessage = `Bluetooth state: ${bluetoothState}. Please ensure it is enabled and permissions are granted.`;
      buttonText = 'Request Permissions';
      onButtonPress = onRequestPermission;
      break;
  }

  return (
    <View style={[s.statusBanner, { backgroundColor: isWarning ? colors.warning : colors.primary }]}>
      <Text style={s.statusText}>{bannerMessage}</Text>
      {onButtonPress && (
        <TouchableOpacity style={s.statusButton} onPress={onButtonPress}>
          <Text style={s.statusButtonText}>{buttonText}</Text>
        </TouchableOpacity>
      )}
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  statusBanner: {
    padding: 12,
    borderRadius: 8,
    marginBottom: 15,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  statusText: {
    color: 'white',
    fontSize: 14,
    fontWeight: '500',
    flex: 1,
    marginRight: 10,
  },
  statusButton: {
    backgroundColor: 'rgba(255, 255, 255, 0.3)',
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 6,
  },
  statusButtonText: {
    color: 'white',
    fontWeight: '600',
    fontSize: 12,
  },
});

export default BluetoothStatusBanner;
