import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Platform } from 'react-native';

import type { AudioDevice } from '@siteed/expo-audio-studio';

import { Button, Caption, Card, Divider, SectionLabel } from '@/components/ui';
import { AUTO_DEVICE_ID, isBluetoothInput } from '@/hooks/usePhoneAudioDevices';
import { useTheme, type Theme } from '@/theme';
import type { MicCaptureProfile } from '@/utils/storage';

interface PhoneAudioMicPickerProps {
  devices: AudioDevice[];
  selectedDeviceId: string;
  effectiveDevice: AudioDevice | null;
  loading: boolean;
  /** Disable changing the mic (e.g. while streaming is active). */
  disabled: boolean;
  onSelect: (id: string) => void;
  onRefresh: () => void;
  /** iOS mic processing profile; the row is hidden on other platforms. */
  captureProfile: MicCaptureProfile;
  onSelectCaptureProfile: (profile: MicCaptureProfile) => void;
}

const PROFILE_OPTIONS: { id: MicCaptureProfile; label: string; subtitle: string }[] = [
  { id: 'far-field', label: 'Far-field', subtitle: 'raw, whole room' },
  { id: 'voice', label: 'Voice', subtitle: 'iOS processed' },
];

const deviceLabel = (device: AudioDevice): string => {
  const base = device.name?.trim() || 'Unknown device';
  if (isBluetoothInput(device)) return `🎧 ${base}`;
  return `🎙 ${base}`;
};

const PhoneAudioMicPicker: React.FC<PhoneAudioMicPickerProps> = ({
  devices,
  selectedDeviceId,
  effectiveDevice,
  loading,
  disabled,
  onSelect,
  onRefresh,
  captureProfile,
  onSelectCaptureProfile,
}) => {
  const t = useTheme();
  const s = createStyles(t);

  const autoSelected = selectedDeviceId === AUTO_DEVICE_ID;
  const autoSubtitle = effectiveDevice
    ? `using ${effectiveDevice.name}`
    : 'using phone mic';

  const renderChip = (id: string, label: string, key: string, subtitle?: string) => {
    const isSelected = selectedDeviceId === id;
    return (
      <TouchableOpacity
        key={key}
        style={[s.chip, isSelected && s.chipSelected, disabled && s.chipDisabled]}
        onPress={() => !disabled && onSelect(id)}
        disabled={disabled}
        activeOpacity={0.7}
      >
        <Text style={[s.chipText, isSelected && s.chipTextSelected]} numberOfLines={1}>
          {label}
        </Text>
        {subtitle ? (
          <Text style={[s.chipSubtitle, isSelected && s.chipTextSelected]} numberOfLines={1}>
            {subtitle}
          </Text>
        ) : null}
      </TouchableOpacity>
    );
  };

  return (
    <Card
      title="Microphone"
      headerRight={
        <Button variant="link" size="sm" loading={loading} onPress={onRefresh}>
          {loading ? null : 'Refresh'}
        </Button>
      }
    >
      <View style={s.chipRow}>
        {renderChip(AUTO_DEVICE_ID, 'Auto', 'auto', autoSelected ? autoSubtitle : undefined)}
        {devices.map((d) => renderChip(d.id, deviceLabel(d), d.id))}
      </View>

      {devices.length === 0 && !loading && (
        <Caption style={s.hint}>
          No input devices detected. Connect your Bluetooth headset, then tap Refresh.
        </Caption>
      )}

      {Platform.OS === 'ios' && (
        <>
          <Divider style={s.divider} />
          <SectionLabel>Processing</SectionLabel>
          <View style={s.chipRow}>
            {PROFILE_OPTIONS.map(({ id, label, subtitle }) => {
              const isSelected = captureProfile === id;
              return (
                <TouchableOpacity
                  key={id}
                  style={[s.chip, isSelected && s.chipSelected, disabled && s.chipDisabled]}
                  onPress={() => !disabled && onSelectCaptureProfile(id)}
                  disabled={disabled}
                  activeOpacity={0.7}
                >
                  <Text style={[s.chipText, isSelected && s.chipTextSelected]} numberOfLines={1}>
                    {label}
                  </Text>
                  <Text style={[s.chipSubtitle, isSelected && s.chipTextSelected]} numberOfLines={1}>
                    {subtitle}
                  </Text>
                </TouchableOpacity>
              );
            })}
          </View>
        </>
      )}
    </Card>
  );
};

const createStyles = (t: Theme) =>
  StyleSheet.create({
    chipRow: {
      flexDirection: 'row',
      flexWrap: 'wrap',
      gap: t.space[2],
    },
    chip: {
      paddingVertical: t.space[1.5],
      paddingHorizontal: t.space[3],
      borderRadius: t.radius.full,
      borderWidth: t.borderWidth,
      borderColor: t.color.border.base,
      backgroundColor: t.color.surface.sunken,
      maxWidth: 220,
    },
    chipSelected: {
      borderColor: t.color.accent.base,
      backgroundColor: t.color.accent.navBg,
    },
    chipDisabled: {
      opacity: 0.5,
    },
    chipText: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      color: t.color.text.primary,
      fontWeight: t.weight.medium,
    },
    chipTextSelected: {
      color: t.color.accent.fg,
    },
    chipSubtitle: {
      fontFamily: t.font.sans,
      ...t.type.xs,
      color: t.color.text.muted,
      marginTop: t.space.px,
    },
    hint: {
      marginTop: t.space[2],
      fontStyle: 'italic',
    },
    divider: {
      marginTop: t.space[3],
      marginBottom: t.space[3],
    },
  });

export default PhoneAudioMicPicker;
