import React, { useState, useEffect, useCallback } from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
} from 'react-native';
import theme from '../theme/design-system';
import type { ServerConnection, ConnectionStatus } from '../types/serverConnection';
import { buildServerUrl, buildHttpUrl } from '../types/serverConnection';
import {
  getServerConnections,
  saveServerConnections,
  getActiveServerId,
  saveActiveServerId,
} from '../utils/storage';
import { ServerConnectionForm } from './ServerConnectionForm';
import { ServerConnectionList } from './ServerConnectionList';

interface ServerManagerProps {
  onConnectionChange?: (connection: ServerConnection | null, status: ConnectionStatus) => void;
}

export const ServerManager: React.FC<ServerManagerProps> = ({
  onConnectionChange,
}) => {
  const [connections, setConnections] = useState<ServerConnection[]>([]);
  const [activeConnectionId, setActiveConnectionId] = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>({
    status: 'idle',
    message: '',
  });
  const [showForm, setShowForm] = useState(false);
  const [editingConnection, setEditingConnection] = useState<ServerConnection | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Load saved connections on mount
  useEffect(() => {
    const loadConnections = async () => {
      try {
        const savedConnections = await getServerConnections();
        const activeId = await getActiveServerId();
        setConnections(savedConnections);
        setActiveConnectionId(activeId);
      } catch (error) {
        console.error('[ServerManager] Error loading connections:', error);
      } finally {
        setIsLoading(false);
      }
    };
    loadConnections();
  }, []);

  // Save connections when they change
  const persistConnections = useCallback(async (newConnections: ServerConnection[]) => {
    await saveServerConnections(newConnections);
    setConnections(newConnections);
  }, []);

  // Handle adding/updating a connection
  const handleSaveConnection = useCallback(async (connection: ServerConnection) => {
    const existingIndex = connections.findIndex(c => c.id === connection.id);
    let newConnections: ServerConnection[];

    if (existingIndex !== -1) {
      // Update existing connection
      newConnections = [...connections];
      newConnections[existingIndex] = connection;
    } else {
      // Add new connection
      newConnections = [...connections, connection];
    }

    await persistConnections(newConnections);
    setEditingConnection(null);

    // If this is the first connection, select it
    if (connections.length === 0) {
      setActiveConnectionId(connection.id);
      await saveActiveServerId(connection.id);
    }
  }, [connections, persistConnections]);

  // Handle deleting a connection
  const handleDeleteConnection = useCallback(async (connectionId: string) => {
    const newConnections = connections.filter(c => c.id !== connectionId);
    await persistConnections(newConnections);

    // If deleted connection was active, clear selection
    if (activeConnectionId === connectionId) {
      setActiveConnectionId(null);
      await saveActiveServerId(null);
      setConnectionStatus({ status: 'idle', message: '' });
      onConnectionChange?.(null, { status: 'idle', message: '' });
    }
  }, [connections, activeConnectionId, persistConnections, onConnectionChange]);

  // Handle selecting a connection
  const handleSelectConnection = useCallback(async (connection: ServerConnection) => {
    setActiveConnectionId(connection.id);
    await saveActiveServerId(connection.id);
    // Reset status when selecting new connection
    setConnectionStatus({ status: 'idle', message: 'Tap Connect to connect' });
  }, []);

  // Handle editing a connection
  const handleEditConnection = useCallback((connection: ServerConnection) => {
    setEditingConnection(connection);
    setShowForm(true);
  }, []);

  // Test connection to server
  const testConnection = useCallback(async (connection: ServerConnection): Promise<boolean> => {
    try {
      const httpUrl = buildHttpUrl(connection);
      const response = await fetch(`${httpUrl}/health`, {
        method: 'GET',
        headers: {
          'Accept': 'application/json',
        },
      });
      return response.ok;
    } catch (error) {
      console.error('[ServerManager] Health check failed:', error);
      return false;
    }
  }, []);

  // Handle connect button
  const handleConnect = useCallback(async () => {
    const connection = connections.find(c => c.id === activeConnectionId);
    if (!connection) return;

    setConnectionStatus({ status: 'connecting', message: 'Connecting...' });
    onConnectionChange?.(connection, { status: 'connecting', message: 'Connecting...' });

    try {
      // Test basic connectivity
      const isHealthy = await testConnection(connection);

      if (!isHealthy) {
        const errorStatus: ConnectionStatus = {
          status: 'error',
          message: 'Server not reachable',
          lastChecked: new Date(),
        };
        setConnectionStatus(errorStatus);
        onConnectionChange?.(connection, errorStatus);
        return;
      }

      // Check if authentication is required
      if (connection.username && connection.password) {
        // Attempt authentication
        const httpUrl = buildHttpUrl(connection);
        try {
          const authResponse = await fetch(`${httpUrl}/auth/jwt/login`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: `username=${encodeURIComponent(connection.username)}&password=${encodeURIComponent(connection.password)}`,
          });

          if (!authResponse.ok) {
            const errorStatus: ConnectionStatus = {
              status: 'auth_required',
              message: 'Authentication failed',
              lastChecked: new Date(),
            };
            setConnectionStatus(errorStatus);
            onConnectionChange?.(connection, errorStatus);
            return;
          }

          // Auth successful
          const authData = await authResponse.json();
          console.log('[ServerManager] Authentication successful, token received');

          const successStatus: ConnectionStatus = {
            status: 'connected',
            message: 'Connected and authenticated',
            lastChecked: new Date(),
          };
          setConnectionStatus(successStatus);
          onConnectionChange?.(connection, successStatus);
        } catch (authError) {
          console.error('[ServerManager] Auth error:', authError);
          const errorStatus: ConnectionStatus = {
            status: 'error',
            message: 'Authentication error',
            lastChecked: new Date(),
          };
          setConnectionStatus(errorStatus);
          onConnectionChange?.(connection, errorStatus);
        }
      } else {
        // No auth required, just mark as connected
        const successStatus: ConnectionStatus = {
          status: 'connected',
          message: 'Connected',
          lastChecked: new Date(),
        };
        setConnectionStatus(successStatus);
        onConnectionChange?.(connection, successStatus);
      }
    } catch (error) {
      console.error('[ServerManager] Connection error:', error);
      const errorStatus: ConnectionStatus = {
        status: 'error',
        message: 'Connection failed',
        lastChecked: new Date(),
      };
      setConnectionStatus(errorStatus);
      onConnectionChange?.(connection, errorStatus);
    }
  }, [connections, activeConnectionId, testConnection, onConnectionChange]);

  // Handle disconnect
  const handleDisconnect = useCallback(() => {
    const connection = connections.find(c => c.id === activeConnectionId);
    const disconnectedStatus: ConnectionStatus = {
      status: 'idle',
      message: 'Disconnected',
    };
    setConnectionStatus(disconnectedStatus);
    onConnectionChange?.(connection || null, disconnectedStatus);
  }, [connections, activeConnectionId, onConnectionChange]);

  const handleAddServer = () => {
    setEditingConnection(null);
    setShowForm(true);
  };

  const handleCloseForm = () => {
    setShowForm(false);
    setEditingConnection(null);
  };

  if (isLoading) {
    return (
      <View style={styles.section}>
        <Text style={styles.loadingText}>Loading servers...</Text>
      </View>
    );
  }

  return (
    <View style={styles.section}>
      <View style={styles.header}>
        <Text style={styles.sectionTitle}>Server Configuration</Text>
        <TouchableOpacity
          style={styles.addButton}
          onPress={handleAddServer}
          testID="add-server-button"
        >
          <Text style={styles.addButtonText}>+ Add Server</Text>
        </TouchableOpacity>
      </View>

      <ServerConnectionList
        connections={connections}
        activeConnectionId={activeConnectionId}
        connectionStatus={connectionStatus}
        onSelect={handleSelectConnection}
        onEdit={handleEditConnection}
        onDelete={handleDeleteConnection}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
      />

      <ServerConnectionForm
        visible={showForm}
        onClose={handleCloseForm}
        onSave={handleSaveConnection}
        editConnection={editingConnection}
      />
    </View>
  );
};

const styles = StyleSheet.create({
  section: {
    marginBottom: theme.spacing.lg,
    padding: theme.spacing.md,
    backgroundColor: theme.colors.background.primary,
    borderRadius: theme.borderRadius.md,
    ...theme.shadows.sm,
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: theme.spacing.md,
  },
  sectionTitle: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
  },
  addButton: {
    backgroundColor: theme.colors.primary.main,
    paddingVertical: theme.spacing.sm,
    paddingHorizontal: theme.spacing.md,
    borderRadius: theme.borderRadius.md,
  },
  addButtonText: {
    color: theme.colors.text.inverse,
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
  },
  loadingText: {
    fontSize: theme.typography.fontSize.md,
    color: theme.colors.text.secondary,
    textAlign: 'center',
    padding: theme.spacing.lg,
  },
});

export default ServerManager;
