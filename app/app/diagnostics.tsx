import React from 'react';
import { View, Text, FlatList, TouchableOpacity, StyleSheet, SafeAreaView } from 'react-native';
import { useTheme, ThemeColors } from '@/theme';
import { useConnectionLog, ConnectionEvent, ConnectionEventType } from '@/contexts/ConnectionLogContext';

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

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.background }}>
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
});
