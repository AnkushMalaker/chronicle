import React, { useState, useCallback } from 'react';
import { View, StyleSheet } from 'react-native';
import ServerManager from './ServerManager';
import AuthSection from './AuthSection';
import ObsidianIngest from './ObsidianIngest';
import type { ServerConnection, ConnectionStatus } from '../types/serverConnection';
import { buildHttpUrl } from '../types/serverConnection';

interface SettingsPanelProps {
  backendUrl: string;
  onBackendUrlChange: (url: string) => Promise<void>;
  jwtToken: string | null;
  isAuthenticated: boolean;
  currentUserEmail: string | null;
  onAuthStatusChange: (isAuthenticated: boolean, email: string | null, token: string | null) => void;
}

/**
 * Panel component that groups all settings and configuration options.
 * Includes server connection management, authentication, and Obsidian integration.
 */
export const SettingsPanel: React.FC<SettingsPanelProps> = ({
  backendUrl,
  onBackendUrlChange,
  jwtToken,
  isAuthenticated,
  currentUserEmail,
  onAuthStatusChange,
}) => {
  const [activeConnection, setActiveConnection] = useState<ServerConnection | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>({
    status: 'idle',
    message: '',
  });

  // Handle connection changes from ServerManager
  const handleConnectionChange = useCallback((
    connection: ServerConnection | null,
    status: ConnectionStatus
  ) => {
    setActiveConnection(connection);
    setConnectionStatus(status);

    // Update the backend URL for compatibility with other components
    if (connection) {
      const httpUrl = buildHttpUrl(connection);
      onBackendUrlChange(httpUrl);

      // If connection includes auth and was successful, update auth status
      if (status.status === 'connected' && connection.username) {
        // Auth was handled by ServerManager
        onAuthStatusChange(true, connection.username, null);
      }
    }
  }, [onBackendUrlChange, onAuthStatusChange]);

  // Derive backend URL from active connection for child components
  const effectiveBackendUrl = activeConnection ? buildHttpUrl(activeConnection) : backendUrl;

  return (
    <View style={styles.container} testID="settings-panel">
      {/* Server Connection Management */}
      <ServerManager
        onConnectionChange={handleConnectionChange}
      />

      {/* Authentication Section - shown when connected but not authenticated via connection */}
      {connectionStatus.status === 'connected' && !isAuthenticated && (
        <AuthSection
          backendUrl={effectiveBackendUrl}
          isAuthenticated={isAuthenticated}
          currentUserEmail={currentUserEmail}
          onAuthStatusChange={onAuthStatusChange}
        />
      )}

      {/* Obsidian Integration - Only when authenticated */}
      {isAuthenticated && jwtToken && (
        <ObsidianIngest
          backendUrl={effectiveBackendUrl}
          jwtToken={jwtToken}
        />
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    width: '100%',
  },
});

export default SettingsPanel;
