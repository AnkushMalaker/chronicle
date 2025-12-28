/**
 * Server Connection Types
 *
 * Data model for storing and managing backend server connections.
 */

export type Protocol = 'ws' | 'wss' | 'http' | 'https';
export type Route = 'ws_pcm' | 'ws_omi' | 'ws' | '';

export interface ServerConnection {
  id: string;
  name: string;
  protocol: Protocol;
  domain: string;
  port?: string;
  route: Route;
  username: string;
  password: string;
  createdAt: number;
  updatedAt: number;
}

export interface ConnectionStatus {
  status: 'idle' | 'connecting' | 'connected' | 'error' | 'auth_required';
  message: string;
  lastChecked?: Date;
}

/**
 * Build a full URL from server connection parts
 */
export const buildServerUrl = (connection: ServerConnection): string => {
  const { protocol, domain, port, route } = connection;
  let url = `${protocol}://${domain}`;
  if (port) {
    url += `:${port}`;
  }
  if (route) {
    url += `/${route}`;
  }
  return url;
};

/**
 * Build HTTP URL for health checks from a WebSocket URL
 */
export const buildHttpUrl = (connection: ServerConnection): string => {
  const { protocol, domain, port } = connection;
  const httpProtocol = protocol === 'wss' ? 'https' : protocol === 'ws' ? 'http' : protocol;
  let url = `${httpProtocol}://${domain}`;
  if (port) {
    url += `:${port}`;
  }
  return url;
};

/**
 * Generate a unique ID for a new connection
 */
export const generateConnectionId = (): string => {
  return `conn_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
};

/**
 * Create a new empty connection with defaults
 */
export const createEmptyConnection = (): Omit<ServerConnection, 'id' | 'createdAt' | 'updatedAt'> => ({
  name: '',
  protocol: 'wss',
  domain: '',
  port: '',
  route: 'ws_pcm',
  username: '',
  password: '',
});
