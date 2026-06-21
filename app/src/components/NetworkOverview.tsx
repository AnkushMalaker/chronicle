import React, { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, Alert, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { useTheme, ThemeColors } from '../theme';
import {
  listServices,
  serviceAction,
  getOperation,
  ServiceInfo,
  ServicesResult,
} from '../services/serviceManager';

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
  const { colors } = useTheme();
  const s = createStyles(colors);

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
    const badgeColor =
      health === 'healthy'
        ? colors.success
        : health === 'stopped' || health === 'unhealthy'
        ? colors.danger
        : health === 'unknown'
        ? colors.disabled
        : colors.warning; // partial | starting
    return (
      <View key={`${svc.node || 'local'}:${svc.name}`} style={s.row}>
        <View style={s.rowHeader}>
          <View style={s.rowHeaderText}>
            <Text style={s.serviceName}>{svc.name}</Text>
            {svc.description ? <Text style={s.descText}>{svc.description}</Text> : null}
            {svc.enabled === false ? <Text style={s.descText}>not enabled in config</Text> : null}
          </View>
          <View style={[s.badge, { backgroundColor: badgeColor }]}>
            <Text style={s.badgeText}>{health}</Text>
          </View>
        </View>
        <View style={s.actions}>
          {isBusy ? (
            <ActivityIndicator size="small" color={colors.primary} />
          ) : up ? (
            <>
              <TouchableOpacity style={s.actionButton} onPress={() => runAction(svc, 'restart')}>
                <Text style={s.actionText}>Restart</Text>
              </TouchableOpacity>
              <TouchableOpacity style={s.actionButton} onPress={() => runAction(svc, 'stop')}>
                <Text style={s.actionText}>Stop</Text>
              </TouchableOpacity>
            </>
          ) : (
            <TouchableOpacity
              style={[s.actionButton, s.startButton]}
              onPress={() => runAction(svc, 'start')}
            >
              <Text style={[s.actionText, s.startText]}>Start</Text>
            </TouchableOpacity>
          )}
        </View>
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
      <View>
        <Text style={s.emptyText}>Network control unavailable ({reason}).</Text>
        <Text style={s.hintText}>{hint}</Text>
      </View>
    );
  };

  return (
    <View style={s.section}>
      <View style={s.header}>
        <TouchableOpacity
          style={s.headerLeft}
          onPress={() => setExpanded(e => !e)}
          activeOpacity={0.7}
        >
          <Text style={s.chevron}>{expanded ? '▾' : '▸'}</Text>
          <Text style={s.sectionTitle}>Network Overview</Text>
        </TouchableOpacity>
        <TouchableOpacity onPress={refresh} disabled={loading}>
          <Text style={s.refreshText}>{loading ? 'Refreshing…' : 'Refresh'}</Text>
        </TouchableOpacity>
      </View>

      {loading && !result ? <ActivityIndicator size="small" color={colors.primary} /> : null}

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
                  <View key={node} style={s.group}>
                    <View style={s.groupHeaderRow}>
                      <View style={s.groupHeaderLeft}>
                        <View style={s.nodeDot} />
                        <Text style={s.groupHeader}>{node}</Text>
                        {svcs[0]?.remote ? <Text style={s.remoteTag}>REMOTE</Text> : null}
                      </View>
                      <Text style={s.groupCount}>{groupUp}/{svcs.length} up</Text>
                    </View>
                    {svcs.map(renderServiceRow)}
                  </View>
                );
              })
            : null}
        </>
      ) : null}
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: colors.card,
    borderRadius: 10,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.1,
    shadowRadius: 3,
    elevation: 2,
  },
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
    fontSize: 14,
    color: colors.textSecondary,
    marginRight: 8,
    width: 14,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    color: colors.text,
  },
  refreshText: {
    fontSize: 13,
    color: colors.primary,
    fontWeight: '600',
  },
  summary: {
    fontSize: 13,
    color: colors.textSecondary,
    marginTop: 8,
  },
  group: {
    marginTop: 16,
    backgroundColor: colors.background,
    borderRadius: 10,
    borderLeftWidth: 3,
    borderLeftColor: colors.primary,
    padding: 10,
  },
  groupHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 10,
  },
  groupHeaderLeft: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
  },
  nodeDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.primary,
    marginRight: 8,
  },
  groupHeader: {
    fontSize: 14,
    fontWeight: '700',
    color: colors.text,
  },
  remoteTag: {
    marginLeft: 8,
    fontSize: 10,
    fontWeight: '700',
    color: colors.textTertiary,
    borderWidth: 1,
    borderColor: colors.separator,
    borderRadius: 4,
    paddingHorizontal: 5,
    paddingVertical: 1,
    overflow: 'hidden',
  },
  groupCount: {
    fontSize: 12,
    fontWeight: '600',
    color: colors.textTertiary,
  },
  row: {
    marginBottom: 10,
    padding: 10,
    borderWidth: 1,
    borderColor: colors.separator,
    borderRadius: 8,
    backgroundColor: colors.card,
  },
  rowHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
  },
  rowHeaderText: {
    flex: 1,
    marginRight: 8,
  },
  serviceName: {
    fontSize: 14,
    fontWeight: '600',
    color: colors.text,
  },
  descText: {
    fontSize: 12,
    color: colors.textTertiary,
    marginTop: 2,
  },
  badge: {
    paddingVertical: 3,
    paddingHorizontal: 8,
    borderRadius: 10,
  },
  badgeText: {
    color: '#fff',
    fontSize: 11,
    fontWeight: '700',
  },
  nodeText: {
    fontSize: 12,
    color: colors.textTertiary,
    marginTop: 4,
  },
  actions: {
    flexDirection: 'row',
    gap: 8,
    marginTop: 10,
  },
  actionButton: {
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    backgroundColor: colors.inputBackground,
  },
  startButton: {
    backgroundColor: colors.primary,
    borderColor: colors.primary,
  },
  actionText: {
    color: colors.textSecondary,
    fontSize: 12,
    fontWeight: '600',
  },
  startText: {
    color: '#fff',
  },
  emptyText: {
    color: colors.textTertiary,
    fontSize: 13,
    fontStyle: 'italic',
  },
  hintText: {
    color: colors.textSecondary,
    fontSize: 12,
    marginTop: 6,
  },
});

export default NetworkOverview;
