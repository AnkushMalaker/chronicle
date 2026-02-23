import { useState, useEffect, useRef, useCallback } from 'react';
import { useConnectionLog } from '../contexts/ConnectionLogContext';

interface UseBatteryMonitorParams {
  connectedDeviceId: string | null;
  getBatteryLevel: () => Promise<number>;
  onConnectionLost?: () => void;
}

interface UseBatteryMonitor {
  batteryLevel: number;
  isLowBattery: boolean;
  refreshBattery: () => Promise<void>;
}

const POLL_INTERVAL_MS = 60_000; // 60 seconds
const MIN_CHANGE = 5; // Minimum % change to update UI
const MAX_UI_STALE_MS = 15 * 60_000; // 15 minutes
const LOW_BATTERY_THRESHOLD = 20;
const MAX_CONSECUTIVE_FAILURES = 2;

export const useBatteryMonitor = ({
  connectedDeviceId,
  getBatteryLevel,
  onConnectionLost,
}: UseBatteryMonitorParams): UseBatteryMonitor => {
  const [batteryLevel, setBatteryLevel] = useState<number>(-1);
  const [isLowBattery, setIsLowBattery] = useState(false);
  const { addEvent } = useConnectionLog();

  const lastDisplayedRef = useRef<number>(-1);
  const lastUpdateTimeRef = useRef<number>(0);
  const consecutiveFailuresRef = useRef<number>(0);
  const intervalRef = useRef<NodeJS.Timeout | null>(null);

  const shouldUpdate = useCallback((newLevel: number): boolean => {
    const current = lastDisplayedRef.current;
    if (current === -1) return true; // First read
    if (Math.abs(newLevel - current) >= MIN_CHANGE) return true;
    if (Date.now() - lastUpdateTimeRef.current >= MAX_UI_STALE_MS) return true;
    // Crosses 20% threshold
    if ((current > LOW_BATTERY_THRESHOLD && newLevel <= LOW_BATTERY_THRESHOLD) ||
        (current <= LOW_BATTERY_THRESHOLD && newLevel > LOW_BATTERY_THRESHOLD)) return true;
    return false;
  }, []);

  const poll = useCallback(async () => {
    if (!connectedDeviceId) return;

    try {
      const level = await getBatteryLevel();
      consecutiveFailuresRef.current = 0;

      if (shouldUpdate(level)) {
        setBatteryLevel(level);
        lastDisplayedRef.current = level;
        lastUpdateTimeRef.current = Date.now();
        setIsLowBattery(level <= LOW_BATTERY_THRESHOLD);
        addEvent('battery_read', `Battery: ${level}%`);
      }
    } catch (error) {
      consecutiveFailuresRef.current++;
      addEvent('health_ping', `Battery read failed (${consecutiveFailuresRef.current}/${MAX_CONSECUTIVE_FAILURES})`);

      if (consecutiveFailuresRef.current >= MAX_CONSECUTIVE_FAILURES) {
        addEvent('error', 'Connection presumed lost after consecutive battery read failures');
        onConnectionLost?.();
      }
    }
  }, [connectedDeviceId, getBatteryLevel, shouldUpdate, addEvent, onConnectionLost]);

  const refreshBattery = useCallback(async () => {
    if (!connectedDeviceId) return;
    try {
      const level = await getBatteryLevel();
      consecutiveFailuresRef.current = 0;
      setBatteryLevel(level);
      lastDisplayedRef.current = level;
      lastUpdateTimeRef.current = Date.now();
      setIsLowBattery(level <= LOW_BATTERY_THRESHOLD);
      addEvent('battery_read', `Battery: ${level}% (manual refresh)`);
    } catch (error) {
      addEvent('error', `Battery refresh failed: ${error}`);
    }
  }, [connectedDeviceId, getBatteryLevel, addEvent]);

  useEffect(() => {
    if (connectedDeviceId) {
      consecutiveFailuresRef.current = 0;
      lastDisplayedRef.current = -1;
      lastUpdateTimeRef.current = 0;
      // Initial read after short delay for connection to stabilize
      const initTimer = setTimeout(poll, 2000);
      intervalRef.current = setInterval(poll, POLL_INTERVAL_MS);
      return () => {
        clearTimeout(initTimer);
        if (intervalRef.current) clearInterval(intervalRef.current);
      };
    } else {
      setBatteryLevel(-1);
      setIsLowBattery(false);
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    }
  }, [connectedDeviceId, poll]);

  return { batteryLevel, isLowBattery, refreshBattery };
};
