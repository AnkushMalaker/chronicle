import React from 'react';

import { Button, Card } from '@/components/ui';

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
  return (
    <Card title="Bluetooth Connection">
      <Button
        variant={scanning ? 'warning' : 'primary'}
        size="lg"
        fullWidth
        onPress={scanning ? onStopScanPress : onScanPress}
        disabled={!canScan && !scanning}
      >
        {scanning ? 'Stop Scan' : 'Scan for Devices'}
      </Button>
    </Card>
  );
};

export default ScanControls;
