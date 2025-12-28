/**
 * OfflineBanner - UI indicator for offline recording mode
 *
 * Shows:
 * - Recording indicator when buffering offline
 * - Buffered audio duration
 * - Pending segments count
 * - Sync progress when uploading
 * - Storage warning
 */

import React from 'react';
import {
  View,
  Text,
  StyleSheet,
  Animated,
  TouchableOpacity,
} from 'react-native';
import theme from '../theme/design-system';
import { OfflineStorageStats, PendingSegment } from '../storage/offlineStorage';
import { SyncProgress } from '../services/offlineSync';

interface OfflineBannerProps {
  visible: boolean;
  isBuffering: boolean;
  bufferDurationMs: number;
  pendingSegments: PendingSegment[];
  stats: OfflineStorageStats;
  storageWarning: boolean;
  syncProgress?: SyncProgress | null;
  onSyncPress?: () => void;
}

export const OfflineBanner: React.FC<OfflineBannerProps> = ({
  visible,
  isBuffering,
  bufferDurationMs,
  pendingSegments,
  stats,
  storageWarning,
  syncProgress,
  onSyncPress,
}) => {
  const pulseAnim = React.useRef(new Animated.Value(1)).current;

  // Pulsing animation for recording indicator
  React.useEffect(() => {
    if (isBuffering) {
      const pulse = Animated.loop(
        Animated.sequence([
          Animated.timing(pulseAnim, {
            toValue: 0.4,
            duration: 800,
            useNativeDriver: true,
          }),
          Animated.timing(pulseAnim, {
            toValue: 1,
            duration: 800,
            useNativeDriver: true,
          }),
        ])
      );
      pulse.start();
      return () => pulse.stop();
    } else {
      pulseAnim.setValue(1);
    }
  }, [isBuffering, pulseAnim]);

  if (!visible) return null;

  const formatDuration = (ms: number): string => {
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
  };

  const formatBytes = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const pendingCount = pendingSegments.length;
  const isSyncing = syncProgress?.inProgress;

  return (
    <View style={styles.container} testID="offline-banner">
      {/* Recording indicator */}
      {isBuffering && (
        <View style={styles.recordingSection}>
          <Animated.View
            style={[
              styles.recordingDot,
              { opacity: pulseAnim },
            ]}
          />
          <Text style={styles.recordingText}>
            Offline Recording
          </Text>
          <Text style={styles.durationText}>
            {formatDuration(bufferDurationMs)}
          </Text>
        </View>
      )}

      {/* Pending segments info */}
      {!isBuffering && pendingCount > 0 && (
        <View style={styles.pendingSection}>
          <View style={styles.pendingIcon}>
            <Text style={styles.pendingIconText}>!</Text>
          </View>
          <View style={styles.pendingInfo}>
            <Text style={styles.pendingTitle}>
              {pendingCount} segment{pendingCount !== 1 ? 's' : ''} pending
            </Text>
            <Text style={styles.pendingSubtitle}>
              {formatBytes(stats.totalBytes)} buffered
            </Text>
          </View>

          {/* Sync button or progress */}
          {isSyncing ? (
            <View style={styles.syncProgress}>
              <Text style={styles.syncProgressText}>
                {syncProgress.completed}/{syncProgress.total}
              </Text>
            </View>
          ) : (
            <TouchableOpacity
              style={styles.syncButton}
              onPress={onSyncPress}
              testID="sync-button"
            >
              <Text style={styles.syncButtonText}>Sync</Text>
            </TouchableOpacity>
          )}
        </View>
      )}

      {/* Storage warning */}
      {storageWarning && (
        <View style={styles.warningSection}>
          <Text style={styles.warningIcon}>!</Text>
          <Text style={styles.warningText}>
            Storage nearly full ({formatBytes(stats.totalBytes)})
          </Text>
        </View>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: theme.colors.background.tertiary,
    borderBottomWidth: 1,
    borderBottomColor: theme.colors.border.light,
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.sm,
  },

  // Recording state
  recordingSection: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: theme.spacing.sm,
  },
  recordingDot: {
    width: 12,
    height: 12,
    borderRadius: 6,
    backgroundColor: theme.colors.error.main,
  },
  recordingText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.error.main,
    flex: 1,
  },
  durationText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.secondary,
    fontFamily: 'monospace',
  },

  // Pending segments
  pendingSection: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: theme.spacing.sm,
  },
  pendingIcon: {
    width: 24,
    height: 24,
    borderRadius: 12,
    backgroundColor: theme.colors.warning.main,
    alignItems: 'center',
    justifyContent: 'center',
  },
  pendingIconText: {
    fontSize: 14,
    fontWeight: theme.typography.fontWeight.bold,
    color: theme.colors.text.inverse,
  },
  pendingInfo: {
    flex: 1,
  },
  pendingTitle: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.text.primary,
  },
  pendingSubtitle: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.tertiary,
  },

  // Sync button
  syncButton: {
    backgroundColor: theme.colors.primary.main,
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.xs,
    borderRadius: theme.borderRadius.sm,
  },
  syncButtonText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.semibold,
    color: theme.colors.primary.contrast,
  },

  // Sync progress
  syncProgress: {
    paddingHorizontal: theme.spacing.md,
    paddingVertical: theme.spacing.xs,
  },
  syncProgressText: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.secondary,
  },

  // Storage warning
  warningSection: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: theme.spacing.sm,
    marginTop: theme.spacing.xs,
    backgroundColor: theme.colors.warning.background,
    padding: theme.spacing.sm,
    borderRadius: theme.borderRadius.sm,
  },
  warningIcon: {
    fontSize: 16,
    fontWeight: theme.typography.fontWeight.bold,
    color: theme.colors.warning.main,
  },
  warningText: {
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.warning.main,
    flex: 1,
  },
});

export default OfflineBanner;
