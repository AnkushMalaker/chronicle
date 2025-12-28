import React, { useRef, useCallback, useEffect, useState } from 'react';
import { StyleSheet, Text, View, SafeAreaView, ScrollView, Platform, ActivityIndicator, Button, KeyboardAvoidingView, TouchableOpacity } from 'react-native';
import { OmiConnection } from 'friend-lite-react-native';
import { State as BluetoothState } from 'react-native-ble-plx';
import NetInfo from '@react-native-community/netinfo';

// Hooks
import { useBluetoothManager } from './hooks/useBluetoothManager';
import { useDeviceScanning } from './hooks/useDeviceScanning';
import { useDeviceConnection } from './hooks/useDeviceConnection';
import { useAudioListener } from './hooks/useAudioListener';
import { useAudioStreamer } from './hooks/useAudioStreamer';
import { usePhoneAudioRecorder } from './hooks/usePhoneAudioRecorder';
import { useAutoReconnect } from './hooks/useAutoReconnect';
import { useAudioManager } from './hooks/useAudioManager';
import { useTokenMonitor } from './hooks/useTokenMonitor';
import { useConnectionMonitor } from './hooks/useConnectionMonitor';
import { useConnectionLog } from './hooks/useConnectionLog';
import { useOfflineMode } from './hooks/useOfflineMode';
import { useBackgroundRecorder } from './hooks/useBackgroundRecorder';

// Services
import { handleReconnection, SyncProgress } from './services/offlineSync';
import { registerNotificationHandler } from './services/backgroundRecorder';
import {
  saveWebSocketUrl,
  getWebSocketUrl,
  saveUserId,
  getUserId,
  getAuthEmail,
  getJwtToken,
  clearAuthData,
} from './utils/storage';

// Components
import BluetoothStatusBanner from './components/BluetoothStatusBanner';
import ScanControls from './components/ScanControls';
import PhoneAudioButton from './components/PhoneAudioButton';
import DeviceList from './components/DeviceList';
import ConnectedDevice from './components/ConnectedDevice';
import SettingsPanel from './components/SettingsPanel';
import ConnectionStatusBanner from './components/ConnectionStatusBanner';
import ConnectionLogViewer from './components/ConnectionLogViewer';
import { OfflineBanner } from './components/OfflineBanner';
import theme from './theme/design-system';

export default function App() {
  // Initialize OmiConnection
  const omiConnection = useRef(new OmiConnection()).current;

  // WebSocket URL and User ID state
  const [webSocketUrl, setWebSocketUrl] = useState<string>('');
  const [userId, setUserId] = useState<string>('');

  // Authentication state
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [currentUserEmail, setCurrentUserEmail] = useState<string | null>(null);
  const [jwtToken, setJwtToken] = useState<string | null>(null);

  // Offline mode state
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);

  // Offline mode hook
  const offlineMode = useOfflineMode();

  // Background recorder (Android foreground service)
  useBackgroundRecorder({
    isOffline: offlineMode.isOffline,
    isBuffering: offlineMode.isBuffering,
    currentBufferDurationMs: offlineMode.currentBufferDurationMs,
    pendingSegmentCount: offlineMode.pendingSegments.length,
    onStopRequested: () => {
      // Handle stop from notification - this will trigger offline mode exit
      console.log('[App] Stop recording requested from notification');
    },
  });

  // Register notification handler for Android foreground service
  useEffect(() => {
    const unsubscribe = registerNotificationHandler();
    return () => {
      unsubscribe();
    };
  }, []);

  // Token expiration monitoring
  const handleTokenExpired = useCallback(async () => {
    console.log('[App] Token expired - logging out user');
    await clearAuthData();
    setIsAuthenticated(false);
    setCurrentUserEmail(null);
    setJwtToken(null);
  }, []);

  const { isTokenValid, tokenExpiresAt, minutesUntilExpiration } = useTokenMonitor({
    jwtToken,
    onTokenExpired: handleTokenExpired,
  });

  // Bluetooth Management
  const {
    bleManager,
    bluetoothState,
    permissionGranted,
    requestBluetoothPermission,
    isPermissionsLoading,
  } = useBluetoothManager();

  // Audio Hooks
  const audioStreamer = useAudioStreamer();
  const phoneAudioRecorder = usePhoneAudioRecorder();

  // Refs to break circular dependencies and handle cleanup
  const autoReconnectRef = useRef<ReturnType<typeof useAutoReconnect>>();

  const {
    isListeningAudio: isOmiAudioListenerActive,
    audioPacketsReceived,
    startAudioListener: originalStartAudioListener,
    stopAudioListener: originalStopAudioListener,
    isRetrying: isAudioListenerRetrying,
    retryAttempts: audioListenerRetryAttempts,
  } = useAudioListener(
    omiConnection,
    () => !!deviceConnection.connectedDeviceId
  );

  // Device Connection Callbacks
  const onDeviceConnect = useCallback(async () => {
    console.log('[App] Device connected');
    const deviceId = omiConnection.connectedDeviceId;
    if (deviceId && autoReconnectRef.current) {
      await autoReconnectRef.current.saveConnectedDevice(deviceId);
    }
  }, [omiConnection]);

  const onDeviceDisconnect = useCallback(async () => {
    console.log('[App] Device disconnected');
    // Stop all audio streaming
    if (isOmiAudioListenerActive) {
      await originalStopAudioListener();
    }
    if (audioStreamer.isStreaming) {
      audioStreamer.stopStreaming();
    }
    if (phoneAudioRecorder.isRecording) {
      await phoneAudioRecorder.stopRecording();
    }
  }, [
    isOmiAudioListenerActive,
    originalStopAudioListener,
    audioStreamer,
    phoneAudioRecorder,
  ]);

  // Device Connection Management
  const deviceConnection = useDeviceConnection(
    omiConnection,
    onDeviceDisconnect,
    onDeviceConnect
  );

  // Device Scanning (needs to be before autoReconnect)
  const {
    devices: scannedDevices,
    scanning,
    startScan,
    stopScan: stopDeviceScanAction,
  } = useDeviceScanning(
    bleManager,
    omiConnection,
    permissionGranted,
    bluetoothState === BluetoothState.PoweredOn,
    requestBluetoothPermission
  );

  // Auto-Reconnect Management (now has correct scanning state)
  const autoReconnect = useAutoReconnect({
    bluetoothState,
    permissionGranted,
    connectedDeviceId: deviceConnection.connectedDeviceId,
    isConnecting: deviceConnection.isConnecting,
    scanning,
    connectToDevice: deviceConnection.connectToDevice,
  });

  // Update ref for circular dependency
  autoReconnectRef.current = autoReconnect;

  // Audio Streaming Management with offline support
  const audioManager = useAudioManager({
    webSocketUrl,
    userId,
    jwtToken,
    isAuthenticated,
    omiConnection,
    connectedDeviceId: deviceConnection.connectedDeviceId,
    audioStreamer,
    phoneAudioRecorder,
    startAudioListener: originalStartAudioListener,
    stopAudioListener: originalStopAudioListener,
    offlineMode,
    connectionHandlers: {
      onWebSocketDisconnect: (sessionId, conversationId) => {
        connectionLog.logEvent(
          'websocket',
          'disconnected',
          'Entered offline mode',
          `Session: ${sessionId}`
        );
      },
      onWebSocketReconnect: () => {
        connectionLog.logEvent('websocket', 'connected', 'Exited offline mode');
        // Trigger sync after reconnection
        handleSyncOfflineSegments();
      },
    },
  });

  // Connection Health Monitoring
  const connectionMonitor = useConnectionMonitor({
    connectedDeviceId: deviceConnection.connectedDeviceId,
    bleManager,
    isAudioStreaming: audioStreamer.isStreaming,
    webSocketReadyState: audioStreamer.getWebSocketReadyState?.(),
  });

  // Connection Logging
  const connectionLog = useConnectionLog();
  const [isLogsVisible, setIsLogsVisible] = useState(false);

  // Sync pending offline segments
  const handleSyncOfflineSegments = useCallback(async () => {
    if (!jwtToken || !webSocketUrl || syncProgress?.inProgress) return;

    // Convert WebSocket URL to HTTP URL for API calls
    const baseUrl = webSocketUrl
      .replace(/^ws:/, 'http:')
      .replace(/^wss:/, 'https:')
      .replace(/\/ws.*$/, '');

    console.log('[App] Starting offline sync to', baseUrl);
    connectionLog.logEvent('server', 'connecting', 'Syncing offline segments');

    const result = await handleReconnection(
      baseUrl,
      jwtToken,
      offlineMode.lastActiveConversationId,
      (progress) => setSyncProgress(progress)
    );

    if (result.action === 'upload_as_new' && result.syncResult) {
      if (result.syncResult.success) {
        connectionLog.logEvent(
          'server',
          'connected',
          `Synced ${result.syncResult.uploaded} segments`
        );
      } else {
        connectionLog.logEvent(
          'server',
          'error',
          `Sync failed: ${result.syncResult.failed} segments`,
          result.syncResult.errors.join(', ')
        );
      }
    } else if (result.action === 'resume') {
      connectionLog.logEvent(
        'server',
        'connected',
        'Resuming active conversation',
        result.conversationId
      );
    }

    // Refresh offline mode stats
    await offlineMode.refreshPendingSegments();
    await offlineMode.refreshStats();
    setSyncProgress(null);
  }, [jwtToken, webSocketUrl, syncProgress, offlineMode, connectionLog]);

  // Log network connectivity changes
  useEffect(() => {
    if (connectionLog.isLoading) return;

    const unsubscribe = NetInfo.addEventListener(state => {
      if (state.isConnected === true && state.isInternetReachable === true) {
        connectionLog.logEvent(
          'network',
          'connected',
          'Network connected',
          `Type: ${state.type}`
        );
      } else if (state.isConnected === false) {
        connectionLog.logEvent('network', 'disconnected', 'Network disconnected');
      } else if (state.isInternetReachable === false) {
        connectionLog.logEvent(
          'network',
          'error',
          'No internet access',
          'Connected to network but cannot reach internet'
        );
      }
    });

    return () => unsubscribe();
  }, [connectionLog.isLoading]);

  // Log Bluetooth state changes
  useEffect(() => {
    if (connectionLog.isLoading) return;

    const stateMap: Record<BluetoothState, { status: 'connected' | 'disconnected' | 'connecting' | 'error' | 'unknown'; message: string }> = {
      [BluetoothState.PoweredOn]: { status: 'connected', message: 'Bluetooth powered on' },
      [BluetoothState.PoweredOff]: { status: 'disconnected', message: 'Bluetooth powered off' },
      [BluetoothState.Resetting]: { status: 'connecting', message: 'Bluetooth resetting' },
      [BluetoothState.Unauthorized]: { status: 'error', message: 'Bluetooth unauthorized' },
      [BluetoothState.Unsupported]: { status: 'error', message: 'Bluetooth unsupported' },
      [BluetoothState.Unknown]: { status: 'unknown', message: 'Bluetooth state unknown' },
    };

    const stateInfo = stateMap[bluetoothState];
    if (stateInfo) {
      connectionLog.logEvent('bluetooth', stateInfo.status, stateInfo.message);
    }
  }, [bluetoothState, connectionLog.isLoading]);

  // Log device connection changes
  useEffect(() => {
    if (connectionLog.isLoading) return;

    if (deviceConnection.connectedDeviceId) {
      connectionLog.logEvent(
        'bluetooth',
        'connected',
        'OMI device connected',
        `Device ID: ${deviceConnection.connectedDeviceId}`
      );
    } else if (!deviceConnection.isConnecting) {
      connectionLog.logEvent('bluetooth', 'disconnected', 'OMI device disconnected');
    }
  }, [deviceConnection.connectedDeviceId, connectionLog.isLoading]);

  // Log WebSocket streaming changes
  useEffect(() => {
    if (connectionLog.isLoading) return;

    if (audioStreamer.isStreaming) {
      connectionLog.logEvent('websocket', 'connected', 'Audio streaming started', webSocketUrl);
    } else if (audioStreamer.error) {
      connectionLog.logEvent('websocket', 'error', 'Audio streaming error', audioStreamer.error);
    } else {
      connectionLog.logEvent('websocket', 'disconnected', 'Audio streaming stopped');
    }
  }, [audioStreamer.isStreaming, audioStreamer.error, connectionLog.isLoading]);

  // Log server connection from connection monitor
  useEffect(() => {
    if (connectionLog.isLoading) return;

    const statusMap: Record<string, { status: 'connected' | 'disconnected' | 'connecting' | 'error'; message: string }> = {
      connected: { status: 'connected', message: 'Backend server connected' },
      connecting: { status: 'connecting', message: 'Connecting to backend server' },
      disconnected: { status: 'disconnected', message: 'Backend server disconnected' },
      error: { status: 'error', message: 'Backend server connection error' },
    };

    const statusInfo = statusMap[connectionMonitor.webSocketHealth];
    if (statusInfo) {
      connectionLog.logEvent('server', statusInfo.status, statusInfo.message);
    }
  }, [connectionMonitor.webSocketHealth, connectionLog.isLoading]);

  // Load settings on mount
  useEffect(() => {
    const loadSettings = async () => {
      // Initialize offline storage
      await offlineMode.initialize();

      // Load WebSocket URL
      const storedWsUrl = await getWebSocketUrl();
      if (storedWsUrl) {
        setWebSocketUrl(storedWsUrl);
      } else {
        const defaultUrl = 'ws://localhost:8000/ws';
        setWebSocketUrl(defaultUrl);
        await saveWebSocketUrl(defaultUrl);
      }

      // Load User ID
      const storedUserId = await getUserId();
      if (storedUserId) {
        setUserId(storedUserId);
      }

      // Load authentication data
      const storedEmail = await getAuthEmail();
      const storedToken = await getJwtToken();
      if (storedEmail && storedToken) {
        setCurrentUserEmail(storedEmail);
        setJwtToken(storedToken);
        setIsAuthenticated(true);
      }
    };
    loadSettings();
  }, []);

  // Store latest references for cleanup
  const cleanupRefs = useRef({
    deviceConnection,
    bleManager,
    audioStreamer,
    phoneAudioRecorder,
    offlineMode,
  });

  // Update refs when values change
  useEffect(() => {
    cleanupRefs.current = {
      deviceConnection,
      bleManager,
      audioStreamer,
      phoneAudioRecorder,
      offlineMode,
    };
  });

  // Cleanup on unmount with current refs
  useEffect(() => {
    return () => {
      console.log('App unmounting - cleaning up');
      const refs = cleanupRefs.current;

      if (omiConnection.isConnected()) {
        refs.deviceConnection.disconnectFromDevice().catch(err =>
          console.error("Error disconnecting:", err)
        );
      }
      if (refs.bleManager) {
        refs.bleManager.destroy();
      }
      refs.audioStreamer.stopStreaming();
      refs.phoneAudioRecorder.stopRecording().catch(err =>
        console.error("Error stopping phone audio:", err)
      );
      // Cleanup offline storage
      refs.offlineMode.cleanup().catch(err =>
        console.error("Error cleaning up offline storage:", err)
      );
    };
  }, [omiConnection]);

  // Handlers for settings changes
  const handleSetAndSaveWebSocketUrl = useCallback(async (url: string) => {
    setWebSocketUrl(url);
    await saveWebSocketUrl(url);
  }, []);

  const handleSetAndSaveUserId = useCallback(async (id: string) => {
    setUserId(id);
    await saveUserId(id || null);
  }, []);

  const handleAuthStatusChange = useCallback((
    authenticated: boolean,
    email: string | null,
    token: string | null
  ) => {
    setIsAuthenticated(authenticated);
    setCurrentUserEmail(email);
    setJwtToken(token);
  }, []);

  // Determine if scanning is allowed
  const canScan = React.useMemo(() => (
    permissionGranted &&
    bluetoothState === BluetoothState.PoweredOn &&
    !autoReconnect.isAttemptingAutoReconnect &&
    !deviceConnection.isConnecting &&
    !deviceConnection.connectedDeviceId &&
    (autoReconnect.triedAutoReconnectForCurrentId || !autoReconnect.lastKnownDeviceId)
  ), [
    permissionGranted,
    bluetoothState,
    autoReconnect.isAttemptingAutoReconnect,
    autoReconnect.triedAutoReconnectForCurrentId,
    autoReconnect.lastKnownDeviceId,
    deviceConnection.isConnecting,
    deviceConnection.connectedDeviceId,
  ]);

  // Get device object if connected
  const connectedDevice = React.useMemo(() => {
    if (!deviceConnection.connectedDeviceId) return undefined;
    return scannedDevices.find(d => d.id === deviceConnection.connectedDeviceId);
  }, [deviceConnection.connectedDeviceId, scannedDevices]);

  // Loading screen during permissions
  if (isPermissionsLoading && bluetoothState === BluetoothState.Unknown) {
    return (
      <View style={styles.centeredMessageContainer}>
        <ActivityIndicator size="large" />
        <Text style={styles.centeredMessageText}>
          {autoReconnect.isAttemptingAutoReconnect
            ? `Attempting to reconnect to last device...`
            : 'Initializing Bluetooth...'}
        </Text>
      </View>
    );
  }

  // Auto-reconnect screen
  if (autoReconnect.isAttemptingAutoReconnect) {
    return (
      <SafeAreaView style={styles.container}>
        <View style={styles.centeredMessageContainer}>
          <ActivityIndicator size="large" />
          <Text style={styles.centeredMessageText}>
            Attempting to reconnect to last device...
          </Text>
          <Button
            title="Cancel"
            onPress={autoReconnect.cancelAutoReconnect}
            color={theme.colors.error.main}
          />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.container}>
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={Platform.OS === 'ios' ? 100 : 0}
      >
        <ScrollView
          contentContainerStyle={styles.content}
          keyboardShouldPersistTaps="handled"
        >
          {/* Header with Logs button */}
          <View style={styles.header}>
            <View style={styles.headerSpacer} />
            <Text style={styles.title}>Chronicle</Text>
            <TouchableOpacity
              style={styles.logsButton}
              onPress={() => setIsLogsVisible(true)}
              testID="open-logs-button"
            >
              <Text style={styles.logsButtonText}>Logs</Text>
            </TouchableOpacity>
          </View>

          {/* Connection Health Banner */}
          <ConnectionStatusBanner
            bluetoothHealth={connectionMonitor.bluetoothHealth}
            webSocketHealth={connectionMonitor.webSocketHealth}
            minutesUntilTokenExpiration={minutesUntilExpiration}
          />

          {/* Offline Mode Banner */}
          <OfflineBanner
            visible={offlineMode.isOffline || offlineMode.pendingSegments.length > 0}
            isBuffering={offlineMode.isBuffering}
            bufferDurationMs={offlineMode.currentBufferDurationMs}
            pendingSegments={offlineMode.pendingSegments}
            stats={offlineMode.stats}
            storageWarning={offlineMode.storageWarning}
            syncProgress={syncProgress}
            onSyncPress={handleSyncOfflineSegments}
          />

          {/* Settings Panel */}
          <SettingsPanel
            backendUrl={webSocketUrl}
            onBackendUrlChange={handleSetAndSaveWebSocketUrl}
            jwtToken={jwtToken}
            isAuthenticated={isAuthenticated}
            currentUserEmail={currentUserEmail}
            onAuthStatusChange={handleAuthStatusChange}
          />

          {/* Phone Audio Button */}
          <PhoneAudioButton
            isRecording={phoneAudioRecorder.isRecording || audioManager.isPhoneAudioMode}
            isInitializing={phoneAudioRecorder.isInitializing}
            isDisabled={!!deviceConnection.connectedDeviceId || deviceConnection.isConnecting}
            audioLevel={phoneAudioRecorder.audioLevel}
            error={phoneAudioRecorder.error}
            onPress={audioManager.togglePhoneAudio}
          />

          {/* Bluetooth Status */}
          <BluetoothStatusBanner
            bluetoothState={bluetoothState}
            isPermissionsLoading={isPermissionsLoading}
            permissionGranted={permissionGranted}
            onRequestPermission={requestBluetoothPermission}
          />

          {/* Scan Controls */}
          <ScanControls
            scanning={scanning}
            onScanPress={startScan}
            onStopScanPress={stopDeviceScanAction}
            canScan={canScan}
          />

          {/* Device List */}
          {scannedDevices.length > 0 && !deviceConnection.connectedDeviceId && !autoReconnect.isAttemptingAutoReconnect && (
            <DeviceList
              devices={scannedDevices}
              onConnect={deviceConnection.connectToDevice}
              onDisconnect={deviceConnection.disconnectFromDevice}
              isConnecting={deviceConnection.isConnecting}
              connectedDeviceId={deviceConnection.connectedDeviceId}
            />
          )}

          {/* Connected Device */}
          {deviceConnection.connectedDeviceId && (
            <ConnectedDevice
              connectedDeviceId={deviceConnection.connectedDeviceId}
              device={connectedDevice}
              isConnecting={deviceConnection.isConnecting}
              onDisconnect={deviceConnection.disconnectFromDevice}
              onClearLastKnownDevice={autoReconnect.clearLastKnownDevice}
              onGetAudioCodec={deviceConnection.getAudioCodec}
              currentCodec={deviceConnection.currentCodec}
              onGetBatteryLevel={deviceConnection.getBatteryLevel}
              batteryLevel={deviceConnection.batteryLevel}
              isListeningAudio={isOmiAudioListenerActive}
              onStartAudioListener={audioManager.startOmiAudioStreaming}
              onStopAudioListener={audioManager.stopOmiAudioStreaming}
              audioPacketsReceived={audioPacketsReceived}
              webSocketUrl={webSocketUrl}
              onSetWebSocketUrl={handleSetAndSaveWebSocketUrl}
              isAudioStreaming={audioStreamer.isStreaming}
              isConnectingAudioStreamer={audioStreamer.isConnecting}
              audioStreamerError={audioStreamer.error}
              userId={userId}
              onSetUserId={handleSetAndSaveUserId}
              isAudioListenerRetrying={isAudioListenerRetrying}
              audioListenerRetryAttempts={audioListenerRetryAttempts}
            />
          )}
        </ScrollView>
      </KeyboardAvoidingView>

      {/* Connection Log Viewer Modal */}
      <ConnectionLogViewer
        visible={isLogsVisible}
        onClose={() => setIsLogsVisible(false)}
        entries={connectionLog.entries}
        connectionState={connectionLog.connectionState}
        onClearLogs={connectionLog.clearLogs}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: theme.colors.background.secondary,
  },
  content: {
    padding: theme.spacing.lg,
    paddingTop: Platform.OS === 'android' ? theme.spacing.xl : theme.spacing.sm,
    paddingBottom: theme.spacing.xxl,
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: theme.spacing.lg,
  },
  headerSpacer: {
    width: 60,
  },
  title: {
    flex: 1,
    fontSize: theme.typography.fontSize.xxxl,
    fontWeight: theme.typography.fontWeight.bold,
    color: theme.colors.text.primary,
    textAlign: 'center',
    letterSpacing: -0.5,
  },
  logsButton: {
    width: 60,
    paddingVertical: theme.spacing.sm,
    paddingHorizontal: theme.spacing.sm,
    backgroundColor: theme.colors.gray[100],
    borderRadius: theme.borderRadius.sm,
    alignItems: 'center',
  },
  logsButtonText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.secondary,
  },
  centeredMessageContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: theme.spacing.lg,
  },
  centeredMessageText: {
    marginTop: theme.spacing.sm,
    fontSize: theme.typography.fontSize.md,
    color: theme.colors.text.secondary,
    textAlign: 'center',
    lineHeight: theme.typography.lineHeight.relaxed * theme.typography.fontSize.md,
  },
});
