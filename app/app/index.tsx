import React, { useRef, useCallback, useEffect, useState } from 'react';
import { Text, View, SafeAreaView, ScrollView, Platform, FlatList, ActivityIndicator, Alert, Switch, TouchableOpacity, KeyboardAvoidingView, StyleSheet, RefreshControl } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { State as BluetoothState } from 'react-native-ble-plx';
import { Link } from 'expo-router';
import Constants from 'expo-constants';
import { useTheme, ThemeColors } from '@/theme';

// Hooks
import { useBluetoothManager } from '@/hooks/useBluetoothManager';
import { useDeviceScanning } from '@/hooks/useDeviceScanning';
import { useDeviceConnection } from '@/hooks/useDeviceConnection';
import { useSharedAppSettings } from '@/contexts/AppSettingsContext';
import { useAutoReconnect } from '@/hooks/useAutoReconnect';
import { useAudioStreamingOrchestrator } from '@/hooks/useAudioStreamingOrchestrator';
import { useAudioListener } from '@/hooks/useAudioListener';
import { useAudioStreamer } from '@/hooks/useAudioStreamer';
import { usePhoneAudioRecorder } from '@/hooks/usePhoneAudioRecorder';
import { usePhoneAudioDevices } from '@/hooks/usePhoneAudioDevices';
import { useBatteryMonitor } from '@/hooks/useBatteryMonitor';
import { useBackendHealth, isNotConfigured } from '@/hooks/useBackendHealth';
import { saveLastConnectedDeviceId } from '@/utils/storage';

// Components
import BluetoothStatusBanner from '@/components/BluetoothStatusBanner';
import ScanControls from '@/components/ScanControls';
import DeviceListItem from '@/components/DeviceListItem';
import DeviceDetails from '@/components/DeviceDetails';
import PhoneAudioButton from '@/components/PhoneAudioButton';
import PhoneAudioMicPicker from '@/components/PhoneAudioMicPicker';

export default function App() {
  const { colors } = useTheme();
  const s = createStyles(colors);
  const omiConnection = useRef(new OmiConnection()).current;
  const [showOnlyOmi, setShowOnlyOmi] = useState(false);
  const [activeTab, setActiveTab] = useState<'backend' | 'connection'>('backend');

  // Bluetooth
  const { bleManager, bluetoothState, permissionGranted, requestBluetoothPermission, isPermissionsLoading } = useBluetoothManager();

  // Settings (must be before audioStreamer so the token refresh callback can reference it)
  const settings = useSharedAppSettings();

  // Live backend reachability (Connection Doctor), re-probed on pull-to-refresh.
  const { healthStatus, checkBackendHealth } = useBackendHealth(settings.webSocketUrl, settings.jwtToken);
  const [refreshing, setRefreshing] = useState(false);

  // Audio
  const audioStreamer = useAudioStreamer({
    autoReconnectEnabled: settings.autoReconnectEnabled,
    onTokenRefreshed: (newToken) => {
      // Update app-level auth state when auto-re-login refreshes the token
      if (settings.currentUserEmail) {
        settings.handleAuthStatusChange(true, settings.currentUserEmail, newToken);
      }
    },
  });
  const phoneAudioRecorder = usePhoneAudioRecorder();
  const phoneAudioDevices = usePhoneAudioDevices();

  const { isListeningAudio: isOmiAudioListenerActive, audioPacketsReceived, startAudioListener: originalStartAudioListener, stopAudioListener: originalStopAudioListener, isRetrying: isAudioListenerRetrying, retryAttempts: audioListenerRetryAttempts } = useAudioListener(omiConnection, () => !!deviceConnection.connectedDeviceId);

  // Refs for disconnect cleanup
  const isOmiAudioListenerActiveRef = useRef(isOmiAudioListenerActive);
  const isAudioStreamingRef = useRef(audioStreamer.isStreaming);
  // Track if audio pipeline was active before BLE disconnect (for auto-restart on reconnect)
  const wasStreamingBeforeDisconnectRef = useRef(false);
  useEffect(() => { isOmiAudioListenerActiveRef.current = isOmiAudioListenerActive; }, [isOmiAudioListenerActive]);
  useEffect(() => { isAudioStreamingRef.current = audioStreamer.isStreaming; }, [audioStreamer.isStreaming]);

  // Refs to break the declaration-order cycle:
  // onDeviceConnect/onDeviceDisconnect need orchestrator + autoReconnect,
  // but deviceConnection (which needs those callbacks) must be declared
  // before orchestrator and autoReconnect.
  type OrchestratorHandle = ReturnType<typeof useAudioStreamingOrchestrator>;
  type AutoReconnectHandle = ReturnType<typeof useAutoReconnect>;
  const orchestratorRef = useRef<OrchestratorHandle | null>(null);
  const autoReconnectRef = useRef<AutoReconnectHandle | null>(null);

  // Device callbacks
  const onDeviceConnect = useCallback(async () => {
    const deviceIdToSave = omiConnection.connectedDeviceId;
    if (deviceIdToSave) {
      await saveLastConnectedDeviceId(deviceIdToSave);
      autoReconnectRef.current?.setLastKnownDeviceId(deviceIdToSave);
      autoReconnectRef.current?.setTriedAutoReconnectForCurrentId(false);
    }

    // Auto-restart audio pipeline if it was active before BLE disconnect
    if (wasStreamingBeforeDisconnectRef.current) {
      wasStreamingBeforeDisconnectRef.current = false;
      console.log('[App] BLE reconnected — auto-restarting audio pipeline');
      // Short delay to let BLE connection stabilize
      setTimeout(() => {
        orchestratorRef.current?.handleStartAudioListeningAndStreaming().catch(err => {
          console.error('[App] Failed to auto-restart audio pipeline:', err);
        });
      }, 1000);
    }
  }, [omiConnection]);

  const onDeviceDisconnect = useCallback(async () => {
    // Remember if audio was active so we can auto-restart on reconnect
    if (isOmiAudioListenerActiveRef.current || isAudioStreamingRef.current) {
      wasStreamingBeforeDisconnectRef.current = true;
    }

    // Stop audio listener (BLE is gone, can't read audio)
    if (isOmiAudioListenerActiveRef.current) await originalStopAudioListener();

    // Keep WebSocket alive — it will reconnect or idle until BLE comes back.
    // Only stop WebSocket for phone audio mode (no BLE needed there).
    if (phoneAudioRecorder.isRecording) {
      audioStreamer.stopStreaming();
      await phoneAudioRecorder.stopRecording();
      orchestratorRef.current?.setIsPhoneAudioMode(false);
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
    autoReconnectEnabled: settings.autoReconnectEnabled,
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
    resolvePhoneInputDeviceId: phoneAudioDevices.resolveEffectiveDeviceId,
    settings,
  });

  // Keep forward-declared refs in sync so device callbacks can call through.
  orchestratorRef.current = orchestrator;
  autoReconnectRef.current = autoReconnect;

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
    !autoReconnect.isAttemptingAutoReconnect && !autoReconnect.isRetryingConnection &&
    !deviceConnection.isConnecting &&
    !deviceConnection.connectedDeviceId &&
    (autoReconnect.triedAutoReconnectForCurrentId || !autoReconnect.lastKnownDeviceId)
  ), [permissionGranted, bluetoothState, autoReconnect.isAttemptingAutoReconnect, autoReconnect.isRetryingConnection, deviceConnection.isConnecting, deviceConnection.connectedDeviceId, autoReconnect.triedAutoReconnectForCurrentId, autoReconnect.lastKnownDeviceId]);

  const filteredDevices = React.useMemo(() => {
    if (!showOnlyOmi) return scannedDevices;
    return scannedDevices.filter(d => {
      const name = d.name?.toLowerCase() || '';
      return name.includes('omi') || name.includes('friend') || name.includes('neo') || name.includes('elato');
    });
  }, [scannedDevices, showOnlyOmi]);

  // Pull-to-refresh: re-probe backend reachability and refresh live device state.
  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await Promise.all([
        checkBackendHealth(false),
        deviceConnection.connectedDeviceId ? batteryMonitor.refreshBattery() : Promise.resolve(),
        phoneAudioDevices.refresh().then(() => {}, () => {}),
      ]);
    } finally {
      setRefreshing(false);
    }
  }, [checkBackendHealth, deviceConnection.connectedDeviceId, batteryMonitor.refreshBattery, phoneAudioDevices.refresh]);

  const bluetoothReady = bluetoothState === BluetoothState.PoweredOn && permissionGranted;
  // A fresh install points at localhost (the phone itself), which can never be a
  // real backend — treat that (and empty) as "not paired yet" so the setup card
  // and health pill reflect reality.
  const backendConfigured = !isNotConfigured(settings.webSocketUrl);
  // The pill reflects the live probe, not just config: a confirmed-bad probe
  // (offline / unreachable / down / unhealthy) turns it red; pending or healthy
  // probes fall back to the config+bluetooth view.
  const backendDown = ['offline', 'backend_down', 'unreachable', 'unhealthy'].includes(healthStatus.status);
  const isOperational = bluetoothReady && backendConfigured && !backendDown;
  const healthLabel = backendDown
    ? (healthStatus.status === 'offline' ? "You're Offline" : 'Backend Unreachable')
    : isOperational ? 'System Operational' : 'Action Needed';
  const healthTone = backendDown ? colors.danger : isOperational ? colors.success : colors.warning;
  const batteryDisplay = deviceConnection.connectedDeviceId
    ? batteryMonitor.batteryLevel >= 0 ? `${batteryMonitor.batteryLevel}%` : '...'
    : '--';
  const streamDisplay = audioStreamer.isStreaming
    ? 'Streaming'
    : (phoneAudioRecorder.isRecording || orchestrator.isPhoneAudioMode)
      ? 'Phone Mic'
      : 'Idle';

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

  return (
    <SafeAreaView style={s.container}>
      <View style={s.pulseBackground} />
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={Platform.OS === 'ios' ? 100 : 0}>
        <ScrollView
          contentContainerStyle={s.content}
          keyboardShouldPersistTaps="handled"
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor={colors.primary}
              colors={[colors.primary]}
              progressBackgroundColor={colors.card}
            />
          }
        >
          <View style={s.headerCard}>
            <View style={s.titleRow}>
              <View style={s.brandRow}>
                <Text style={s.title}>Chronicle</Text>
                <Text style={s.versionText}>v{Constants.expoConfig?.version ?? ''}</Text>
              </View>
              <View style={s.headerActions}>
                <Link href="/diagnostics" asChild>
                  <TouchableOpacity style={s.diagButton}>
                    <Text style={s.diagButtonText}>Diagnostics</Text>
                  </TouchableOpacity>
                </Link>
                <Link href="/settings" asChild>
                  <TouchableOpacity style={s.gearButton} accessibilityLabel="Settings">
                    <Text style={s.gearButtonText}>⚙</Text>
                  </TouchableOpacity>
                </Link>
              </View>
            </View>

            <View style={[s.healthPill, { borderColor: healthTone }]}>
              <View style={[s.healthDot, { backgroundColor: healthTone }]} />
              <Text style={[s.healthText, { color: healthTone }]}>{healthLabel}</Text>
            </View>
          </View>

          <View style={s.heroCard}>
            <Text style={s.heroTitle}>{activeTab === 'backend' ? 'Backend Dashboard' : 'Connection Center'}</Text>
            <Text style={s.heroSubtitle}>
              {activeTab === 'backend'
                ? 'Control center for backend, audio streaming, and wakeword behavior.'
                : 'Manage Bluetooth pairing, reconnect flow, and device audio routing.'}
            </Text>
            <View style={s.heroMetricsRow}>
              <View style={s.metricBlock}>
                <Text style={s.metricValue}>{streamDisplay}</Text>
                <Text style={s.metricLabel}>Audio State</Text>
              </View>
              <View style={s.metricDivider} />
              <View style={s.metricBlock}>
                <Text style={s.metricValue}>{batteryDisplay}</Text>
                <Text style={s.metricLabel}>Battery</Text>
              </View>
              <View style={s.metricDivider} />
              <View style={s.metricBlock}>
                <Text style={s.metricValue}>{deviceConnection.connectedDeviceId ? 'Connected' : 'Idle'}</Text>
                <Text style={s.metricLabel}>Device</Text>
              </View>
            </View>
          </View>

          {activeTab === 'backend' && (
            <>
              {(!backendConfigured || !settings.isAuthenticated) && (
                <Link href="/settings" asChild>
                  <TouchableOpacity style={s.setupCard}>
                    <Text style={s.setupTitle}>
                      {!backendConfigured ? 'Connect to your backend' : 'Sign in to your backend'}
                    </Text>
                    <Text style={s.setupSubtitle}>
                      {!backendConfigured
                        ? 'Scan the QR code from your Chronicle dashboard to pair this app.'
                        : 'Log in to access advanced backend features.'}
                    </Text>
                    <Text style={s.setupCta}>Open Settings ⚙</Text>
                  </TouchableOpacity>
                </Link>
              )}

              <Text style={s.sectionLabel}>Audio Deck</Text>
              <PhoneAudioButton
                isRecording={phoneAudioRecorder.isRecording || orchestrator.isPhoneAudioMode}
                isInitializing={phoneAudioRecorder.isInitializing}
                isDisabled={!!deviceConnection.connectedDeviceId || deviceConnection.isConnecting}
                audioLevel={phoneAudioRecorder.audioLevel}
                error={phoneAudioRecorder.error}
                onPress={orchestrator.handleTogglePhoneAudio}
              />

              {!deviceConnection.connectedDeviceId && !deviceConnection.isConnecting && (
                <PhoneAudioMicPicker
                  devices={phoneAudioDevices.devices}
                  selectedDeviceId={phoneAudioDevices.selectedDeviceId}
                  effectiveDevice={phoneAudioDevices.effectiveDevice}
                  loading={phoneAudioDevices.loading}
                  disabled={phoneAudioRecorder.isRecording || orchestrator.isPhoneAudioMode}
                  onSelect={phoneAudioDevices.setSelectedDeviceId}
                  onRefresh={phoneAudioDevices.refresh}
                />
              )}
            </>
          )}

          {activeTab === 'connection' && (
            <>
              <Text style={s.sectionLabel}>Bluetooth</Text>
              <BluetoothStatusBanner bluetoothState={bluetoothState} isPermissionsLoading={isPermissionsLoading} permissionGranted={permissionGranted} onRequestPermission={requestBluetoothPermission} />
              <ScanControls scanning={scanning} onScanPress={startScan} onStopScanPress={stopDeviceScanAction} canScan={canScan} />

          {(autoReconnect.isAttemptingAutoReconnect || autoReconnect.isRetryingConnection) && (
            <View style={s.retryBanner}>
              <ActivityIndicator size="small" color={colors.warning} />
              <Text style={s.retryBannerText}>
                {autoReconnect.isRetryingConnection
                  ? `Reconnecting in ${autoReconnect.retryBackoffSeconds}s... (attempt ${autoReconnect.connectionRetryCount})`
                  : `Reconnecting to ${autoReconnect.lastKnownDeviceId?.substring(0, 10) ?? 'device'}...`}
              </Text>
              <TouchableOpacity
                style={[s.button, { backgroundColor: colors.danger, paddingVertical: 6, paddingHorizontal: 10 }]}
                onPress={autoReconnect.handleCancelAutoReconnect}
              >
                <Text style={s.buttonText}>Cancel</Text>
              </TouchableOpacity>
            </View>
          )}

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
                  <Text style={s.filterText}>Show only OMI/Friend/Neo/Elato</Text>
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
                    {showOnlyOmi ? `No OMI/Friend/Neo/Elato devices found. ${scannedDevices.length} other device(s) hidden by filter.` : 'No devices found.'}
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
                  autoReconnectEnabled={settings.autoReconnectEnabled}
                  onToggleAutoReconnect={settings.handleToggleAutoReconnect}
                />
              )}
            </>
          )}
        </ScrollView>
      </KeyboardAvoidingView>
      <View style={s.bottomNav}>
        <TouchableOpacity
          style={activeTab === 'connection' ? s.navItemActive : s.navItem}
          onPress={() => setActiveTab('connection')}
        >
          <Text style={activeTab === 'connection' ? s.navItemActiveText : s.navItemText}>Connection</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={activeTab === 'backend' ? s.navItemActive : s.navItem}
          onPress={() => setActiveTab('backend')}
        >
          <Text style={activeTab === 'backend' ? s.navItemActiveText : s.navItemText}>Backend</Text>
        </TouchableOpacity>
      </View>
    </SafeAreaView>
  );
}

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  pulseBackground: {
    position: 'absolute',
    width: 440,
    height: 440,
    borderRadius: 220,
    backgroundColor: colors.primary,
    opacity: 0.05,
    top: -140,
    right: -140,
  },
  content: {
    padding: 16,
    paddingTop: Platform.OS === 'android' ? 30 : 10,
    paddingBottom: 110,
  },
  headerCard: {
    marginBottom: 14,
    padding: 14,
    borderRadius: 14,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.cardBorder,
  },
  titleRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 10,
  },
  brandRow: {
    flexDirection: 'row',
    alignItems: 'baseline',
  },
  title: {
    fontSize: 26,
    fontWeight: 'bold',
    color: colors.text,
  },
  versionText: {
    fontSize: 12,
    color: colors.textTertiary,
    marginLeft: 8,
  },
  headerActions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  diagButton: {
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 10,
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
  },
  diagButtonText: {
    fontSize: 12,
    color: colors.primary,
    fontWeight: '700',
  },
  gearButton: {
    width: 34,
    height: 34,
    borderRadius: 10,
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    alignItems: 'center',
    justifyContent: 'center',
  },
  gearButtonText: {
    fontSize: 16,
    color: colors.primary,
    fontWeight: '700',
  },
  setupCard: {
    marginBottom: 20,
    padding: 16,
    borderRadius: 12,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.primary,
  },
  setupTitle: {
    fontSize: 16,
    fontWeight: '700',
    color: colors.text,
  },
  setupSubtitle: {
    marginTop: 6,
    fontSize: 13,
    color: colors.textSecondary,
  },
  setupCta: {
    marginTop: 12,
    fontSize: 14,
    fontWeight: '700',
    color: colors.primary,
  },
  healthPill: {
    alignSelf: 'flex-start',
    flexDirection: 'row',
    alignItems: 'center',
    borderWidth: 1,
    borderRadius: 16,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  healthDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    marginRight: 8,
  },
  healthText: {
    fontSize: 12,
    fontWeight: '700',
  },
  heroCard: {
    marginBottom: 16,
    padding: 14,
    borderRadius: 14,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.cardBorder,
  },
  heroTitle: {
    fontSize: 20,
    fontWeight: '700',
    color: colors.text,
  },
  heroSubtitle: {
    marginTop: 4,
    fontSize: 13,
    color: colors.textSecondary,
  },
  heroMetricsRow: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: 14,
    paddingTop: 12,
    borderTopWidth: 1,
    borderTopColor: colors.separator,
  },
  metricBlock: {
    flex: 1,
    alignItems: 'center',
  },
  metricValue: {
    fontSize: 14,
    fontWeight: '700',
    color: colors.text,
  },
  metricLabel: {
    marginTop: 4,
    fontSize: 11,
    color: colors.textTertiary,
    textTransform: 'uppercase',
    letterSpacing: 0.6,
  },
  metricDivider: {
    width: 1,
    height: 28,
    backgroundColor: colors.separator,
    marginHorizontal: 4,
  },
  sectionLabel: {
    fontSize: 12,
    color: colors.textTertiary,
    textTransform: 'uppercase',
    letterSpacing: 0.8,
    marginBottom: 8,
    marginTop: 2,
    fontWeight: '700',
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
    marginBottom: 15,
  },
  filterContainer: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginTop: 8,
  },
  filterText: {
    marginRight: 8,
    fontSize: 14,
    color: colors.text,
    flexShrink: 1,
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
  retryBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 12,
    marginBottom: 15,
    backgroundColor: colors.card,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.warning,
  },
  retryBannerText: {
    flex: 1,
    marginLeft: 10,
    fontSize: 14,
    color: colors.warning,
    fontWeight: '500',
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
  bottomNav: {
    position: 'absolute',
    left: 14,
    right: 14,
    bottom: 14,
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderRadius: 14,
    padding: 8,
  },
  navItem: {
    flex: 1,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 10,
  },
  navItemText: {
    fontSize: 12,
    color: colors.textSecondary,
    fontWeight: '700',
  },
  navItemActive: {
    flex: 1,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 10,
    backgroundColor: colors.primary,
  },
  navItemActiveText: {
    fontSize: 12,
    color: 'white',
    fontWeight: '700',
  },
});
