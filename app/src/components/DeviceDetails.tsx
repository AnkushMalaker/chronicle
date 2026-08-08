import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { BleAudioCodec } from 'friend-lite-react-native';

import { Button, Card, CardWell, Divider, InlineAlert, TextField } from '@/components/ui';
import { useTheme, type Theme } from '@/theme';

interface DeviceDetailsProps {
  connectedDeviceId: string | null;
  onGetAudioCodec: () => void;
  currentCodec: BleAudioCodec | null;
  batteryLevel: number;
  isLowBattery: boolean;
  onRefreshBattery: () => void;
  isListeningAudio: boolean;
  onStartAudioListener: () => void;
  onStopAudioListener: () => void;
  audioPacketsReceived: number;
  webSocketUrl: string;
  onSetWebSocketUrl: (url: string) => void;
  isAudioStreaming: boolean;
  isConnectingAudioStreamer: boolean;
  audioStreamerError: string | null;
  userId: string;
  onSetUserId: (userId: string) => void;
  isAudioListenerRetrying?: boolean;
  audioListenerRetryAttempts?: number;
  autoReconnectEnabled?: boolean;
  onToggleAutoReconnect?: () => void;
}

export const DeviceDetails: React.FC<DeviceDetailsProps> = ({
  connectedDeviceId,
  onGetAudioCodec,
  currentCodec,
  batteryLevel,
  isLowBattery,
  onRefreshBattery,
  isListeningAudio,
  onStartAudioListener,
  onStopAudioListener,
  audioPacketsReceived,
  webSocketUrl,
  onSetWebSocketUrl,
  isAudioStreaming,
  isConnectingAudioStreamer,
  audioStreamerError,
  userId,
  onSetUserId,
  isAudioListenerRetrying,
  audioListenerRetryAttempts,
  autoReconnectEnabled = true,
  onToggleAutoReconnect,
}) => {
  const t = useTheme();
  const s = createStyles(t);

  if (!connectedDeviceId) return null;

  const isListenerActive = isListeningAudio || isAudioListenerRetrying;

  return (
    <Card title="Device Functions">
      <Button variant="primary" size="lg" fullWidth onPress={onGetAudioCodec}>
        Get Audio Codec
      </Button>
      {currentCodec && (
        <CardWell style={s.infoWell}>
          <Text style={s.infoTitle}>Current Audio Codec:</Text>
          <Text style={s.infoValue}>{currentCodec}</Text>
        </CardWell>
      )}

      {batteryLevel >= 0 ? (
        <CardWell style={[s.batteryWell, isLowBattery ? s.batteryWellLow : null]}>
          <View style={s.batteryHeaderRow}>
            <Text style={s.infoTitle}>Battery Level:</Text>
            <Button variant="secondary" size="sm" onPress={onRefreshBattery}>
              Refresh
            </Button>
          </View>
          <View style={s.batteryLevelDisplayContainer}>
            <View
              style={[
                s.batteryLevelBar,
                {
                  width: `${batteryLevel}%`,
                  backgroundColor: isLowBattery
                    ? t.color.status.danger.base
                    : t.color.status.success.base,
                },
              ]}
            />
            <Text style={s.batteryLevelText}>{batteryLevel}%</Text>
          </View>
          {isLowBattery && <Text style={s.lowBatteryText}>Low battery</Text>}
        </CardWell>
      ) : (
        <CardWell style={s.batteryWell}>
          <Text style={s.infoTitle}>Battery: reading...</Text>
        </CardWell>
      )}

      <View style={s.subSection}>
        <Text style={s.subSectionTitle}>User ID (optional)</Text>
        <TextField
          label="Enter User ID (for device identification):"
          value={userId}
          onChangeText={onSetUserId}
          placeholder="e.g., device_name, user_identifier"
          autoCapitalize="none"
          returnKeyType="done"
          autoCorrect={false}
          editable={!isListeningAudio && !isAudioStreaming}
        />
        {userId && (
          <CardWell style={s.infoWell}>
            <Text style={s.infoTitle}>Current User ID:</Text>
            <Text style={s.infoValue}>{userId}</Text>
          </CardWell>
        )}
      </View>

      <View style={s.subSection}>
        <View style={s.listenerRow}>
          <Button
            variant={isListenerActive ? 'warning' : 'primary'}
            size="lg"
            style={s.listenerButton}
            onPress={isListenerActive ? onStopAudioListener : onStartAudioListener}
          >
            {isListeningAudio ? "Stop Audio Listener" :
             isAudioListenerRetrying ? "Stop Retry" : "Start Audio Listener"}
          </Button>

          {onToggleAutoReconnect && (
            <TouchableOpacity
              style={[s.lockButton, autoReconnectEnabled ? s.lockButtonOn : null]}
              onPress={onToggleAutoReconnect}
              accessibilityRole="switch"
              accessibilityState={{ checked: autoReconnectEnabled }}
              accessibilityLabel={autoReconnectEnabled ? 'Auto-reconnect on' : 'Connect once'}
            >
              <Text style={s.lockIcon}>{autoReconnectEnabled ? '🔒' : '🔓'}</Text>
            </TouchableOpacity>
          )}
        </View>

        {onToggleAutoReconnect && (
          <Text style={s.lockHint}>
            {autoReconnectEnabled
              ? '🔒 Stays connected — auto-reconnects if the connection drops.'
              : '🔓 Connect once — won’t auto-reconnect if it drops.'}
          </Text>
        )}

        {isAudioListenerRetrying && (
          <InlineAlert tone="warning" style={s.retryAlert}>
            Retrying audio listener... (Attempt {audioListenerRetryAttempts || 0}/10)
          </InlineAlert>
        )}

        {isListeningAudio && (
          <CardWell style={s.infoWell}>
            <Text style={s.infoTitle}>Audio Packets Received:</Text>
            <Text style={s.infoValueLg}>{audioPacketsReceived}</Text>
          </CardWell>
        )}
      </View>

      <Divider style={s.sectionDivider} />
      <View style={s.customStreamerSection}>
        <Text style={s.subSectionTitle}>Custom Audio Streaming</Text>
        <TextField
          label="Backend WebSocket URL:"
          value={webSocketUrl}
          onChangeText={onSetWebSocketUrl}
          placeholder="wss://your-backend.com/ws/audio"
          autoCapitalize="none"
          keyboardType="url"
          returnKeyType="done"
          autoCorrect={false}
          editable={!isListeningAudio && !isAudioStreaming}
        />

        {isConnectingAudioStreamer && (
          <Text style={s.statusText}>Connecting to WebSocket...</Text>
        )}
        {isAudioStreaming && (
          <Text style={[s.statusText, s.statusTextStreaming]}>Streaming audio to WebSocket...</Text>
        )}
        {audioStreamerError && (
          <Text style={[s.statusText, s.statusTextError]}>Error: {audioStreamerError}</Text>
        )}
      </View>
    </Card>
  );
};

const createStyles = (t: Theme) => StyleSheet.create({
  subSection: {
    marginTop: t.space[5],
  },
  subSectionTitle: {
    fontFamily: t.font.sans,
    ...t.type.base,
    fontWeight: t.weight.semibold,
    marginBottom: t.space[3],
    color: t.color.text.secondary,
  },
  listenerRow: {
    flexDirection: 'row',
    alignItems: 'stretch',
    marginTop: t.space[4],
    gap: t.space[2],
  },
  listenerButton: {
    flex: 1,
  },
  lockButton: {
    width: 52,
    borderRadius: t.radius.lg,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: t.borderWidth,
    borderColor: t.color.border.base,
    backgroundColor: t.color.surface.sunken,
  },
  lockButtonOn: {
    borderColor: t.color.accent.base,
    backgroundColor: t.color.surface.raised,
  },
  lockIcon: {
    fontSize: 22,
  },
  lockHint: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.muted,
    marginTop: t.space[2],
  },
  infoWell: {
    marginTop: t.space[3],
    alignItems: 'center',
  },
  infoTitle: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    fontWeight: t.weight.medium,
    color: t.color.text.secondary,
  },
  infoValue: {
    fontFamily: t.font.sans,
    ...t.type.base,
    fontWeight: t.weight.bold,
    color: t.color.accent.fg,
    marginTop: t.space[1],
  },
  infoValueLg: {
    fontFamily: t.font.sans,
    ...t.type.lg,
    fontWeight: t.weight.bold,
    color: t.color.status.warning.fg,
    marginTop: t.space[1],
  },
  batteryWell: {
    marginTop: t.space[4],
    borderLeftWidth: 4,
    borderLeftColor: t.color.status.success.base,
  },
  batteryWellLow: {
    borderLeftColor: t.color.status.danger.base,
  },
  batteryHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: t.space[1],
  },
  lowBatteryText: {
    fontFamily: t.font.sans,
    marginTop: t.space[1.5],
    ...t.type.xs,
    color: t.color.status.danger.fg,
    fontWeight: t.weight.semibold,
    textAlign: 'center',
  },
  batteryLevelDisplayContainer: {
    width: '100%',
    height: 24,
    backgroundColor: t.color.border.subtle,
    borderRadius: t.radius.full,
    marginTop: t.space[2],
    overflow: 'hidden',
    position: 'relative',
  },
  batteryLevelBar: {
    height: '100%',
    borderRadius: t.radius.full,
    position: 'absolute',
    left: 0,
    top: 0,
  },
  batteryLevelText: {
    position: 'absolute',
    width: '100%',
    textAlign: 'center',
    lineHeight: 24,
    fontFamily: t.font.sans,
    fontSize: t.type.xs.fontSize,
    fontWeight: t.weight.bold,
    color: t.color.text.primary,
  },
  sectionDivider: {
    marginTop: t.space[5],
  },
  customStreamerSection: {
    paddingTop: t.space[4],
  },
  statusText: {
    fontFamily: t.font.sans,
    marginTop: t.space[2],
    ...t.type.sm,
    color: t.color.text.secondary,
    textAlign: 'left',
  },
  statusTextStreaming: {
    color: t.color.status.success.fg,
  },
  statusTextError: {
    color: t.color.status.danger.fg,
    fontWeight: t.weight.bold,
  },
  retryAlert: {
    marginTop: t.space[3],
  },
});

export default DeviceDetails;
