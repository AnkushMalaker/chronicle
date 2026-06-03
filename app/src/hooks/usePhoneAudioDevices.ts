// usePhoneAudioDevices.ts
// Enumerates available audio INPUT devices (phone mic, Bluetooth headset, wired, etc.)
// and tracks which one to use for phone-audio streaming.
//
// Selection model:
//   - 'auto'  → prefer an available Bluetooth input if present, else system default mic.
//   - <id>    → use that specific device when available, else fall back to system default.
import { useState, useRef, useEffect, useCallback } from 'react';
import { audioDeviceManager } from '@siteed/expo-audio-studio';
import type { AudioDevice } from '@siteed/expo-audio-studio';

export const AUTO_DEVICE_ID = 'auto';

/** Heuristic: is this input device a Bluetooth headset/earbud mic? */
export const isBluetoothInput = (device: AudioDevice): boolean => {
  const type = (device.type || '').toLowerCase();
  const name = (device.name || '').toLowerCase();
  return (
    type.includes('bluetooth') ||
    type.includes('bt') ||
    name.includes('bluetooth') ||
    name.includes('airpod') ||
    name.includes('headset') ||
    name.includes('headphone') ||
    name.includes('buds') ||
    name.includes('hands-free')
  );
};

/**
 * Resolve the device that should actually be used given the current selection.
 * Returns null when the system default mic should be used (no explicit deviceId).
 */
export const pickPreferredDevice = (
  devices: AudioDevice[],
  selectedDeviceId: string
): AudioDevice | null => {
  if (selectedDeviceId && selectedDeviceId !== AUTO_DEVICE_ID) {
    const explicit = devices.find((d) => d.id === selectedDeviceId && d.isAvailable);
    if (explicit) return explicit;
    // Selected device is gone — fall through to auto behaviour.
  }
  const bluetooth = devices.find((d) => isBluetoothInput(d) && d.isAvailable);
  if (bluetooth) return bluetooth;
  return null; // system default
};

export interface UsePhoneAudioDevices {
  devices: AudioDevice[];
  selectedDeviceId: string;
  setSelectedDeviceId: (id: string) => void;
  /** The device 'auto' would resolve to right now (for UI display), or null for default mic. */
  effectiveDevice: AudioDevice | null;
  loading: boolean;
  refresh: () => Promise<AudioDevice[]>;
  /** Resolve the deviceId to pass to the recorder at start time (undefined = system default). */
  resolveEffectiveDeviceId: () => Promise<string | undefined>;
}

export const usePhoneAudioDevices = (): UsePhoneAudioDevices => {
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string>(AUTO_DEVICE_ID);
  const [loading, setLoading] = useState<boolean>(false);

  const selectedRef = useRef<string>(selectedDeviceId);
  const mountedRef = useRef<boolean>(true);
  useEffect(() => {
    selectedRef.current = selectedDeviceId;
  }, [selectedDeviceId]);

  const refresh = useCallback(async (): Promise<AudioDevice[]> => {
    setLoading(true);
    try {
      const list = await audioDeviceManager.getAvailableDevices({ refresh: true });
      if (mountedRef.current) setDevices(list);
      return list;
    } catch (error) {
      console.warn('[PhoneAudioDevices] Failed to enumerate devices:', error);
      return [];
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    refresh();
    // React to devices connecting/disconnecting (e.g. Bluetooth headset paired while app open)
    const removeListener = audioDeviceManager.addDeviceChangeListener((list) => {
      if (mountedRef.current) setDevices(list);
    });
    return () => {
      mountedRef.current = false;
      removeListener();
    };
  }, [refresh]);

  const resolveEffectiveDeviceId = useCallback(async (): Promise<string | undefined> => {
    // Refresh right before starting so we pick up a just-connected headset.
    let list = devices;
    try {
      list = await audioDeviceManager.getAvailableDevices({ refresh: true });
      if (mountedRef.current) setDevices(list);
    } catch (error) {
      console.warn('[PhoneAudioDevices] resolve refresh failed, using cached list:', error);
    }
    const picked = pickPreferredDevice(list, selectedRef.current);
    return picked?.id;
  }, [devices]);

  const effectiveDevice = pickPreferredDevice(devices, selectedDeviceId);

  return {
    devices,
    selectedDeviceId,
    setSelectedDeviceId,
    effectiveDevice,
    loading,
    refresh,
    resolveEffectiveDeviceId,
  };
};
