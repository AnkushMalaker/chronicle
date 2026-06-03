import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, ActivityIndicator } from 'react-native';
import { useTheme, ThemeColors } from '../theme';
import type { AudioDevice } from '@siteed/expo-audio-studio';
import { AUTO_DEVICE_ID, isBluetoothInput } from '../hooks/usePhoneAudioDevices';

interface PhoneAudioMicPickerProps {
  devices: AudioDevice[];
  selectedDeviceId: string;
  effectiveDevice: AudioDevice | null;
  loading: boolean;
  /** Disable changing the mic (e.g. while streaming is active). */
  disabled: boolean;
  onSelect: (id: string) => void;
  onRefresh: () => void;
}

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
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);

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
    <View style={s.container}>
      <View style={s.header}>
        <Text style={s.label}>Microphone</Text>
        <TouchableOpacity onPress={onRefresh} disabled={loading} activeOpacity={0.7}>
          {loading ? (
            <ActivityIndicator size="small" color={colors.textTertiary} />
          ) : (
            <Text style={s.refresh}>Refresh</Text>
          )}
        </TouchableOpacity>
      </View>

      <View style={s.chipRow}>
        {renderChip(AUTO_DEVICE_ID, 'Auto', 'auto', autoSelected ? autoSubtitle : undefined)}
        {devices.map((d) => renderChip(d.id, deviceLabel(d), d.id))}
      </View>

      {devices.length === 0 && !loading && (
        <Text style={s.hint}>
          No input devices detected. Connect your Bluetooth headset, then tap Refresh.
        </Text>
      )}
    </View>
  );
};

const createStyles = (colors: ThemeColors) =>
  StyleSheet.create({
    container: {
      marginTop: -4,
      marginBottom: 10,
      paddingHorizontal: 20,
    },
    header: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'center',
      marginBottom: 8,
    },
    label: {
      fontSize: 13,
      fontWeight: '600',
      color: colors.textSecondary,
    },
    refresh: {
      fontSize: 13,
      color: colors.primary,
      fontWeight: '500',
    },
    chipRow: {
      flexDirection: 'row',
      flexWrap: 'wrap',
      gap: 8,
    },
    chip: {
      paddingVertical: 6,
      paddingHorizontal: 12,
      borderRadius: 16,
      borderWidth: 1,
      borderColor: colors.inputBorder,
      backgroundColor: colors.inputBackground,
      maxWidth: 220,
    },
    chipSelected: {
      borderColor: colors.primary,
      backgroundColor: colors.primary,
    },
    chipDisabled: {
      opacity: 0.5,
    },
    chipText: {
      fontSize: 13,
      color: colors.text,
      fontWeight: '500',
    },
    chipTextSelected: {
      color: '#FFFFFF',
    },
    chipSubtitle: {
      fontSize: 10,
      color: colors.textTertiary,
      marginTop: 1,
    },
    hint: {
      marginTop: 8,
      fontSize: 12,
      color: colors.textTertiary,
      fontStyle: 'italic',
    },
  });

export default PhoneAudioMicPicker;
