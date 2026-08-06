import React from 'react';

import { StatusDot, type Tone } from '@/components/ui';
import { useTheme } from '@/theme';

interface StatusIndicatorProps {
  isActive: boolean;
  size?: number;
  /** Tone shown while active. */
  activeTone?: Tone;
  /** Tone shown while inactive. */
  inactiveTone?: Tone;
}

const StatusIndicator: React.FC<StatusIndicatorProps> = ({
  isActive,
  size = 10,
  activeTone = 'success',
  inactiveTone = 'danger',
}) => {
  const t = useTheme();

  return (
    <StatusDot
      tone={isActive ? activeTone : inactiveTone}
      size={size}
      // Spacing around the dot, as before.
      style={{ marginHorizontal: t.space[2] }}
    />
  );
};

export default StatusIndicator;
