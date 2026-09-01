import React, { useCallback, useEffect, useState } from 'react';
import { Linking, StyleSheet, View } from 'react-native';

import { Body, Button, Card, Caption } from '@/components/ui';
import {
  enablePushNotifications,
  notificationPermissionState,
  type NotificationPermissionState,
} from '@/services/pushNotifications';
import { useTheme, type Theme } from '@/theme';

export default function NotificationsSection({ backendUrl, authenticated }: { backendUrl: string; authenticated: boolean }) {
  const t = useTheme();
  const s = createStyles(t);
  const [permission, setPermission] = useState<NotificationPermissionState>('undetermined');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    void notificationPermissionState().then(setPermission).catch(() => setPermission('unsupported'));
  }, []);
  useEffect(refresh, [refresh]);

  const enable = async () => {
    setBusy(true);
    setError(null);
    try {
      setPermission(await enablePushNotifications(backendUrl));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not enable notifications.');
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Notifications">
      <View style={s.row}>
        <View style={s.copy}>
          <Body>Permission: {permission}</Body>
          <Caption>Priority reminders and agent messages use the phone’s normal notification settings.</Caption>
        </View>
        {permission === 'granted' ? (
          <Button size="sm" variant="outline" onPress={() => Linking.openSettings()}>System settings</Button>
        ) : permission === 'denied' ? (
          <Button size="sm" variant="outline" onPress={() => Linking.openSettings()}>Open settings</Button>
        ) : (
          <Button size="sm" onPress={enable} loading={busy} disabled={!authenticated || permission === 'unsupported'}>Enable notifications</Button>
        )}
      </View>
      {!authenticated && <Caption style={s.note}>Log in before enabling notifications.</Caption>}
      {error && <Caption style={s.error}>{error}</Caption>}
    </Card>
  );
}

const createStyles = (t: Theme) => StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: t.space[3] },
  copy: { flex: 1, gap: t.space[1] },
  note: { marginTop: t.space[2] },
  error: { marginTop: t.space[2], color: t.color.status.danger.fg },
});
