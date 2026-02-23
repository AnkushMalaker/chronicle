import React from 'react';
import { TouchableOpacity, Text, View, StyleSheet, ActivityIndicator } from 'react-native';
import { useTheme, ThemeColors } from '../theme';

interface PhoneAudioButtonProps {
  isRecording: boolean;
  isInitializing: boolean;
  isDisabled: boolean;
  audioLevel: number;
  error: string | null;
  onPress: () => void;
}

const PhoneAudioButton: React.FC<PhoneAudioButtonProps> = ({
  isRecording,
  isInitializing,
  isDisabled,
  audioLevel,
  error,
  onPress,
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);

  const getButtonStyle = () => {
    if (isDisabled && !isRecording) return [s.button, { backgroundColor: colors.disabled }];
    if (isRecording) return [s.button, { backgroundColor: colors.danger }];
    if (error) return [s.button, { backgroundColor: colors.warning }];
    return [s.button, { backgroundColor: colors.primary }];
  };

  const getButtonText = () => {
    if (isInitializing) return 'Initializing...';
    if (isRecording) return 'Stop Phone Audio';
    return 'Stream Phone Audio';
  };

  return (
    <View style={s.container}>
      <View style={s.buttonWrapper}>
        <TouchableOpacity
          style={getButtonStyle()}
          onPress={onPress}
          disabled={isDisabled || isInitializing}
          activeOpacity={0.7}
        >
          {isInitializing ? (
            <ActivityIndicator size="small" color="#fff" />
          ) : (
            <View style={s.buttonContent}>
              <Text style={s.buttonText}>{getButtonText()}</Text>
            </View>
          )}
        </TouchableOpacity>
      </View>

      {isRecording && (
        <View style={s.audioLevelContainer}>
          <View style={s.audioLevelBackground}>
            <View style={[s.audioLevelBar, { width: `${Math.min(audioLevel * 100, 100)}%` }]} />
          </View>
          <Text style={s.audioLevelText}>Audio Level</Text>
        </View>
      )}

      {isRecording && (
        <Text style={s.statusText}>Streaming audio to backend...</Text>
      )}

      {error && !isRecording && (
        <Text style={s.errorText}>{error}</Text>
      )}

      {isDisabled && !isRecording && (
        <Text style={s.disabledText}>Disconnect Bluetooth device to use phone audio</Text>
      )}
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  container: {
    marginVertical: 10,
    paddingHorizontal: 20,
  },
  buttonWrapper: {
    alignSelf: 'stretch',
  },
  button: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    minHeight: 48,
  },
  buttonContent: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
  },
  buttonText: {
    color: '#FFFFFF',
    fontSize: 16,
    fontWeight: '600',
  },
  statusText: {
    textAlign: 'center',
    marginTop: 8,
    fontSize: 12,
    color: colors.textTertiary,
  },
  errorText: {
    textAlign: 'center',
    marginTop: 8,
    fontSize: 12,
    color: colors.danger,
  },
  disabledText: {
    textAlign: 'center',
    marginTop: 8,
    fontSize: 12,
    color: colors.textTertiary,
    fontStyle: 'italic',
  },
  audioLevelContainer: {
    marginTop: 12,
    alignItems: 'center',
  },
  audioLevelBackground: {
    width: '100%',
    height: 4,
    backgroundColor: colors.separator,
    borderRadius: 2,
    overflow: 'hidden',
  },
  audioLevelBar: {
    height: '100%',
    backgroundColor: colors.success,
    borderRadius: 2,
  },
  audioLevelText: {
    marginTop: 4,
    fontSize: 10,
    color: colors.textTertiary,
  },
});

export default PhoneAudioButton;
