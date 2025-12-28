import React from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  FlatList,
  Alert,
} from 'react-native';
import theme from '../theme/design-system';
import type { ServerConnection, ConnectionStatus } from '../types/serverConnection';
import { buildServerUrl } from '../types/serverConnection';

interface ServerConnectionListProps {
  connections: ServerConnection[];
  activeConnectionId: string | null;
  connectionStatus: ConnectionStatus;
  onSelect: (connection: ServerConnection) => void;
  onEdit: (connection: ServerConnection) => void;
  onDelete: (connectionId: string) => void;
  onConnect: () => void;
  onDisconnect: () => void;
}

export const ServerConnectionList: React.FC<ServerConnectionListProps> = ({
  connections,
  activeConnectionId,
  connectionStatus,
  onSelect,
  onEdit,
  onDelete,
  onConnect,
  onDisconnect,
}) => {
  const handleDelete = (connection: ServerConnection) => {
    Alert.alert(
      'Delete Server',
      `Are you sure you want to delete "${connection.name}"?`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Delete',
          style: 'destructive',
          onPress: () => onDelete(connection.id),
        },
      ]
    );
  };

  const getStatusColor = () => {
    switch (connectionStatus.status) {
      case 'connected':
        return theme.colors.status.healthy;
      case 'connecting':
        return theme.colors.status.checking;
      case 'error':
        return theme.colors.status.unhealthy;
      case 'auth_required':
        return theme.colors.secondary.main;
      default:
        return theme.colors.text.tertiary;
    }
  };

  const getStatusLabel = () => {
    switch (connectionStatus.status) {
      case 'connected':
        return 'Connected';
      case 'connecting':
        return 'Connecting...';
      case 'error':
        return 'Error';
      case 'auth_required':
        return 'Auth Required';
      default:
        return 'Not Connected';
    }
  };

  const renderConnectionItem = ({ item }: { item: ServerConnection }) => {
    const isActive = item.id === activeConnectionId;
    const url = buildServerUrl(item);

    return (
      <TouchableOpacity
        style={[styles.connectionItem, isActive && styles.connectionItemActive]}
        onPress={() => onSelect(item)}
        testID={`server-item-${item.id}`}
      >
        <View style={styles.connectionContent}>
          <View style={styles.connectionHeader}>
            <Text style={[styles.connectionName, isActive && styles.connectionNameActive]}>
              {item.name}
            </Text>
            {isActive && (
              <View style={[styles.statusBadge, { backgroundColor: getStatusColor() }]}>
                <Text style={styles.statusBadgeText}>{getStatusLabel()}</Text>
              </View>
            )}
          </View>
          <Text style={styles.connectionUrl} numberOfLines={1}>
            {url}
          </Text>
          {item.username ? (
            <Text style={styles.connectionAuth}>User: {item.username}</Text>
          ) : null}
        </View>

        <View style={styles.actionButtons}>
          <TouchableOpacity
            style={styles.iconButton}
            onPress={() => onEdit(item)}
            testID={`edit-server-${item.id}`}
          >
            <Text style={styles.iconButtonText}>✏️</Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.iconButton, styles.deleteButton]}
            onPress={() => handleDelete(item)}
            testID={`delete-server-${item.id}`}
          >
            <Text style={styles.iconButtonText}>🗑️</Text>
          </TouchableOpacity>
        </View>
      </TouchableOpacity>
    );
  };

  const renderEmptyState = () => (
    <View style={styles.emptyState}>
      <Text style={styles.emptyStateText}>No servers configured</Text>
      <Text style={styles.emptyStateSubtext}>
        Tap "Add Server" to create your first connection
      </Text>
    </View>
  );

  const activeConnection = connections.find(c => c.id === activeConnectionId);
  const isConnected = connectionStatus.status === 'connected';
  const isConnecting = connectionStatus.status === 'connecting';

  return (
    <View style={styles.container}>
      <Text style={styles.sectionTitle}>Saved Servers</Text>

      <FlatList
        data={connections}
        keyExtractor={(item) => item.id}
        renderItem={renderConnectionItem}
        ListEmptyComponent={renderEmptyState}
        scrollEnabled={false}
        style={styles.list}
      />

      {activeConnection && (
        <View style={styles.connectSection}>
          <View style={styles.selectedServerInfo}>
            <Text style={styles.selectedLabel}>Selected:</Text>
            <Text style={styles.selectedName}>{activeConnection.name}</Text>
          </View>

          {connectionStatus.message ? (
            <Text style={[styles.statusMessage, { color: getStatusColor() }]}>
              {connectionStatus.message}
            </Text>
          ) : null}

          {isConnected ? (
            <TouchableOpacity
              style={[styles.connectButton, styles.disconnectButton]}
              onPress={onDisconnect}
              testID="disconnect-button"
            >
              <Text style={styles.connectButtonText}>Disconnect</Text>
            </TouchableOpacity>
          ) : (
            <TouchableOpacity
              style={[
                styles.connectButton,
                isConnecting && styles.connectingButton,
              ]}
              onPress={onConnect}
              disabled={isConnecting}
              testID="connect-button"
            >
              <Text style={styles.connectButtonText}>
                {isConnecting ? 'Connecting...' : 'Connect'}
              </Text>
            </TouchableOpacity>
          )}
        </View>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    marginBottom: theme.spacing.md,
  },
  sectionTitle: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
    marginBottom: theme.spacing.sm,
  },
  list: {
    maxHeight: 300,
  },
  connectionItem: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: theme.spacing.md,
    backgroundColor: theme.colors.gray[50],
    borderRadius: theme.borderRadius.md,
    marginBottom: theme.spacing.sm,
    borderWidth: 2,
    borderColor: 'transparent',
  },
  connectionItemActive: {
    borderColor: theme.colors.primary.main,
    backgroundColor: theme.colors.primary.dark + '30',  // 30% opacity
  },
  connectionContent: {
    flex: 1,
  },
  connectionHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: theme.spacing.sm,
    marginBottom: 4,
  },
  connectionName: {
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
  },
  connectionNameActive: {
    color: theme.colors.primary.dark,
  },
  statusBadge: {
    paddingHorizontal: theme.spacing.sm,
    paddingVertical: 2,
    borderRadius: theme.borderRadius.full,
  },
  statusBadgeText: {
    fontSize: theme.typography.fontSize.xs,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.inverse,
  },
  connectionUrl: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
    fontFamily: 'monospace',
  },
  connectionAuth: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
    marginTop: 2,
  },
  actionButtons: {
    flexDirection: 'row',
    gap: theme.spacing.xs,
  },
  iconButton: {
    padding: theme.spacing.sm,
    borderRadius: theme.borderRadius.sm,
    backgroundColor: theme.colors.gray[100],
  },
  deleteButton: {
    backgroundColor: theme.colors.error.light,
  },
  iconButtonText: {
    fontSize: 16,
  },
  emptyState: {
    padding: theme.spacing.xl,
    alignItems: 'center',
  },
  emptyStateText: {
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.secondary,
    marginBottom: theme.spacing.xs,
  },
  emptyStateSubtext: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.tertiary,
    textAlign: 'center',
  },
  connectSection: {
    marginTop: theme.spacing.md,
    padding: theme.spacing.md,
    backgroundColor: theme.colors.background.primary,
    borderRadius: theme.borderRadius.md,
    borderWidth: 1,
    borderColor: theme.colors.border.light,
  },
  selectedServerInfo: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: theme.spacing.sm,
  },
  selectedLabel: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
    marginRight: theme.spacing.xs,
  },
  selectedName: {
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
  },
  statusMessage: {
    fontSize: theme.typography.fontSize.sm,
    marginBottom: theme.spacing.sm,
  },
  connectButton: {
    backgroundColor: theme.colors.primary.main,
    paddingVertical: theme.spacing.md,
    borderRadius: theme.borderRadius.md,
    alignItems: 'center',
  },
  disconnectButton: {
    backgroundColor: theme.colors.error.main,
  },
  connectingButton: {
    backgroundColor: theme.colors.warning.main,
  },
  connectButtonText: {
    color: theme.colors.primary.contrast,  // Dark text for WCAG AA
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
  },
});

export default ServerConnectionList;
