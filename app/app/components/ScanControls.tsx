import React from 'react';
import { TouchableOpacity, Text, StyleSheet, View } from 'react-native';
import theme from '../theme/design-system';

interface ScanControlsProps {
  scanning: boolean;
  onScanPress: () => void;
  onStopScanPress: () => void;
  canScan: boolean; // To disable button if permissions not granted or BT is off
}

export const ScanControls: React.FC<ScanControlsProps> = ({
  scanning,
  onScanPress,
  onStopScanPress,
  canScan,
}) => {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>Bluetooth Connection</Text>
      <TouchableOpacity
        style={[
          styles.button,
          scanning ? styles.buttonWarning : null,
          !canScan && !scanning ? styles.buttonDisabled : null, // Disable if cannot scan and not already scanning
        ]}
        onPress={scanning ? onStopScanPress : onScanPress}
        disabled={!canScan && !scanning}
      >
        <Text style={styles.buttonText}>{scanning ? "Stop Scan" : "Scan for Devices"}</Text>
      </TouchableOpacity>
    </View>
  );
};

const styles = StyleSheet.create({
  section: {
    marginBottom: theme.spacing.lg,
    padding: theme.spacing.md,
    backgroundColor: theme.colors.background.primary,
    borderRadius: theme.borderRadius.md,
    ...theme.shadows.sm,
  },
  sectionTitle: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.semibold,
    marginBottom: theme.spacing.md,
    color: theme.colors.text.primary,
  },
  button: {
    ...theme.components.button.primary,
    alignItems: 'center',
    ...theme.shadows.sm,
  },
  buttonWarning: {
    backgroundColor: theme.colors.warning.main,
  },
  buttonDisabled: {
    backgroundColor: theme.colors.gray[300],
    borderWidth: 1,
    borderColor: theme.colors.border.medium,
  },
  buttonText: {
    color: theme.colors.primary.contrast,  // Dark text for WCAG AA on emerald
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
  },
});

export default ScanControls; 