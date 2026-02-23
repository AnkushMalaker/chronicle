import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, TextInput } from 'react-native';
import { BleAudioCodec } from 'friend-lite-react-native';
import { useTheme, ThemeColors } from '../theme';

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
  audioListenerRetryAttempts
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);

  if (!connectedDeviceId) return null;

  return (
    <View style={s.section}>
      <Text style={s.sectionTitle}>Device Functions</Text>

      <TouchableOpacity style={s.button} onPress={onGetAudioCodec}>
        <Text style={s.buttonText}>Get Audio Codec</Text>
      </TouchableOpacity>
      {currentCodec && (
        <View style={s.infoContainerSM}>
          <Text style={s.infoTitle}>Current Audio Codec:</Text>
          <Text style={s.infoValue}>{currentCodec}</Text>
        </View>
      )}

      {batteryLevel >= 0 ? (
        <View style={[s.batteryContainer, isLowBattery ? { borderLeftColor: colors.danger } : null]}>
          <View style={s.batteryHeaderRow}>
            <Text style={s.infoTitle}>Battery Level:</Text>
            <TouchableOpacity onPress={onRefreshBattery} style={s.refreshButton}>
              <Text style={s.refreshButtonText}>Refresh</Text>
            </TouchableOpacity>
          </View>
          <View style={s.batteryLevelDisplayContainer}>
            <View style={[s.batteryLevelBar, { width: `${batteryLevel}%`, backgroundColor: isLowBattery ? colors.danger : colors.success }]} />
            <Text style={s.batteryLevelText}>{batteryLevel}%</Text>
          </View>
          {isLowBattery && <Text style={s.lowBatteryText}>Low battery</Text>}
        </View>
      ) : (
        <View style={s.batteryContainer}>
          <Text style={s.infoTitle}>Battery: reading...</Text>
        </View>
      )}

      <View style={s.subSection}>
        <Text style={s.subSectionTitle}>User ID (optional)</Text>
        <Text style={s.inputLabel}>Enter User ID (for device identification):</Text>
        <TextInput
          style={s.textInput}
          value={userId}
          onChangeText={onSetUserId}
          placeholder="e.g., device_name, user_identifier"
          placeholderTextColor={colors.textTertiary}
          autoCapitalize="none"
          returnKeyType="done"
          autoCorrect={false}
          editable={!isListeningAudio && !isAudioStreaming}
        />
        {userId && (
          <View style={s.infoContainerSM}>
            <Text style={s.infoTitle}>Current User ID:</Text>
            <Text style={s.infoValue}>{userId}</Text>
          </View>
        )}
      </View>

      <View style={s.subSection}>
        <TouchableOpacity
          style={[
            s.button,
            isListeningAudio || isAudioListenerRetrying ? { backgroundColor: colors.warning } : null,
            { marginTop: 15 }
          ]}
          onPress={isListeningAudio || isAudioListenerRetrying ? onStopAudioListener : onStartAudioListener}
        >
          <Text style={s.buttonText}>
            {isListeningAudio ? "Stop Audio Listener" :
             isAudioListenerRetrying ? "Stop Retry" : "Start Audio Listener"}
          </Text>
        </TouchableOpacity>

        {isAudioListenerRetrying && (
          <View style={s.retryContainer}>
            <Text style={s.retryText}>
              Retrying audio listener... (Attempt {audioListenerRetryAttempts || 0}/10)
            </Text>
          </View>
        )}

        {isListeningAudio && (
          <View style={s.infoContainerSM}>
            <Text style={s.infoTitle}>Audio Packets Received:</Text>
            <Text style={s.infoValueLg}>{audioPacketsReceived}</Text>
          </View>
        )}
      </View>

      <View style={s.customStreamerSection}>
        <Text style={s.subSectionTitle}>Custom Audio Streaming</Text>
        <Text style={s.inputLabel}>Backend WebSocket URL:</Text>
        <TextInput
          style={s.textInput}
          value={webSocketUrl}
          onChangeText={onSetWebSocketUrl}
          placeholder="wss://your-backend.com/ws/audio"
          placeholderTextColor={colors.textTertiary}
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
          <Text style={[s.statusText, { color: colors.success }]}>Streaming audio to WebSocket...</Text>
        )}
        {audioStreamerError && (
          <Text style={[s.statusText, { color: colors.danger, fontWeight: 'bold' }]}>Error: {audioStreamerError}</Text>
        )}
      </View>
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
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
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 15,
    color: colors.text,
  },
  subSection: {
    marginTop: 20,
  },
  subSectionTitle: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 12,
    color: colors.textSecondary,
  },
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    elevation: 2,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
  infoContainerSM: {
    marginTop: 10,
    padding: 10,
    backgroundColor: colors.inputBackground,
    borderRadius: 8,
    alignItems: 'center',
  },
  infoTitle: {
    fontSize: 14,
    fontWeight: '500',
    color: colors.textSecondary,
  },
  infoValue: {
    fontSize: 16,
    fontWeight: 'bold',
    color: colors.primary,
    marginTop: 5,
  },
  infoValueLg: {
    fontSize: 18,
    fontWeight: 'bold',
    color: colors.warning,
    marginTop: 5,
  },
  batteryContainer: {
    marginTop: 15,
    padding: 12,
    backgroundColor: colors.inputBackground,
    borderRadius: 8,
    borderLeftWidth: 4,
    borderLeftColor: colors.success,
  },
  batteryHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 4,
  },
  refreshButton: {
    paddingVertical: 4,
    paddingHorizontal: 10,
    borderRadius: 4,
    backgroundColor: colors.separator,
  },
  refreshButtonText: {
    fontSize: 12,
    color: colors.textSecondary,
    fontWeight: '500',
  },
  lowBatteryText: {
    marginTop: 6,
    fontSize: 12,
    color: colors.danger,
    fontWeight: '600',
    textAlign: 'center',
  },
  batteryLevelDisplayContainer: {
    width: '100%',
    height: 24,
    backgroundColor: colors.separator,
    borderRadius: 12,
    marginTop: 8,
    overflow: 'hidden',
    position: 'relative',
  },
  batteryLevelBar: {
    height: '100%',
    backgroundColor: colors.success,
    borderRadius: 12,
    position: 'absolute',
    left: 0,
    top: 0,
  },
  batteryLevelText: {
    position: 'absolute',
    width: '100%',
    textAlign: 'center',
    lineHeight: 24,
    fontSize: 12,
    fontWeight: 'bold',
    color: colors.text,
  },
  customStreamerSection: {
    marginTop: 20,
    paddingTop: 15,
    borderTopWidth: 1,
    borderTopColor: colors.separator,
  },
  inputLabel: {
    fontSize: 14,
    color: colors.text,
    marginBottom: 5,
    fontWeight: '500',
  },
  textInput: {
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: 6,
    padding: 10,
    fontSize: 14,
    width: '100%',
    marginBottom: 10,
    color: colors.text,
  },
  statusText: {
    marginTop: 8,
    fontSize: 13,
    color: colors.textSecondary,
    textAlign: 'left',
  },
  retryContainer: {
    marginTop: 10,
    padding: 12,
    backgroundColor: colors.inputBackground,
    borderRadius: 8,
    borderLeftWidth: 4,
    borderLeftColor: colors.warning,
  },
  retryText: {
    fontSize: 14,
    color: colors.warning,
    fontWeight: '500',
    textAlign: 'center',
  },
});

export default DeviceDetails;
