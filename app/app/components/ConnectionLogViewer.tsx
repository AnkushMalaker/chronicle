import React, { useState, useMemo } from 'react';
import {
  View,
  Text,
  Modal,
  TouchableOpacity,
  FlatList,
  StyleSheet,
  SafeAreaView,
} from 'react-native';
import theme from '../theme/design-system';
import {
  ConnectionLogEntry,
  ConnectionType,
  ConnectionState,
  CONNECTION_TYPE_LABELS,
  CONNECTION_TYPE_EMOJIS,
  CONNECTION_TYPE_COLORS,
  STATUS_ICONS,
  STATUS_COLORS,
} from '../types/connectionLog';

interface ConnectionLogViewerProps {
  visible: boolean;
  onClose: () => void;
  entries: ConnectionLogEntry[];
  connectionState: ConnectionState;
  onClearLogs: () => void;
}

type FilterType = 'all' | ConnectionType;

const FILTER_OPTIONS: { key: FilterType; label: string; emoji?: string; color?: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'network', label: 'Network', emoji: CONNECTION_TYPE_EMOJIS.network, color: CONNECTION_TYPE_COLORS.network },
  { key: 'server', label: 'Server', emoji: CONNECTION_TYPE_EMOJIS.server, color: CONNECTION_TYPE_COLORS.server },
  { key: 'bluetooth', label: 'Bluetooth', emoji: CONNECTION_TYPE_EMOJIS.bluetooth, color: CONNECTION_TYPE_COLORS.bluetooth },
  { key: 'websocket', label: 'WebSocket', emoji: CONNECTION_TYPE_EMOJIS.websocket, color: CONNECTION_TYPE_COLORS.websocket },
];

export const ConnectionLogViewer: React.FC<ConnectionLogViewerProps> = ({
  visible,
  onClose,
  entries,
  connectionState,
  onClearLogs,
}) => {
  const [activeFilter, setActiveFilter] = useState<FilterType>('all');

  // Filter entries based on selected type
  const filteredEntries = useMemo(() => {
    if (activeFilter === 'all') return entries;
    return entries.filter(entry => entry.type === activeFilter);
  }, [entries, activeFilter]);

  // Get status color from theme
  const getStatusColor = (colorKey: string): string => {
    return theme.colors.status[colorKey as keyof typeof theme.colors.status] || theme.colors.status.unknown;
  };

  // Format timestamp for display
  const formatTime = (date: Date): string => {
    return date.toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  };

  const formatDate = (date: Date): string => {
    const today = new Date();
    const isToday = date.toDateString() === today.toDateString();

    if (isToday) {
      return 'Today';
    }

    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    if (date.toDateString() === yesterday.toDateString()) {
      return 'Yesterday';
    }

    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
    });
  };

  // Render current status summary
  const renderStatusSummary = () => (
    <View style={styles.statusSummary} testID="connection-status-summary">
      {(['network', 'server', 'bluetooth', 'websocket'] as ConnectionType[]).map(type => {
        const status = connectionState[type];
        const colorKey = STATUS_COLORS[status];
        const statusColor = getStatusColor(colorKey);
        const statusIcon = STATUS_ICONS[status];
        const typeColor = CONNECTION_TYPE_COLORS[type];
        const typeEmoji = CONNECTION_TYPE_EMOJIS[type];

        return (
          <View key={type} style={styles.statusItem}>
            <View style={[styles.statusIconContainer, { borderColor: typeColor }]}>
              <Text style={styles.typeEmoji}>{typeEmoji}</Text>
              <View style={[styles.statusDot, { backgroundColor: statusColor }]} />
            </View>
            <Text style={[styles.statusLabel, { color: typeColor }]}>
              {CONNECTION_TYPE_LABELS[type]}
            </Text>
            <Text style={[styles.statusIndicator, { color: statusColor }]}>{statusIcon}</Text>
          </View>
        );
      })}
    </View>
  );

  // Render filter chips
  const renderFilters = () => (
    <View style={styles.filterContainer}>
      {FILTER_OPTIONS.map(option => {
        const isActive = activeFilter === option.key;
        const chipColor = option.color || theme.colors.gray[400];

        return (
          <TouchableOpacity
            key={option.key}
            style={[
              styles.filterChip,
              isActive && [styles.filterChipActive, { borderColor: chipColor, backgroundColor: chipColor + '20' }],
            ]}
            onPress={() => setActiveFilter(option.key)}
            testID={`filter-${option.key}`}
          >
            {option.emoji && (
              <Text style={styles.filterEmoji}>{option.emoji}</Text>
            )}
            <Text
              style={[
                styles.filterText,
                isActive && [styles.filterTextActive, { color: chipColor }],
              ]}
            >
              {option.label}
            </Text>
          </TouchableOpacity>
        );
      })}
    </View>
  );

  // Render individual log entry
  const renderLogEntry = ({ item, index }: { item: ConnectionLogEntry; index: number }) => {
    const colorKey = STATUS_COLORS[item.status];
    const statusColor = getStatusColor(colorKey);
    const statusIcon = STATUS_ICONS[item.status];
    const typeColor = CONNECTION_TYPE_COLORS[item.type];
    const typeEmoji = CONNECTION_TYPE_EMOJIS[item.type];

    // Check if we need to show date header
    const showDateHeader = index === 0 ||
      formatDate(item.timestamp) !== formatDate(filteredEntries[index - 1].timestamp);

    return (
      <>
        {showDateHeader && (
          <View style={styles.dateHeader}>
            <Text style={styles.dateHeaderText}>{formatDate(item.timestamp)}</Text>
          </View>
        )}
        <View style={styles.logEntry} testID={`log-entry-${item.id}`}>
          <View style={styles.logTimeContainer}>
            <Text style={styles.logTime}>{formatTime(item.timestamp)}</Text>
          </View>
          <View style={[styles.logIndicator, { backgroundColor: typeColor }]} />
          <View style={styles.logContent}>
            <View style={styles.logHeader}>
              <Text style={styles.logTypeEmoji}>{typeEmoji}</Text>
              <Text style={[styles.logType, { color: typeColor }]}>
                {CONNECTION_TYPE_LABELS[item.type]}
              </Text>
              <Text style={[styles.logStatusIcon, { color: statusColor }]}>{statusIcon}</Text>
            </View>
            <Text style={styles.logMessage}>{item.message}</Text>
            {item.details && (
              <Text style={styles.logDetails}>{item.details}</Text>
            )}
          </View>
        </View>
      </>
    );
  };

  // Empty state
  const renderEmptyState = () => (
    <View style={styles.emptyState}>
      <Text style={styles.emptyStateIcon}>📋</Text>
      <Text style={styles.emptyStateText}>No log entries</Text>
      <Text style={styles.emptyStateSubtext}>
        Connection events will appear here as they occur
      </Text>
    </View>
  );

  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onClose}
    >
      <SafeAreaView style={styles.container}>
        {/* Header */}
        <View style={styles.header}>
          <Text style={styles.title}>Connection Logs</Text>
          <TouchableOpacity
            style={styles.closeButton}
            onPress={onClose}
            testID="close-logs-button"
          >
            <Text style={styles.closeButtonText}>Done</Text>
          </TouchableOpacity>
        </View>

        {/* Current Status Summary */}
        {renderStatusSummary()}

        {/* Filters */}
        {renderFilters()}

        {/* Log Count */}
        <View style={styles.countContainer}>
          <Text style={styles.countText}>
            {filteredEntries.length} {filteredEntries.length === 1 ? 'entry' : 'entries'}
          </Text>
          {entries.length > 0 && (
            <TouchableOpacity
              style={styles.clearButton}
              onPress={onClearLogs}
              testID="clear-logs-button"
            >
              <Text style={styles.clearButtonText}>Clear All</Text>
            </TouchableOpacity>
          )}
        </View>

        {/* Log List */}
        <FlatList
          data={filteredEntries}
          keyExtractor={item => item.id}
          renderItem={renderLogEntry}
          ListEmptyComponent={renderEmptyState}
          contentContainerStyle={styles.listContent}
          showsVerticalScrollIndicator={true}
        />
      </SafeAreaView>
    </Modal>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: theme.colors.background.primary,
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: theme.colors.border.light,
  },
  title: {
    fontSize: theme.typography.fontSize.xl,
    fontWeight: theme.typography.fontWeight.bold,
    color: theme.colors.text.primary,
  },
  closeButton: {
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
  },
  closeButtonText: {
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.primary.main,
  },
  statusSummary: {
    flexDirection: 'row',
    justifyContent: 'space-around',
    paddingVertical: theme.spacing.md,
    paddingHorizontal: theme.spacing.sm,
    backgroundColor: theme.colors.background.secondary,
    borderBottomWidth: 1,
    borderBottomColor: theme.colors.border.light,
  },
  statusItem: {
    alignItems: 'center',
    gap: 4,
  },
  statusIconContainer: {
    width: 44,
    height: 44,
    borderRadius: 22,
    borderWidth: 2,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: theme.colors.background.tertiary,
    position: 'relative',
  },
  typeEmoji: {
    fontSize: 20,
  },
  statusDot: {
    position: 'absolute',
    bottom: -2,
    right: -2,
    width: 14,
    height: 14,
    borderRadius: 7,
    borderWidth: 2,
    borderColor: theme.colors.background.secondary,
  },
  statusLabel: {
    fontSize: theme.typography.fontSize.xs,
    fontWeight: theme.typography.fontWeight.medium,
  },
  statusIndicator: {
    fontSize: 12,
    fontWeight: theme.typography.fontWeight.bold,
  },
  filterContainer: {
    flexDirection: 'row',
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
    gap: theme.spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: theme.colors.border.light,
  },
  filterChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.xs,
    borderRadius: theme.borderRadius.full,
    backgroundColor: theme.colors.gray[100],
    borderWidth: 2,
    borderColor: theme.colors.border.light,
  },
  filterChipActive: {
    // Colors applied dynamically in component
  },
  filterEmoji: {
    fontSize: 14,
  },
  filterText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
  },
  filterTextActive: {
    fontWeight: theme.typography.fontWeight.semibold,
  },
  countContainer: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
  },
  countText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.tertiary,
  },
  clearButton: {
    paddingHorizontal: theme.spacing.sm,
    paddingVertical: theme.spacing.xs,
  },
  clearButtonText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.error.main,
  },
  listContent: {
    paddingBottom: theme.spacing.xl,
  },
  dateHeader: {
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
    backgroundColor: theme.colors.background.secondary,
  },
  dateHeaderText: {
    fontSize: theme.typography.fontSize.xs,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.tertiary,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  logEntry: {
    flexDirection: 'row',
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: theme.colors.border.light,
  },
  logTimeContainer: {
    width: 70,
    marginRight: theme.spacing.sm,
  },
  logTime: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
    fontFamily: 'monospace',
  },
  logIndicator: {
    width: 3,
    borderRadius: 1.5,
    marginRight: theme.spacing.sm,
  },
  logContent: {
    flex: 1,
  },
  logHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: theme.spacing.xs,
    marginBottom: 2,
  },
  logTypeEmoji: {
    fontSize: 14,
  },
  logType: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
    // Color applied dynamically
  },
  logStatusIcon: {
    fontSize: 12,
    fontWeight: theme.typography.fontWeight.bold,
    marginLeft: 4,
  },
  logMessage: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
  },
  logDetails: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
    marginTop: 2,
    fontFamily: 'monospace',
  },
  emptyState: {
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: theme.spacing.xxl,
  },
  emptyStateIcon: {
    fontSize: 48,
    marginBottom: theme.spacing.md,
  },
  emptyStateText: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.secondary,
    marginBottom: theme.spacing.xs,
  },
  emptyStateSubtext: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.tertiary,
    textAlign: 'center',
  },
});

export default ConnectionLogViewer;
