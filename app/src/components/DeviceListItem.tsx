import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { OmiDevice } from 'friend-lite-react-native';

import { Badge, Button } from '@/components/ui';
import { useTheme, type Theme } from '@/theme';
import { detectDeviceType } from '@/utils/deviceType';

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
  const t = useTheme();
  const s = createStyles(t);
  const isThisDeviceConnected = connectedDeviceId === device.id;
  const isAnotherDeviceConnected = connectedDeviceId !== null && connectedDeviceId !== device.id;
  const deviceType = detectDeviceType(device.name);

  return (
    <View style={s.deviceItem}>
      <View style={s.deviceInfoContainer}>
        <View style={s.deviceNameRow}>
          <Text style={s.deviceName}>{device.name || 'Unknown Device'}</Text>
          {deviceType !== 'unknown' && (
            <Badge tone={deviceType === 'neo' ? 'warning' : 'accent'} style={s.deviceTypeBadge}>
              {deviceType === 'neo' ? 'Neo' : 'OMI'}
            </Badge>
          )}
          <SignalStrength rssi={device.rssi} />
        </View>
        <Text style={s.deviceInfo}>ID: {device.id}</Text>
        {device.rssi != null && <Text style={s.deviceInfo}>RSSI: {device.rssi} dBm</Text>}
      </View>
      {
        isThisDeviceConnected ? (
          <Button
            variant="danger"
            size="sm"
            onPress={onDisconnect}
            disabled={isConnecting}
          >
            {isConnecting ? 'Disconnecting...' : 'Disconnect'}
          </Button>
        ) : (
          <Button
            variant="primary"
            size="sm"
            onPress={() => onConnect(device.id)}
            disabled={isConnecting || isAnotherDeviceConnected}
          >
            {isConnecting && connectedDeviceId === device.id ? 'Connecting...' : 'Connect'}
          </Button>
        )
      }
    </View>
  );
};

const createStyles = (t: Theme) => StyleSheet.create({
  deviceItem: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingVertical: t.space[3],
    paddingHorizontal: t.space[1.5],
    borderBottomWidth: t.borderWidth,
    borderBottomColor: t.color.border.subtle,
  },
  deviceInfoContainer: {
    flex: 1,
    marginRight: t.space[3],
  },
  deviceNameRow: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  deviceName: {
    fontFamily: t.font.sans,
    ...t.type.base,
    fontWeight: t.weight.medium,
    color: t.color.text.primary,
  },
  deviceInfo: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.secondary,
    marginTop: t.space[0.5],
  },
  deviceTypeBadge: {
    marginLeft: t.space[1.5],
  },
});

export default DeviceListItem;
