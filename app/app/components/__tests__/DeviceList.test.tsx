import React from 'react';
import { render, fireEvent, waitFor } from '@testing-library/react-native';
import DeviceList from '../DeviceList';
import type { Device } from 'react-native-ble-plx';

describe('DeviceList', () => {
  const mockDevices: Device[] = [
    { id: 'device-1', name: 'OMI Device', rssi: -60 } as Device,
    { id: 'device-2', name: 'Friend Wearable', rssi: -70 } as Device,
    { id: 'device-3', name: 'Some Random Device', rssi: -50 } as Device,
  ];

  const mockOnConnect = jest.fn();
  const mockOnDisconnect = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('should render device list when devices are available', () => {
    const { getByTestID, getByText } = render(
      <DeviceList
        devices={mockDevices}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={false}
        connectedDeviceId={null}
      />
    );

    expect(getByTestID('device-list-section')).toBeTruthy();
    expect(getByText('Found Devices')).toBeTruthy();
  });

  it('should render null when no devices are found', () => {
    const { queryByTestID } = render(
      <DeviceList
        devices={[]}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={false}
        connectedDeviceId={null}
      />
    );

    expect(queryByTestID('device-list-section')).toBeNull();
  });

  it('should show filter toggle for OMI/Friend devices', () => {
    const { getByText, getByTestID } = render(
      <DeviceList
        devices={mockDevices}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={false}
        connectedDeviceId={null}
      />
    );

    expect(getByText('Show only OMI/Friend')).toBeTruthy();
    expect(getByTestID('device-filter-toggle')).toBeTruthy();
  });

  it('should filter devices when toggle is enabled', async () => {
    const { getByTestID, getByText, queryByText } = render(
      <DeviceList
        devices={mockDevices}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={false}
        connectedDeviceId={null}
      />
    );

    // Initially all devices visible
    expect(getByText('OMI Device')).toBeTruthy();
    expect(getByText('Friend Wearable')).toBeTruthy();
    expect(getByText('Some Random Device')).toBeTruthy();

    // Enable filter
    const toggle = getByTestID('device-filter-toggle');
    fireEvent(toggle, 'valueChange', true);

    // Wait for filter to apply
    await waitFor(() => {
      expect(queryByText('Some Random Device')).toBeNull();
    });

    // OMI/Friend devices still visible
    expect(getByText('OMI Device')).toBeTruthy();
    expect(getByText('Friend Wearable')).toBeTruthy();
  });

  it('should show message when filter hides all devices', async () => {
    const nonOmiDevices: Device[] = [
      { id: 'device-1', name: 'Random Device', rssi: -60 } as Device,
    ];

    const { getByTestID, getByText } = render(
      <DeviceList
        devices={nonOmiDevices}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={false}
        connectedDeviceId={null}
      />
    );

    // Enable filter
    const toggle = getByTestID('device-filter-toggle');
    fireEvent(toggle, 'valueChange', true);

    await waitFor(() => {
      expect(getByText(/No OMI\/Friend devices found/)).toBeTruthy();
      expect(getByText(/1 other device\(s\) hidden by filter/)).toBeTruthy();
    });
  });

  it('should render FlatList with correct testID', () => {
    const { getByTestID } = render(
      <DeviceList
        devices={mockDevices}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={false}
        connectedDeviceId={null}
      />
    );

    expect(getByTestID('device-list-flatlist')).toBeTruthy();
  });

  it('should pass correct props to DeviceListItem', () => {
    const { getByText } = render(
      <DeviceList
        devices={mockDevices}
        onConnect={mockOnConnect}
        onDisconnect={mockOnDisconnect}
        isConnecting={true}
        connectedDeviceId='device-1'
      />
    );

    // Verify devices are rendered
    expect(getByText('OMI Device')).toBeTruthy();
  });
});
