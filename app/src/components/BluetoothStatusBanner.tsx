import React from 'react';
import { Linking } from 'react-native';
import { State as BluetoothState } from 'react-native-ble-plx';

import { Button, InlineAlert } from '@/components/ui';

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
  if (isPermissionsLoading && bluetoothState === BluetoothState.Unknown) {
    return <InlineAlert tone="accent">Initializing Bluetooth...</InlineAlert>;
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
    <InlineAlert
      tone={isWarning ? 'warning' : 'accent'}
      action={
        onButtonPress ? (
          <Button variant="outline" size="sm" onPress={onButtonPress}>
            {buttonText}
          </Button>
        ) : undefined
      }
    >
      {bannerMessage}
    </InlineAlert>
  );
};

export default BluetoothStatusBanner;
