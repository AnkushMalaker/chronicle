// PhoneAudioButton.tsx
import React from 'react';
import {
  TouchableOpacity,
  Text,
  View,
  StyleSheet,
  ActivityIndicator,
} from 'react-native';
import theme from '../theme/design-system';

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

  const getButtonStyle = () => {
    if (isDisabled && !isRecording) {
      return [styles.button, styles.buttonDisabled];
    }
    if (isRecording) {
      return [styles.button, styles.buttonRecording];
    }
    if (error) {
      return [styles.button, styles.buttonError];
    }
    return [styles.button, styles.buttonIdle];
  };

  const getButtonText = () => {
    if (isInitializing) {
      return 'Initializing...';
    }
    if (isRecording) {
      return 'Stop Phone Audio';
    }
    return 'Stream Phone Audio';
  };

  const getMicrophoneIcon = () => {
    if (isRecording) {
      return '🎤'; // Recording microphone
    }
    return '🎙️'; // Idle microphone
  };

  return (
    <View style={styles.container}>
      <View style={styles.buttonWrapper}>
        <TouchableOpacity
          style={getButtonStyle()}
          onPress={onPress}
          disabled={isDisabled || isInitializing}
          activeOpacity={0.7}
        >
          {isInitializing ? (
            <ActivityIndicator size="small" color="#fff" />
          ) : (
            <View style={styles.buttonContent}>
              <Text style={styles.icon}>{getMicrophoneIcon()}</Text>
              <Text style={styles.buttonText}>{getButtonText()}</Text>
            </View>
          )}
        </TouchableOpacity>
      </View>

      {/* Audio Level Indicator */}
      {isRecording && (
        <View style={styles.audioLevelContainer}>
          <View style={styles.audioLevelBackground}>
            <View
              style={[
                styles.audioLevelBar,
                { width: `${Math.min(audioLevel * 100, 100)}%` },
              ]}
            />
          </View>
          <Text style={styles.audioLevelText}>Audio Level</Text>
        </View>
      )}

      {/* Status Message */}
      {isRecording && (
        <Text style={styles.statusText}>
          Streaming audio to backend...
        </Text>
      )}

      {/* Error Message */}
      {error && !isRecording && (
        <Text style={styles.errorText}>{error}</Text>
      )}

      {/* Disabled Message */}
      {isDisabled && !isRecording && (
        <Text style={styles.disabledText}>
          Disconnect Bluetooth device to use phone audio
        </Text>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    marginVertical: theme.spacing.sm + 2,
    paddingHorizontal: theme.spacing.lg + 4,
  },
  buttonWrapper: {
    alignSelf: 'stretch',
  },
  button: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: theme.spacing.md - 4,
    paddingHorizontal: theme.spacing.lg + 4,
    borderRadius: theme.borderRadius.sm,
    minHeight: 48,
  },
  buttonContent: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
  },
  buttonIdle: {
    backgroundColor: theme.colors.primary.main,  // Primary emerald for main action
  },
  buttonRecording: {
    backgroundColor: theme.colors.error.main,  // Red when recording
  },
  buttonDisabled: {
    backgroundColor: theme.colors.gray[300],  // More visible disabled state
    borderWidth: 1,
    borderColor: theme.colors.border.medium,
  },
  buttonError: {
    backgroundColor: theme.colors.warning.main,
  },
  buttonText: {
    color: theme.colors.primary.contrast,  // Dark text for WCAG AA contrast
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
    marginLeft: theme.spacing.sm,
  },
  icon: {
    fontSize: theme.typography.fontSize.xl,
  },
  statusText: {
    textAlign: 'center',
    marginTop: theme.spacing.sm,
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
  },
  errorText: {
    textAlign: 'center',
    marginTop: theme.spacing.sm,
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.error.main,
  },
  disabledText: {
    textAlign: 'center',
    marginTop: theme.spacing.sm,
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
    fontStyle: 'italic',
  },
  audioLevelContainer: {
    marginTop: theme.spacing.md - 4,
    alignItems: 'center',
  },
  audioLevelBackground: {
    width: '100%',
    height: 4,
    backgroundColor: theme.colors.gray[200],
    borderRadius: 2,
    overflow: 'hidden',
  },
  audioLevelBar: {
    height: '100%',
    backgroundColor: theme.colors.primary.main,  // Green bar for audio level
    borderRadius: 2,
  },
  audioLevelText: {
    marginTop: theme.spacing.xs,
    fontSize: 10,
    color: theme.colors.text.tertiary,
  },
});

export default PhoneAudioButton;