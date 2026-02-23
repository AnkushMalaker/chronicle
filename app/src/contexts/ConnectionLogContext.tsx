import React, { createContext, useContext, useCallback, useRef, useState } from 'react';

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
  | 'connect_success'
  | 'connect_fail'
  | 'disconnect'
  | 'battery_read'
  | 'audio_start'
  | 'audio_stop'
  | 'error'
  | 'health_ping'
  | 'reconnect_attempt'
  | 'bt_state_change';

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
