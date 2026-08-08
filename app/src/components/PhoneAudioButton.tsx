import React from 'react';
import { View, StyleSheet } from 'react-native';

import { Button, Caption, type ButtonVariant } from '@/components/ui';
import { useTheme, type Theme } from '@/theme';

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
  const t = useTheme();
  const s = createStyles(t);

  const getButtonVariant = (): ButtonVariant => {
    // `secondary` is the neutral chip fill, which is the old `disabled` grey;
    // the Button dims itself while `disabled` is set.
    if (isDisabled && !isRecording) return 'secondary';
    if (isRecording) return 'danger';
    if (error) return 'warning';
    return 'primary';
  };

  const getButtonText = () => {
    if (isInitializing) return 'Initializing...';
    if (isRecording) return 'Stop Phone Audio';
    return 'Stream Phone Audio';
  };

  return (
    <View style={s.container}>
      <View style={s.buttonWrapper}>
        <Button
          variant={getButtonVariant()}
          size="lg"
          fullWidth
          loading={isInitializing}
          disabled={isDisabled}
          onPress={onPress}
        >
          {isInitializing ? null : getButtonText()}
        </Button>
      </View>

      {isRecording && (
        <View style={s.audioLevelContainer}>
          <View style={s.audioLevelBackground}>
            <View style={[s.audioLevelBar, { width: `${Math.min(audioLevel * 100, 100)}%` }]} />
          </View>
          <Caption style={s.audioLevelText}>Audio Level</Caption>
        </View>
      )}

      {isRecording && (
        <Caption style={s.statusText}>Streaming audio to backend...</Caption>
      )}

      {error && !isRecording && (
        <Caption style={s.errorText}>{error}</Caption>
      )}

      {isDisabled && !isRecording && (
        <Caption style={s.disabledText}>Disconnect Bluetooth device to use phone audio</Caption>
      )}
    </View>
  );
};

const createStyles = (t: Theme) => StyleSheet.create({
  container: {
    marginVertical: t.space[3],
  },
  buttonWrapper: {
    alignSelf: 'stretch',
  },
  statusText: {
    textAlign: 'center',
    marginTop: t.space[2],
  },
  errorText: {
    textAlign: 'center',
    marginTop: t.space[2],
    color: t.color.status.danger.fg,
  },
  disabledText: {
    textAlign: 'center',
    marginTop: t.space[2],
    fontStyle: 'italic',
  },
  audioLevelContainer: {
    marginTop: t.space[3],
    alignItems: 'center',
  },
  audioLevelBackground: {
    width: '100%',
    height: t.space[1],
    backgroundColor: t.color.border.subtle,
    borderRadius: t.radius.sm,
    overflow: 'hidden',
  },
  audioLevelBar: {
    height: '100%',
    backgroundColor: t.color.status.success.base,
    borderRadius: t.radius.sm,
  },
  audioLevelText: {
    marginTop: t.space[1],
  },
});

export default PhoneAudioButton;
