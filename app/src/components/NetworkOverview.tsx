import React, { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, Alert, StyleSheet, Text, TouchableOpacity, View } from 'react-native';

import {
  Badge,
  Button,
  ButtonRow,
  Card,
  CardWell,
  InlineAlert,
  StatusDot,
  type Tone,
} from '@/components/ui';
import {
  listServices,
  serviceAction,
  getOperation,
  ServiceInfo,
  ServicesResult,
} from '@/services/serviceManager';
import { useTheme, type Theme } from '@/theme';

interface NetworkOverviewProps {
  backendUrl: string;
}

// Poll an async operation to completion (best-effort; bounded).
const waitForOperation = async (
  backendUrl: string,
  operationId: string,
  node?: string | null
): Promise<void> => {
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const op = await getOperation(backendUrl, operationId, node);
      const status = (op.status || '').toLowerCase();
      if (status && status !== 'running' && status !== 'pending' && status !== 'in_progress') {
        return;
      }
    } catch {
      return; // stop polling on error; the refresh will reflect reality
    }
  }
};

const NetworkOverview: React.FC<NetworkOverviewProps> = ({ backendUrl }) => {
  const t = useTheme();
  const s = createStyles(t);

  const [result, setResult] = useState<ServicesResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyService, setBusyService] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const refresh = useCallback(async () => {
    if (!backendUrl.trim()) {
      setResult({ available: false, reason: 'no_backend_url' });
      return;
    }
    setLoading(true);
    try {
      setResult(await listServices(backendUrl));
    } catch (e) {
      setResult({ available: false, reason: e instanceof Error ? e.message : 'error' });
    } finally {
      setLoading(false);
    }
  }, [backendUrl]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const runAction = useCallback(
    async (service: ServiceInfo, action: 'start' | 'stop' | 'restart') => {
      setBusyService(service.name);
      try {
        const res = await serviceAction(backendUrl, service.name, action, service.node);
        const op = res.operation;
        if (op?.id) {
          await waitForOperation(backendUrl, op.id, op.node ?? service.node);
        }
        await refresh();
      } catch (e) {
        Alert.alert('Action Failed', e instanceof Error ? e.message : 'Failed to control service.');
      } finally {
        setBusyService(null);
      }
    },
    [backendUrl, refresh]
  );

  const services = result?.services || [];
  const total = services.length;
  const upCount = services.filter(svc => svc.health && svc.health !== 'stopped').length;

  // Group services by the node (host) they run on.
  const groups = services.reduce<Record<string, ServiceInfo[]>>((acc, svc) => {
    const key = svc.node || 'This device';
    (acc[key] = acc[key] || []).push(svc);
    return acc;
  }, {});

  const renderServiceRow = (svc: ServiceInfo) => {
    // The agent reports `health` (healthy|partial|starting|unhealthy|stopped),
    // not a running boolean. Anything other than "stopped" means it's up.
    const health = svc.health || 'unknown';
    const up = health !== 'stopped' && health !== 'unknown';
    const isBusy = busyService === svc.name;
    const badgeTone: Tone =
      health === 'healthy'
        ? 'success'
        : health === 'stopped' || health === 'unhealthy'
        ? 'danger'
        : health === 'unknown'
        ? 'neutral'
        : 'warning'; // partial | starting
    return (
      <View key={`${svc.node || 'local'}:${svc.name}`} style={s.row}>
        <View style={s.rowHeader}>
          <View style={s.rowHeaderText}>
            <Text style={s.serviceName}>{svc.name}</Text>
            {svc.description ? <Text style={s.descText}>{svc.description}</Text> : null}
            {svc.enabled === false ? <Text style={s.descText}>not enabled in config</Text> : null}
          </View>
          <Badge tone={badgeTone}>{health}</Badge>
        </View>
        <ButtonRow style={s.actions}>
          {isBusy ? (
            <ActivityIndicator size="small" color={t.color.accent.fg} />
          ) : up ? (
            <>
              <Button variant="secondary" size="sm" onPress={() => runAction(svc, 'restart')}>
                Restart
              </Button>
              <Button variant="danger" size="sm" onPress={() => runAction(svc, 'stop')}>
                Stop
              </Button>
            </>
          ) : (
            <Button variant="primary" size="sm" onPress={() => runAction(svc, 'start')}>
              Start
            </Button>
          )}
        </ButtonRow>
      </View>
    );
  };

  const renderUnavailable = () => {
    const reason = result?.reason || 'unavailable';
    const hint =
      reason === 'not_configured'
        ? 'Scan a QR code from the dashboard System page to add a service-manager token.'
        : reason === 'no_backend_url'
        ? 'Set a backend URL first.'
        : 'The service manager is unreachable from here.';
    return (
      <InlineAlert tone="warning" title={`Network control unavailable (${reason}).`}>
        {hint}
      </InlineAlert>
    );
  };

  return (
    <Card>
      <View style={s.header}>
        <TouchableOpacity
          style={s.headerLeft}
          onPress={() => setExpanded(e => !e)}
          activeOpacity={0.7}
        >
          <Text style={s.chevron}>{expanded ? '▾' : '▸'}</Text>
          <Text style={s.sectionTitle}>Network Overview</Text>
        </TouchableOpacity>
        <Button variant="link" size="sm" loading={loading} onPress={refresh}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </Button>
      </View>

      {loading && !result ? <ActivityIndicator size="small" color={t.color.accent.fg} /> : null}

      {result && !result.available ? renderUnavailable() : null}

      {result?.available ? (
        <>
          <TouchableOpacity onPress={() => setExpanded(e => !e)} activeOpacity={0.7}>
            <Text style={s.summary}>
              {total === 0
                ? 'No services reported.'
                : `${total} service${total === 1 ? '' : 's'} · ${upCount} up · ${total - upCount} stopped${expanded ? '' : ' · tap to manage'}`}
            </Text>
          </TouchableOpacity>
          {expanded && total > 0
            ? Object.entries(groups).map(([node, svcs]) => {
                const groupUp = svcs.filter(svc => svc.health && svc.health !== 'stopped').length;
                return (
                  <CardWell key={node} style={s.group}>
                    <View style={s.groupHeaderRow}>
                      <View style={s.groupHeaderLeft}>
                        <StatusDot tone="accent" size={8} style={s.nodeDot} />
                        <Text style={s.groupHeader}>{node}</Text>
                        {svcs[0]?.remote ? (
                          <Badge tone="neutral" style={s.remoteTag}>
                            REMOTE
                          </Badge>
                        ) : null}
                      </View>
                      <Text style={s.groupCount}>{groupUp}/{svcs.length} up</Text>
                    </View>
                    {svcs.map(renderServiceRow)}
                  </CardWell>
                );
              })
            : null}
        </>
      ) : null}
    </Card>
  );
};

const createStyles = (t: Theme) => StyleSheet.create({
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  headerLeft: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
  },
  chevron: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.secondary,
    marginRight: t.space[2],
    width: 14,
  },
  sectionTitle: {
    fontFamily: t.font.sans,
    ...t.type.lg,
    fontWeight: t.weight.semibold,
    color: t.color.text.primary,
  },
  summary: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.secondary,
    marginTop: t.space[2],
  },
  group: {
    marginTop: t.space[4],
    borderLeftWidth: 3,
    borderLeftColor: t.color.accent.base,
  },
  groupHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: t.space[3],
  },
  groupHeaderLeft: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
  },
  nodeDot: {
    marginRight: t.space[2],
  },
  groupHeader: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    fontWeight: t.weight.bold,
    color: t.color.text.primary,
  },
  remoteTag: {
    marginLeft: t.space[2],
  },
  groupCount: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    fontWeight: t.weight.semibold,
    color: t.color.text.muted,
  },
  row: {
    marginBottom: t.space[3],
    padding: t.space[3],
    borderWidth: t.borderWidth,
    borderColor: t.color.border.base,
    borderRadius: t.radius.lg,
    backgroundColor: t.color.surface.raised,
  },
  rowHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
  },
  rowHeaderText: {
    flex: 1,
    marginRight: t.space[2],
  },
  serviceName: {
    fontFamily: t.font.mono,
    ...t.type.sm,
    fontWeight: t.weight.semibold,
    color: t.color.text.primary,
  },
  descText: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.text.muted,
    marginTop: t.space[0.5],
  },
  actions: {
    marginTop: t.space[3],
  },
});

export default NetworkOverview;
