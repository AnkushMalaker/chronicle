import React, { createContext, useContext, useCallback, useRef, useState } from 'react';
import { logInfo } from '@/utils/logger';

export interface ConnectionEvent {
  id: string;
  timestamp: Date;
  type: ConnectionEventType;
  deviceId?: string;
  deviceName?: string;
  details?: string;
  rssi?: number;
}

export type ConnectionEventType =
  | 'scan_start'
  | 'scan_stop'
  | 'scan_result'
  | 'connect_start'
  | 'device_active'
  | 'connect_success'
  | 'connect_fail'
  | 'disconnect'
  | 'disconnect_reason'
  | 'battery_read'
  | 'audio_start'
  | 'audio_stop'
  | 'error'
  | 'health_ping'
  | 'reconnect_attempt'
  | 'reconnect_backoff'
  | 'bt_state_change'
  // WebSocket (audio streaming) lifecycle — distinct from the BLE events above
  | 'ws_connecting'
  | 'ws_open'
  | 'ws_close'
  | 'ws_error'
  | 'ws_reconnect'
  | 'ws_reauth'
  | 'net_change';

const MAX_EVENTS = 200;
let eventCounter = 0;

interface ConnectionLogContextValue {
  events: ConnectionEvent[];
  addEvent: (type: ConnectionEventType, details?: string, extra?: Partial<ConnectionEvent>) => void;
  clearEvents: () => void;
}

const ConnectionLogContext = createContext<ConnectionLogContextValue>({
  events: [],
  addEvent: () => {},
  clearEvents: () => {},
});

export const ConnectionLogProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [events, setEvents] = useState<ConnectionEvent[]>([]);
  const eventsRef = useRef<ConnectionEvent[]>([]);

  const addEvent = useCallback((type: ConnectionEventType, details?: string, extra?: Partial<ConnectionEvent>) => {
    const event: ConnectionEvent = {
      id: `evt-${++eventCounter}`,
      timestamp: new Date(),
      type,
      details,
      ...extra,
    };

    eventsRef.current = [event, ...eventsRef.current].slice(0, MAX_EVENTS);
    setEvents(eventsRef.current);

    const extras = [
      event.deviceName ? `device="${event.deviceName}"` : null,
      event.deviceId ? `id=${event.deviceId}` : null,
      event.rssi != null ? `rssi=${event.rssi}` : null,
      details ? `details="${details}"` : null,
    ].filter(Boolean).join(' ');
    logInfo('ConnectionLog', `${type}${extras ? ' ' + extras : ''}`);
  }, []);

  const clearEvents = useCallback(() => {
    eventsRef.current = [];
    setEvents([]);
  }, []);

  return (
    <ConnectionLogContext.Provider value={{ events, addEvent, clearEvents }}>
      {children}
    </ConnectionLogContext.Provider>
  );
};

export const useConnectionLog = () => useContext(ConnectionLogContext);
