import React from 'react';
import { View, StyleSheet } from 'react-native';
import { useTheme } from '../theme';

interface SignalStrengthProps {
  rssi: number | null | undefined;
}

function getBars(rssi: number | null | undefined): number {
  if (rssi == null) return 0;
  if (rssi >= -50) return 4;
  if (rssi >= -65) return 3;
  if (rssi >= -80) return 2;
  if (rssi >= -90) return 1;
  return 0;
}

const BAR_HEIGHTS = [6, 10, 14, 18];

const SignalStrength: React.FC<SignalStrengthProps> = ({ rssi }) => {
  const { colors } = useTheme();
  const bars = getBars(rssi);

  return (
    <View style={styles.container}>
      {BAR_HEIGHTS.map((height, i) => (
        <View
          key={i}
          style={[
            styles.bar,
            { height, backgroundColor: i < bars ? colors.success : colors.separator },
          ]}
        />
      ))}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: 2,
    marginLeft: 8,
  },
  bar: {
    width: 4,
    borderRadius: 1,
  },
});

export default SignalStrength;
