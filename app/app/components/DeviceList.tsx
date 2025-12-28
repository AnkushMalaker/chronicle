import React, { useState, useMemo } from 'react';
import { View, Text, FlatList, Switch, StyleSheet } from 'react-native';
import type { Device } from 'react-native-ble-plx';
import DeviceListItem from './DeviceListItem';
import theme from '../theme/design-system';

interface DeviceListProps {
  devices: Device[];
  onConnect: (device: Device | string) => Promise<void>;
  onDisconnect: () => Promise<void>;
  isConnecting: boolean;
  connectedDeviceId: string | null;
}

/**
 * Component to display scanned Bluetooth devices with filtering options.
 * Allows users to toggle between showing all devices or only OMI/Friend devices.
 */
export const DeviceList: React.FC<DeviceListProps> = ({
  devices,
  onConnect,
  onDisconnect,
  isConnecting,
  connectedDeviceId,
}) => {
  const [showOnlyOmi, setShowOnlyOmi] = useState(false);

  // Filter devices based on toggle
  const filteredDevices = useMemo(() => {
    if (!showOnlyOmi) {
      return devices;
    }
    return devices.filter(device => {
      const name = device.name?.toLowerCase() || '';
      return name.includes('omi') || name.includes('friend');
    });
  }, [devices, showOnlyOmi]);

  if (devices.length === 0) {
    return null;
  }

  return (
    <View style={styles.section} testID="device-list-section">
      <View style={styles.sectionHeaderWithFilter}>
        <Text style={styles.sectionTitle} testID="device-list-title">Found Devices</Text>
        <View style={styles.filterContainer}>
          <Text style={styles.filterText}>Show only OMI/Friend</Text>
          <Switch
            testID="device-filter-toggle"
            accessibilityLabel="Show only OMI/Friend devices"
            trackColor={{ false: theme.colors.gray[400], true: theme.colors.primary.light }}
            thumbColor={showOnlyOmi ? theme.colors.primary.main : theme.colors.gray[100]}
            ios_backgroundColor={theme.colors.gray[600]}
            onValueChange={setShowOnlyOmi}
            value={showOnlyOmi}
          />
        </View>
      </View>

      {filteredDevices.length > 0 ? (
        <FlatList
          testID="device-list-flatlist"
          data={filteredDevices}
          renderItem={({ item }) => (
            <DeviceListItem
              device={item}
              onConnect={onConnect}
              onDisconnect={onDisconnect}
              isConnecting={isConnecting}
              connectedDeviceId={connectedDeviceId}
            />
          )}
          keyExtractor={(item) => item.id}
          style={styles.deviceList}
        />
      ) : (
        <View style={styles.noDevicesContainer} testID="no-devices-message">
          <Text style={styles.noDevicesText}>
            {showOnlyOmi
              ? `No OMI/Friend devices found. ${devices.length} other device(s) hidden by filter.`
              : 'No devices found.'
            }
          </Text>
        </View>
      )}
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
  sectionHeaderWithFilter: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: theme.spacing.md,
  },
  sectionTitle: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
  },
  filterContainer: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  filterText: {
    marginRight: theme.spacing.sm,
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
  },
  deviceList: {
    maxHeight: 200,
  },
  noDevicesContainer: {
    padding: theme.spacing.lg,
    alignItems: 'center',
  },
  noDevicesText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.tertiary,
    textAlign: 'center',
    fontStyle: 'italic',
    lineHeight: theme.typography.lineHeight.normal * theme.typography.fontSize.sm,
  },
});

export default DeviceList;
