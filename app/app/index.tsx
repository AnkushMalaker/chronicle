import React, { useRef, useCallback, useEffect, useState } from 'react';
import { Text, View, SafeAreaView, ScrollView, Platform, FlatList, ActivityIndicator, Alert, Switch, TouchableOpacity, KeyboardAvoidingView, StyleSheet } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { State as BluetoothState } from 'react-native-ble-plx';
import { Link } from 'expo-router';
import { useTheme, ThemeColors } from '@/theme';

// Hooks
import { useBluetoothManager } from '@/hooks/useBluetoothManager';
import { useDeviceScanning } from '@/hooks/useDeviceScanning';
import { useDeviceConnection } from '@/hooks/useDeviceConnection';
import { useAppSettings } from '@/hooks/useAppSettings';
import { useAutoReconnect } from '@/hooks/useAutoReconnect';
import { useAudioStreamingOrchestrator } from '@/hooks/useAudioStreamingOrchestrator';
import { useAudioListener } from '@/hooks/useAudioListener';
import { useAudioStreamer } from '@/hooks/useAudioStreamer';
import { usePhoneAudioRecorder } from '@/hooks/usePhoneAudioRecorder';
import { useBatteryMonitor } from '@/hooks/useBatteryMonitor';
import { saveLastConnectedDeviceId } from '@/utils/storage';

// Components
import BluetoothStatusBanner from '@/components/BluetoothStatusBanner';
import ScanControls from '@/components/ScanControls';
import DeviceListItem from '@/components/DeviceListItem';
import DeviceDetails from '@/components/DeviceDetails';
import AuthSection from '@/components/AuthSection';
import BackendStatus from '@/components/BackendStatus';
import ObsidianIngest from '@/components/ObsidianIngest';
import PhoneAudioButton from '@/components/PhoneAudioButton';

export default function App() {
  const { colors } = useTheme();
  const s = createStyles(colors);
  const omiConnection = useRef(new OmiConnection()).current;
  const [showOnlyOmi, setShowOnlyOmi] = useState(false);

  // Bluetooth
  const { bleManager, bluetoothState, permissionGranted, requestBluetoothPermission, isPermissionsLoading } = useBluetoothManager();

  // Audio
  const audioStreamer = useAudioStreamer();
  const phoneAudioRecorder = usePhoneAudioRecorder();

  const { isListeningAudio: isOmiAudioListenerActive, audioPacketsReceived, startAudioListener: originalStartAudioListener, stopAudioListener: originalStopAudioListener, isRetrying: isAudioListenerRetrying, retryAttempts: audioListenerRetryAttempts } = useAudioListener(omiConnection, () => !!deviceConnection.connectedDeviceId);

  // Refs for disconnect cleanup
  const isOmiAudioListenerActiveRef = useRef(isOmiAudioListenerActive);
  const isAudioStreamingRef = useRef(audioStreamer.isStreaming);
  useEffect(() => { isOmiAudioListenerActiveRef.current = isOmiAudioListenerActive; }, [isOmiAudioListenerActive]);
  useEffect(() => { isAudioStreamingRef.current = audioStreamer.isStreaming; }, [audioStreamer.isStreaming]);

  // Settings
  const settings = useAppSettings();

  // Device callbacks
  const onDeviceConnect = useCallback(async () => {
    const deviceIdToSave = omiConnection.connectedDeviceId;
    if (deviceIdToSave) {
      await saveLastConnectedDeviceId(deviceIdToSave);
      autoReconnect.setLastKnownDeviceId(deviceIdToSave);
      autoReconnect.setTriedAutoReconnectForCurrentId(false);
    }
  }, [omiConnection]);

  const onDeviceDisconnect = useCallback(async () => {
    if (isOmiAudioListenerActiveRef.current) await originalStopAudioListener();
    if (isAudioStreamingRef.current) audioStreamer.stopStreaming();
    if (phoneAudioRecorder.isRecording) {
      await phoneAudioRecorder.stopRecording();
      orchestrator.setIsPhoneAudioMode(false);
    }
  }, [originalStopAudioListener, audioStreamer.stopStreaming, phoneAudioRecorder.stopRecording, phoneAudioRecorder.isRecording]);

  const deviceConnection = useDeviceConnection(omiConnection, onDeviceDisconnect, onDeviceConnect);

  // Battery monitor
  const batteryMonitor = useBatteryMonitor({
    connectedDeviceId: deviceConnection.connectedDeviceId,
    getBatteryLevel: deviceConnection.getRawBatteryLevel,
    onConnectionLost: deviceConnection.disconnectFromDevice,
  });

  // Auto-reconnect
  const autoReconnect = useAutoReconnect({
    bluetoothState,
    permissionGranted,
    deviceConnection,
    scanning: false,
  });

  // Scanning
  const { devices: scannedDevices, scanning, startScan, stopScan: stopDeviceScanAction } = useDeviceScanning(bleManager, omiConnection, permissionGranted, bluetoothState === BluetoothState.PoweredOn, requestBluetoothPermission);

  // Audio orchestrator
  const orchestrator = useAudioStreamingOrchestrator({
    omiConnection,
    deviceConnection,
    audioStreamer,
    phoneAudioRecorder,
    originalStartAudioListener,
    originalStopAudioListener,
    settings,
  });

  // Cleanup
  const cleanupRefs = useRef({ omiConnection, bleManager, disconnectFromDevice: deviceConnection.disconnectFromDevice, stopAudioStreaming: audioStreamer.stopStreaming, stopPhoneAudio: phoneAudioRecorder.stopRecording });
  useEffect(() => { cleanupRefs.current = { omiConnection, bleManager, disconnectFromDevice: deviceConnection.disconnectFromDevice, stopAudioStreaming: audioStreamer.stopStreaming, stopPhoneAudio: phoneAudioRecorder.stopRecording }; });
  useEffect(() => {
    return () => {
      const refs = cleanupRefs.current;
      if (refs.omiConnection.isConnected()) refs.disconnectFromDevice().catch(() => {});
      if (refs.bleManager) refs.bleManager.destroy();
      refs.stopAudioStreaming();
      refs.stopPhoneAudio().catch(() => {});
    };
  }, []);

  const canScan = React.useMemo(() => (
    permissionGranted && bluetoothState === BluetoothState.PoweredOn &&
    !autoReconnect.isAttemptingAutoReconnect && !deviceConnection.isConnecting &&
    !deviceConnection.connectedDeviceId &&
    (autoReconnect.triedAutoReconnectForCurrentId || !autoReconnect.lastKnownDeviceId)
  ), [permissionGranted, bluetoothState, autoReconnect.isAttemptingAutoReconnect, deviceConnection.isConnecting, deviceConnection.connectedDeviceId, autoReconnect.triedAutoReconnectForCurrentId, autoReconnect.lastKnownDeviceId]);

  const filteredDevices = React.useMemo(() => {
    if (!showOnlyOmi) return scannedDevices;
    return scannedDevices.filter(d => {
      const name = d.name?.toLowerCase() || '';
      return name.includes('omi') || name.includes('friend');
    });
  }, [scannedDevices, showOnlyOmi]);

  // Loading / auto-reconnect screens
  if (isPermissionsLoading && bluetoothState === BluetoothState.Unknown) {
    return (
      <View style={s.centeredMessageContainer}>
        <ActivityIndicator size="large" color={colors.primary} />
        <Text style={s.centeredMessageText}>
          {autoReconnect.isAttemptingAutoReconnect
            ? `Reconnecting to ${autoReconnect.lastKnownDeviceId?.substring(0, 10)}...`
            : 'Initializing Bluetooth...'}
        </Text>
      </View>
    );
  }

  if (autoReconnect.isAttemptingAutoReconnect) {
    return (
      <SafeAreaView style={s.container}>
        <View style={s.centeredMessageContainer}>
          <ActivityIndicator size="large" color={colors.primary} />
          <Text style={s.centeredMessageText}>
            Reconnecting to {autoReconnect.lastKnownDeviceId?.substring(0, 10)}...
          </Text>
          <TouchableOpacity style={[s.button, { backgroundColor: colors.danger, marginTop: 20 }]} onPress={autoReconnect.handleCancelAutoReconnect}>
            <Text style={s.buttonText}>Cancel</Text>
          </TouchableOpacity>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={s.container}>
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={Platform.OS === 'ios' ? 100 : 0}>
        <ScrollView contentContainerStyle={s.content} keyboardShouldPersistTaps="handled">
          <View style={s.titleRow}>
            <Text style={s.title}>Chronicle</Text>
            <Link href="/diagnostics" asChild>
              <TouchableOpacity style={s.diagButton}>
                <Text style={s.diagButtonText}>Logs</Text>
              </TouchableOpacity>
            </Link>
          </View>

          <BackendStatus backendUrl={settings.webSocketUrl} onBackendUrlChange={settings.handleSetAndSaveWebSocketUrl} jwtToken={settings.jwtToken} />
          <AuthSection backendUrl={settings.webSocketUrl} isAuthenticated={settings.isAuthenticated} currentUserEmail={settings.currentUserEmail} onAuthStatusChange={settings.handleAuthStatusChange} />

          {settings.isAuthenticated && <ObsidianIngest backendUrl={settings.webSocketUrl} jwtToken={settings.jwtToken} />}

          <PhoneAudioButton
            isRecording={phoneAudioRecorder.isRecording || orchestrator.isPhoneAudioMode}
            isInitializing={phoneAudioRecorder.isInitializing}
            isDisabled={!!deviceConnection.connectedDeviceId || deviceConnection.isConnecting}
            audioLevel={phoneAudioRecorder.audioLevel}
            error={phoneAudioRecorder.error}
            onPress={orchestrator.handleTogglePhoneAudio}
          />

          <BluetoothStatusBanner bluetoothState={bluetoothState} isPermissionsLoading={isPermissionsLoading} permissionGranted={permissionGranted} onRequestPermission={requestBluetoothPermission} />
          <ScanControls scanning={scanning} onScanPress={startScan} onStopScanPress={stopDeviceScanAction} canScan={canScan} />

          {!settings.isAuthenticated && (
            <View style={s.authWarning}>
              <Text style={s.authWarningText}>Login is required for advanced backend features. Simple backend can be used without authentication.</Text>
            </View>
          )}

          {scannedDevices.length > 0 && !deviceConnection.connectedDeviceId && !autoReconnect.isAttemptingAutoReconnect && (
            <View style={s.section}>
              <View style={s.sectionHeaderWithFilter}>
                <Text style={s.sectionTitle}>Found Devices</Text>
                <View style={s.filterContainer}>
                  <Text style={s.filterText}>Show only OMI/Friend</Text>
                  <Switch
                    trackColor={{ false: colors.disabled, true: colors.primary }}
                    thumbColor={showOnlyOmi ? colors.warning : colors.card}
                    onValueChange={setShowOnlyOmi}
                    value={showOnlyOmi}
                  />
                </View>
              </View>
              {filteredDevices.length > 0 ? (
                <FlatList
                  data={filteredDevices}
                  renderItem={({ item }) => (
                    <DeviceListItem device={item} onConnect={deviceConnection.connectToDevice} onDisconnect={deviceConnection.disconnectFromDevice} isConnecting={deviceConnection.isConnecting} connectedDeviceId={deviceConnection.connectedDeviceId} />
                  )}
                  keyExtractor={(item) => item.id}
                  style={{ maxHeight: 200 }}
                />
              ) : (
                <View style={s.noDevicesContainer}>
                  <Text style={s.noDevicesText}>
                    {showOnlyOmi ? `No OMI/Friend devices found. ${scannedDevices.length} other device(s) hidden by filter.` : 'No devices found.'}
                  </Text>
                </View>
              )}
            </View>
          )}

          {deviceConnection.connectedDeviceId && filteredDevices.find(d => d.id === deviceConnection.connectedDeviceId) && (
            <View style={s.section}>
              <Text style={s.sectionTitle}>Connected Device</Text>
              <DeviceListItem
                device={filteredDevices.find(d => d.id === deviceConnection.connectedDeviceId)!}
                onConnect={() => {}}
                onDisconnect={async () => {
                  await saveLastConnectedDeviceId(null);
                  autoReconnect.setLastKnownDeviceId(null);
                  autoReconnect.setTriedAutoReconnectForCurrentId(true);
                  try { await deviceConnection.disconnectFromDevice(); } catch { Alert.alert('Error', 'Failed to disconnect.'); }
                }}
                isConnecting={deviceConnection.isConnecting}
                connectedDeviceId={deviceConnection.connectedDeviceId}
              />
            </View>
          )}

          {deviceConnection.connectedDeviceId && !filteredDevices.find(d => d.id === deviceConnection.connectedDeviceId) && (
            <View style={s.section}>
              <View style={s.disconnectContainer}>
                <Text style={s.connectedText}>Connected to: {deviceConnection.connectedDeviceId.substring(0, 15)}...</Text>
                <TouchableOpacity
                  style={[s.button, { backgroundColor: colors.danger }]}
                  onPress={async () => {
                    await saveLastConnectedDeviceId(null);
                    autoReconnect.setLastKnownDeviceId(null);
                    autoReconnect.setTriedAutoReconnectForCurrentId(true);
                    try { await deviceConnection.disconnectFromDevice(); } catch { Alert.alert('Error', 'Failed to disconnect.'); }
                  }}
                  disabled={deviceConnection.isConnecting}
                >
                  <Text style={s.buttonText}>{deviceConnection.isConnecting ? 'Disconnecting...' : 'Disconnect'}</Text>
                </TouchableOpacity>
              </View>
            </View>
          )}

          {deviceConnection.connectedDeviceId && (
            <DeviceDetails
              connectedDeviceId={deviceConnection.connectedDeviceId}
              onGetAudioCodec={deviceConnection.getAudioCodec}
              currentCodec={deviceConnection.currentCodec}
              batteryLevel={batteryMonitor.batteryLevel}
              isLowBattery={batteryMonitor.isLowBattery}
              onRefreshBattery={batteryMonitor.refreshBattery}
              isListeningAudio={isOmiAudioListenerActive}
              onStartAudioListener={orchestrator.handleStartAudioListeningAndStreaming}
              onStopAudioListener={orchestrator.handleStopAudioListeningAndStreaming}
              audioPacketsReceived={audioPacketsReceived}
              webSocketUrl={settings.webSocketUrl}
              onSetWebSocketUrl={settings.handleSetAndSaveWebSocketUrl}
              isAudioStreaming={audioStreamer.isStreaming}
              isConnectingAudioStreamer={audioStreamer.isConnecting}
              audioStreamerError={audioStreamer.error}
              userId={settings.userId}
              onSetUserId={settings.handleSetAndSaveUserId}
              isAudioListenerRetrying={isAudioListenerRetrying}
              audioListenerRetryAttempts={audioListenerRetryAttempts}
            />
          )}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  content: {
    padding: 20,
    paddingTop: Platform.OS === 'android' ? 30 : 10,
    paddingBottom: 50,
  },
  titleRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
  },
  title: {
    fontSize: 24,
    fontWeight: 'bold',
    color: colors.text,
  },
  diagButton: {
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 6,
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
  },
  diagButtonText: {
    fontSize: 14,
    color: colors.textSecondary,
    fontWeight: '500',
  },
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: colors.card,
    borderRadius: 10,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.1,
    shadowRadius: 3,
    elevation: 2,
  },
  sectionHeaderWithFilter: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 15,
  },
  filterContainer: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  filterText: {
    marginRight: 8,
    fontSize: 14,
    color: colors.text,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    color: colors.text,
  },
  centeredMessageContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 20,
    backgroundColor: colors.background,
  },
  centeredMessageText: {
    marginTop: 10,
    fontSize: 16,
    color: colors.textSecondary,
    textAlign: 'center',
  },
  disconnectContainer: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: 5,
  },
  connectedText: {
    fontSize: 14,
    color: colors.text,
    flex: 1,
    marginRight: 10,
  },
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 8,
    paddingHorizontal: 12,
    borderRadius: 8,
    alignItems: 'center',
  },
  buttonText: {
    color: 'white',
    fontSize: 14,
    fontWeight: '600',
  },
  noDevicesContainer: {
    padding: 20,
    alignItems: 'center',
  },
  noDevicesText: {
    fontSize: 14,
    color: colors.textTertiary,
    textAlign: 'center',
    fontStyle: 'italic',
  },
  authWarning: {
    marginBottom: 20,
    padding: 15,
    backgroundColor: colors.inputBackground,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.warning,
  },
  authWarningText: {
    fontSize: 14,
    color: colors.warning,
    textAlign: 'center',
    fontWeight: '500',
  },
});
