import React from 'react';
import { StyleSheet, View } from 'react-native';

import { useTheme, type Theme } from '@/theme';

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
  const t = useTheme();
  const s = createStyles(t);
  const bars = getBars(rssi);

  return (
    <View style={s.container}>
      {BAR_HEIGHTS.map((height, i) => (
        <View
          key={i}
          style={[
            s.bar,
            {
              height,
              backgroundColor: i < bars ? t.color.status.success.base : t.color.border.subtle,
            },
          ]}
        />
      ))}
    </View>
  );
};

const createStyles = (t: Theme) =>
  StyleSheet.create({
    container: {
      flexDirection: 'row',
      alignItems: 'flex-end',
      gap: t.space[0.5],
      marginLeft: t.space[2],
    },
    bar: {
      width: t.space[1],
      borderRadius: 1,
    },
  });

export default SignalStrength;
