import React from 'react';
import { render, fireEvent } from '@testing-library/react-native';
import ConnectionStatusBanner from '../ConnectionStatusBanner';

describe('ConnectionStatusBanner', () => {
  const mockOnReconnect = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('should not render when all connections are healthy', () => {
    const { queryByTestID } = render(
      <ConnectionStatusBanner
        bluetoothHealth="good"
        webSocketHealth="connected"
        minutesUntilTokenExpiration={null}
        onReconnect={mockOnReconnect}
      />
    );

    expect(queryByTestID('connection-status-banner')).toBeNull();
  });

  it('should render error banner when WebSocket is disconnected', () => {
    const { getByTestID, getByText } = render(
      <ConnectionStatusBanner
        bluetoothHealth="good"
        webSocketHealth="disconnected"
        minutesUntilTokenExpiration={null}
        onReconnect={mockOnReconnect}
      />
    );

    expect(getByTestID('connection-status-banner')).toBeTruthy();
    expect(getByText('Backend connection lost')).toBeTruthy();
    expect(getByText('❌')).toBeTruthy();
  });

  it('should render warning banner when Bluetooth signal is weak', () => {
    const { getByTestID, getByText } = render(
      <ConnectionStatusBanner
        bluetoothHealth="poor"
        webSocketHealth="connected"
        minutesUntilTokenExpiration={null}
        onReconnect={mockOnReconnect}
      />
    );

    expect(getByTestID('connection-status-banner')).toBeTruthy();
    expect(getByText('Weak Bluetooth signal')).toBeTruthy();
    expect(getByText('⚠️')).toBeTruthy();
  });

  it('should render warning when token is expiring soon', () => {
    const { getByTestID, getByText } = render(
      <ConnectionStatusBanner
        bluetoothHealth="good"
        webSocketHealth="connected"
        minutesUntilTokenExpiration={10}
        onReconnect={mockOnReconnect}
      />
    );

    expect(getByTestID('connection-status-banner')).toBeTruthy();
    expect(getByText('Session expires in 10 min')).toBeTruthy();
    expect(getByText('⏰')).toBeTruthy();
  });

  it('should not show token warning when more than 15 minutes remain', () => {
    const { queryByTestID } = render(
      <ConnectionStatusBanner
        bluetoothHealth="good"
        webSocketHealth="connected"
        minutesUntilTokenExpiration={20}
        onReconnect={mockOnReconnect}
      />
    );

    expect(queryByTestID('connection-status-banner')).toBeNull();
  });

  it('should show reconnect button for connection issues', () => {
    const { getByTestID } = render(
      <ConnectionStatusBanner
        bluetoothHealth="lost"
        webSocketHealth="connected"
        minutesUntilTokenExpiration={null}
        onReconnect={mockOnReconnect}
      />
    );

    const reconnectButton = getByTestID('reconnect-button');
    expect(reconnectButton).toBeTruthy();

    fireEvent.press(reconnectButton);
    expect(mockOnReconnect).toHaveBeenCalledTimes(1);
  });

  it('should prioritize WebSocket error over Bluetooth warning', () => {
    const { getByText } = render(
      <ConnectionStatusBanner
        bluetoothHealth="poor"
        webSocketHealth="disconnected"
        minutesUntilTokenExpiration={null}
        onReconnect={mockOnReconnect}
      />
    );

    // Should show WebSocket error (higher priority)
    expect(getByText('Backend connection lost')).toBeTruthy();
    expect(getByText('❌')).toBeTruthy();
  });

  it('should show Bluetooth disconnected when device is lost', () => {
    const { getByText } = render(
      <ConnectionStatusBanner
        bluetoothHealth="lost"
        webSocketHealth="connected"
        minutesUntilTokenExpiration={null}
        onReconnect={mockOnReconnect}
      />
    );

    expect(getByText('Bluetooth device disconnected')).toBeTruthy();
    expect(getByText('❌')).toBeTruthy();
  });
});
