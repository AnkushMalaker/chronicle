import React from 'react';
import { View, Text, FlatList, StyleSheet, SafeAreaView, Share, Platform, Alert } from 'react-native';
import { useTheme, type Theme } from '@/theme';
import { Badge, Button, Heading, Mono, type Tone } from '@/components/ui';
import { useConnectionLog, ConnectionEvent, ConnectionEventType } from '@/contexts/ConnectionLogContext';
import { getLogPath, readLog, clearLog } from '@/utils/logger';

const EVENT_BADGE_TONES: Record<ConnectionEventType, Tone> = {
  scan_start: 'info',
  scan_stop: 'neutral',
  scan_result: 'suggest',
  connect_start: 'warning',
  connect_success: 'success',
  connect_fail: 'danger',
  disconnect: 'danger',
  battery_read: 'success',
  audio_start: 'info',
  audio_stop: 'neutral',
  error: 'danger',
  health_ping: 'success',
  reconnect_attempt: 'warning',
  reconnect_backoff: 'warning',
  bt_state_change: 'suggest',
  ws_connecting: 'warning',
  ws_open: 'success',
  ws_close: 'danger',
  ws_error: 'danger',
  ws_reconnect: 'warning',
  ws_reauth: 'suggest',
  net_change: 'suggest',
};

function formatTime(date: Date): string {
  return date.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function EventItem({ event, t }: { event: ConnectionEvent; t: Theme }) {
  const s = createItemStyles(t);
  const badgeTone = EVENT_BADGE_TONES[event.type] ?? 'neutral';

  return (
    <View style={s.row}>
      <Mono style={s.time}>{formatTime(event.timestamp)}</Mono>
      <Badge tone={badgeTone} mono style={s.badge}>{event.type.replace(/_/g, ' ')}</Badge>
      <View style={s.details}>
        {event.deviceName && <Text style={s.device}>{event.deviceName}</Text>}
        {event.details && <Text style={s.detail} numberOfLines={2}>{event.details}</Text>}
        {event.rssi != null && <Text style={s.detailMuted}>RSSI: {event.rssi} dBm</Text>}
      </View>
    </View>
  );
}

const createItemStyles = (t: Theme) => StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    paddingVertical: t.space[2],
    paddingHorizontal: t.space[3],
    borderBottomWidth: t.borderWidth,
    borderBottomColor: t.color.border.subtle,
  },
  time: {
    fontFamily: t.font.mono,
    width: 65,
    marginTop: 3,
  },
  badge: {
    marginRight: t.space[2],
    marginTop: t.space[0.5],
  },
  details: {
    flex: 1,
  },
  device: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    fontWeight: t.weight.medium,
    color: t.color.text.primary,
  },
  detail: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.secondary,
    marginTop: 1,
  },
  detailMuted: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.muted,
    marginTop: 1,
  },
});

export default function DiagnosticsScreen() {
  const t = useTheme();
  const s = createScreenStyles(t);
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
    <SafeAreaView style={s.safeArea}>
      <View style={s.logBar}>
        <Text style={s.logBarTitle}>Crash Log</Text>
        <Mono style={s.logBarPath} numberOfLines={1}>{getLogPath()}</Mono>
        <View style={s.logBarRow}>
          <Button variant="secondary" size="sm" style={s.logBtn} onPress={shareLogFile}>Share Log File</Button>
          <Button variant="danger" size="sm" style={s.logBtn} onPress={wipeLogFile}>Clear File</Button>
        </View>
      </View>
      <View style={s.header}>
        <Heading>Connection Log ({events.length})</Heading>
        <Button variant="danger" size="sm" onPress={clearEvents}>Clear</Button>
      </View>

      {events.length === 0 ? (
        <View style={s.empty}>
          <Text style={s.emptyText}>No events recorded yet. Scan or connect a device to see events here.</Text>
        </View>
      ) : (
        <FlatList
          data={events}
          renderItem={({ item }) => <EventItem event={item} t={t} />}
          keyExtractor={(item) => item.id}
          style={s.list}
        />
      )}
    </SafeAreaView>
  );
}

const createScreenStyles = (t: Theme) => StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: t.color.surface.page,
  },
  list: {
    backgroundColor: t.color.surface.raised,
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: t.space[4],
    paddingVertical: t.space[3],
    borderBottomWidth: t.borderWidth,
    borderBottomColor: t.color.border.subtle,
  },
  empty: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: t.space[10],
  },
  emptyText: {
    fontFamily: t.font.sans,
    ...t.type.base,
    color: t.color.text.muted,
    textAlign: 'center',
  },
  logBar: {
    paddingHorizontal: t.space[4],
    paddingVertical: t.space[3],
    borderBottomWidth: t.borderWidth,
    borderBottomColor: t.color.border.subtle,
    backgroundColor: t.color.surface.raised,
  },
  logBarTitle: {
    fontFamily: t.font.sans,
    ...t.type.base,
    fontWeight: t.weight.semibold,
    color: t.color.text.primary,
  },
  logBarPath: {
    marginTop: t.space[0.5],
    marginBottom: t.space[2],
  },
  logBarRow: {
    flexDirection: 'row',
    gap: t.space[2],
  },
  logBtn: {
    flex: 1,
  },
});
