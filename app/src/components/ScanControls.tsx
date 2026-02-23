import React from 'react';
import { TouchableOpacity, Text, StyleSheet, View } from 'react-native';
import { useTheme, ThemeColors } from '../theme';

interface ScanControlsProps {
  scanning: boolean;
  onScanPress: () => void;
  onStopScanPress: () => void;
  canScan: boolean;
}

export const ScanControls: React.FC<ScanControlsProps> = ({
  scanning,
  onScanPress,
  onStopScanPress,
  canScan,
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);

  return (
    <View style={s.section}>
      <Text style={s.sectionTitle}>Bluetooth Connection</Text>
      <TouchableOpacity
        style={[
          s.button,
          scanning ? { backgroundColor: colors.warning } : null,
          !canScan && !scanning ? s.buttonDisabled : null,
        ]}
        onPress={scanning ? onStopScanPress : onScanPress}
        disabled={!canScan && !scanning}
      >
        <Text style={s.buttonText}>{scanning ? "Stop Scan" : "Scan for Devices"}</Text>
      </TouchableOpacity>
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
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    elevation: 2,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.1,
    shadowRadius: 2,
  },
  buttonDisabled: {
    backgroundColor: colors.disabled,
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
});

export default ScanControls;
