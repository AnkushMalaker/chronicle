/**
 * Connection Log Types
 *
 * Tracks connection status changes across multiple subsystems:
 * - Network (internet connectivity)
 * - Server (backend API health)
 * - Bluetooth (device connection)
 * - WebSocket (audio streaming)
 */

export type ConnectionType = 'network' | 'server' | 'bluetooth' | 'websocket';

export type ConnectionStatus = 'connected' | 'disconnected' | 'connecting' | 'error' | 'unknown';

export interface ConnectionLogEntry {
  id: string;
  timestamp: Date;
  type: ConnectionType;
  status: ConnectionStatus;
  message: string;
  details?: string;
  metadata?: Record<string, unknown>;
}

export interface ConnectionState {
  network: ConnectionStatus;
  server: ConnectionStatus;
  bluetooth: ConnectionStatus;
  websocket: ConnectionStatus;
}

// Helper to get human-readable labels
export const CONNECTION_TYPE_LABELS: Record<ConnectionType, string> = {
  network: 'Network',
  server: 'Server',
  bluetooth: 'Bluetooth',
  websocket: 'WebSocket',
};

// Emojis for each connection type
export const CONNECTION_TYPE_EMOJIS: Record<ConnectionType, string> = {
  network: '🌐',
  server: '☁️',
  bluetooth: '📶',
  websocket: '🔌',
};

// Colors for each connection type (theme color keys)
export const CONNECTION_TYPE_COLORS: Record<ConnectionType, string> = {
  network: '#3B82F6',    // Blue
  server: '#A855F7',     // Violet/Purple (secondary)
  bluetooth: '#06B6D4',  // Cyan
  websocket: '#10B981',  // Emerald (primary)
};

// Helper to get status colors (returns theme color key)
export const STATUS_COLORS: Record<ConnectionStatus, string> = {
  connected: 'healthy',
  disconnected: 'unhealthy',
  connecting: 'checking',
  error: 'unhealthy',
  unknown: 'unknown',
};

// Helper to get status icons
export const STATUS_ICONS: Record<ConnectionStatus, string> = {
  connected: '✓',
  disconnected: '✗',
  connecting: '◌',
  error: '!',
  unknown: '?',
};

// Generate unique ID for log entries
export const generateLogId = (): string => {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
};
