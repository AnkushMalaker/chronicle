import React from 'react';
import { View, Text, FlatList, TouchableOpacity, StyleSheet, SafeAreaView, Share, Platform, Alert } from 'react-native';
import { useTheme, ThemeColors } from '@/theme';
import { useConnectionLog, ConnectionEvent, ConnectionEventType } from '@/contexts/ConnectionLogContext';
import { getLogPath, readLog, clearLog } from '@/utils/logger';

const EVENT_BADGE_COLORS: Record<ConnectionEventType, string> = {
  scan_start: '#007AFF',
  scan_stop: '#8E8E93',
  scan_result: '#5856D6',
  connect_start: '#FF9500',
  connect_success: '#34C759',
  connect_fail: '#FF3B30',
  disconnect: '#FF3B30',
  battery_read: '#34C759',
  audio_start: '#007AFF',
  audio_stop: '#8E8E93',
  error: '#FF3B30',
  health_ping: '#34C759',
  reconnect_attempt: '#FF9500',
  reconnect_backoff: '#FF9500',
  bt_state_change: '#5856D6',
};

function formatTime(date: Date): string {
  return date.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function EventItem({ event, colors }: { event: ConnectionEvent; colors: ThemeColors }) {
  const badgeColor = EVENT_BADGE_COLORS[event.type] || colors.textTertiary;

  return (
    <View style={[itemStyles.row, { borderBottomColor: colors.separator }]}>
      <Text style={[itemStyles.time, { color: colors.textTertiary }]}>{formatTime(event.timestamp)}</Text>
      <View style={[itemStyles.badge, { backgroundColor: badgeColor }]}>
        <Text style={itemStyles.badgeText}>{event.type.replace(/_/g, ' ')}</Text>
      </View>
      <View style={itemStyles.details}>
        {event.deviceName && <Text style={[itemStyles.device, { color: colors.text }]}>{event.deviceName}</Text>}
        {event.details && <Text style={[itemStyles.detail, { color: colors.textSecondary }]} numberOfLines={2}>{event.details}</Text>}
        {event.rssi != null && <Text style={[itemStyles.detail, { color: colors.textTertiary }]}>RSSI: {event.rssi} dBm</Text>}
      </View>
    </View>
  );
}

const itemStyles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    paddingVertical: 8,
    paddingHorizontal: 12,
    borderBottomWidth: 1,
  },
  time: {
    fontSize: 11,
    fontFamily: 'monospace',
    width: 65,
    marginTop: 3,
  },
  badge: {
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    marginRight: 8,
    marginTop: 2,
  },
  badgeText: {
    color: 'white',
    fontSize: 10,
    fontWeight: '600',
    textTransform: 'uppercase',
  },
  details: {
    flex: 1,
  },
  device: {
    fontSize: 13,
    fontWeight: '500',
  },
  detail: {
    fontSize: 12,
    marginTop: 1,
  },
});

export default function DiagnosticsScreen() {
  const { colors } = useTheme();
  const { events, clearEvents } = useConnectionLog();

  const shareLogFile = async () => {
    try {
      const contents = await readLog();
      if (!contents) {
        Alert.alert('No log yet', 'The crash log file is empty.');
        return;
      }
      if (Platform.OS === 'ios') {
        await Share.share({ url: `file://${getLogPath()}`, message: contents.slice(-4000) });
      } else {
        await Share.share({ message: contents.slice(-4000) });
      }
    } catch (err) {
      Alert.alert('Share failed', String(err));
    }
  };

  const wipeLogFile = async () => {
    Alert.alert('Clear crash log?', 'Removes the on-device crash log file.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Clear', style: 'destructive', onPress: async () => { await clearLog(); } },
    ]);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.background }}>
      <View style={[screenStyles.logBar, { borderBottomColor: colors.separator, backgroundColor: colors.card }]}>
        <Text style={[screenStyles.logBarTitle, { color: colors.text }]}>Crash Log</Text>
        <Text style={[screenStyles.logBarPath, { color: colors.textTertiary }]} numberOfLines={1}>{getLogPath()}</Text>
        <View style={screenStyles.logBarRow}>
          <TouchableOpacity onPress={shareLogFile} style={[screenStyles.logBtn, { backgroundColor: colors.inputBackground }]}>
            <Text style={[screenStyles.logBtnText, { color: colors.text }]}>Share Log File</Text>
          </TouchableOpacity>
          <TouchableOpacity onPress={wipeLogFile} style={[screenStyles.logBtn, { backgroundColor: colors.inputBackground }]}>
            <Text style={[screenStyles.logBtnText, { color: colors.danger }]}>Clear File</Text>
          </TouchableOpacity>
        </View>
      </View>
      <View style={[screenStyles.header, { borderBottomColor: colors.separator }]}>
        <Text style={[screenStyles.title, { color: colors.text }]}>Connection Log ({events.length})</Text>
        <TouchableOpacity onPress={clearEvents} style={[screenStyles.clearButton, { backgroundColor: colors.inputBackground }]}>
          <Text style={[screenStyles.clearText, { color: colors.danger }]}>Clear</Text>
        </TouchableOpacity>
      </View>

      {events.length === 0 ? (
        <View style={screenStyles.empty}>
          <Text style={[screenStyles.emptyText, { color: colors.textTertiary }]}>No events recorded yet. Scan or connect a device to see events here.</Text>
        </View>
      ) : (
        <FlatList
          data={events}
          renderItem={({ item }) => <EventItem event={item} colors={colors} />}
          keyExtractor={(item) => item.id}
          style={{ backgroundColor: colors.card }}
        />
      )}
    </SafeAreaView>
  );
}

const screenStyles = StyleSheet.create({
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 1,
  },
  title: {
    fontSize: 17,
    fontWeight: '600',
  },
  clearButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 6,
  },
  clearText: {
    fontSize: 14,
    fontWeight: '500',
  },
  empty: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 40,
  },
  emptyText: {
    fontSize: 15,
    textAlign: 'center',
  },
  logBar: {
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderBottomWidth: 1,
  },
  logBarTitle: {
    fontSize: 15,
    fontWeight: '600',
  },
  logBarPath: {
    fontSize: 10,
    fontFamily: 'monospace',
    marginTop: 2,
    marginBottom: 8,
  },
  logBarRow: {
    flexDirection: 'row',
    gap: 8,
  },
  logBtn: {
    flex: 1,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 6,
    alignItems: 'center',
  },
  logBtnText: {
    fontSize: 13,
    fontWeight: '500',
  },
});
