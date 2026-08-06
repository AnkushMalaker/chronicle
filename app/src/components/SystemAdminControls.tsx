import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, StyleSheet, Text, View, ActivityIndicator } from 'react-native';

import { Button, ButtonRow, Card, CardWell, Divider } from '@/components/ui';
import { useTheme, type Theme } from '@/theme';

interface WakewordEntry {
  name: string;
  mode: 'dispatch' | 'collect_only' | 'off';
  collect_only?: boolean;
  disabled?: boolean;
}

interface WakewordModeResponse {
  global_mode?: 'dispatch' | 'collect_only' | 'off' | 'mixed';
  wakewords?: WakewordEntry[];
}

interface WakewordModelsResponse {
  wakewords?: Array<{
    name: string;
    collect_only?: boolean;
    disabled?: boolean;
  }>;
}

interface SystemAdminControlsProps {
  backendUrl: string;
  jwtToken: string | null;
}

const toHttpBaseUrl = (backendUrl: string): string | null => {
  const trimmed = backendUrl.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('ws://')) return trimmed.replace('ws://', 'http://').split('/ws')[0];
  if (trimmed.startsWith('wss://')) return trimmed.replace('wss://', 'https://').split('/ws')[0];
  if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) return trimmed.split('/ws')[0];
  return null;
};

const normalizeMode = (entry: WakewordEntry): 'dispatch' | 'collect_only' | 'off' => {
  if (entry.mode) return entry.mode;
  if (entry.disabled) return 'off';
  if (entry.collect_only) return 'collect_only';
  return 'dispatch';
};

const SystemAdminControls: React.FC<SystemAdminControlsProps> = ({ backendUrl, jwtToken }) => {
  const t = useTheme();
  const s = createStyles(t);

  const baseUrl = useMemo(() => toHttpBaseUrl(backendUrl), [backendUrl]);

  const [wakewords, setWakewords] = useState<WakewordEntry[]>([]);
  const [loadingModes, setLoadingModes] = useState(false);
  const [updatingWord, setUpdatingWord] = useState<string | null>(null);
  const [modeError, setModeError] = useState<string | null>(null);
  const [wakewordUnavailable, setWakewordUnavailable] = useState(false);
  const [supportsModeEndpoint, setSupportsModeEndpoint] = useState<boolean | null>(null);
  const [restarting, setRestarting] = useState(false);

  const authHeaders = useMemo(() => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (jwtToken) headers.Authorization = `Bearer ${jwtToken}`;
    return headers;
  }, [jwtToken]);

  const applyModeResponse = useCallback((data: WakewordModeResponse) => {
    const rows = (data.wakewords || []).map((w) => ({
      ...w,
      mode: normalizeMode(w),
    }));
    setWakewords(rows);
  }, []);

  const fetchModes = useCallback(async () => {
    if (!baseUrl) {
      setWakewords([]);
      setModeError('Set backend URL first.');
      return;
    }

    setLoadingModes(true);
    setModeError(null);
    setWakewordUnavailable(false);
    try {
      const modeResponse = await fetch(`${baseUrl}/api/wakeword/mode`, {
        method: 'GET',
        headers: authHeaders,
      });

      if (modeResponse.ok) {
        const data = (await modeResponse.json()) as WakewordModeResponse;
        applyModeResponse(data);
        setSupportsModeEndpoint(true);
        return;
      }

      // Backward-compatibility: older backends may not have /mode yet.
      if (modeResponse.status === 404) {
        const modelsResponse = await fetch(`${baseUrl}/api/wakeword/models`, {
          method: 'GET',
          headers: authHeaders,
        });

        if (modelsResponse.ok) {
          const modelsData = (await modelsResponse.json()) as WakewordModelsResponse;
          const rows: WakewordEntry[] = (modelsData.wakewords || []).map((w) => ({
            name: w.name,
            mode: w.disabled ? 'off' : w.collect_only ? 'collect_only' : 'dispatch',
            collect_only: !!w.collect_only,
            disabled: !!w.disabled,
          }));
          setWakewords(rows);
          setSupportsModeEndpoint(false);
          return;
        }

        if (modelsResponse.status === 404) {
          // Simple backend or wakeword service not enabled.
          setWakewordUnavailable(true);
          setWakewords([]);
          setSupportsModeEndpoint(false);
          return;
        }

        const body = await modelsResponse.text();
        throw new Error(`Failed to fetch wakeword models (${modelsResponse.status}): ${body}`);
      }

      const body = await modeResponse.text();
      throw new Error(`Failed to fetch wakeword mode (${modeResponse.status}): ${body}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to load wakeword modes';
      setModeError(message);
      setWakewords([]);
      setWakewordUnavailable(false);
      setSupportsModeEndpoint(null);
    } finally {
      setLoadingModes(false);
    }
  }, [applyModeResponse, authHeaders, baseUrl]);

  useEffect(() => {
    fetchModes();
  }, [fetchModes]);

  const setWordMode = useCallback(async (wakeword: string, mode: 'dispatch' | 'collect_only' | 'off') => {
    if (!baseUrl) {
      Alert.alert('Backend URL Required', 'Set backend URL first.');
      return;
    }

    setUpdatingWord(wakeword);
    setModeError(null);
    try {
      if (supportsModeEndpoint !== false) {
        const response = await fetch(`${baseUrl}/api/wakeword/mode`, {
          method: 'POST',
          headers: authHeaders,
          body: JSON.stringify({ wakeword, mode }),
        });

        if (response.ok) {
          const data = (await response.json()) as WakewordModeResponse;
          applyModeResponse(data);
          setSupportsModeEndpoint(true);
          return;
        }

        // Fall back for older backend that lacks /mode.
        if (response.status !== 404) {
          const text = await response.text();
          throw new Error(text || `Failed to set ${wakeword} mode`);
        }
      }

      setSupportsModeEndpoint(false);

      const postDisabled = async (disabled: boolean, strict: boolean): Promise<void> => {
        const resp = await fetch(`${baseUrl}/api/wakeword/disabled`, {
          method: 'POST',
          headers: authHeaders,
          body: JSON.stringify({ wakeword, disabled }),
        });
        if (resp.status === 404 && !strict) return;
        if (!resp.ok) {
          const text = await resp.text();
          if (resp.status === 404 && strict) {
            throw new Error("This backend doesn't support per-wakeword OFF yet.");
          }
          throw new Error(text || `Failed to update disabled state for ${wakeword}`);
        }
      };

      const postCollectOnly = async (collectOnly: boolean): Promise<void> => {
        const resp = await fetch(`${baseUrl}/api/wakeword/collect_only`, {
          method: 'POST',
          headers: authHeaders,
          body: JSON.stringify({ wakeword, collect_only: collectOnly }),
        });
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `Failed to set collect_only for ${wakeword}`);
        }
      };

      if (mode === 'dispatch') {
        await postDisabled(false, false);
        await postCollectOnly(false);
      } else if (mode === 'collect_only') {
        await postDisabled(false, false);
        await postCollectOnly(true);
      } else {
        await postCollectOnly(false);
        await postDisabled(true, true);
      }

      await fetchModes();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to update wakeword mode';
      setModeError(message);
      Alert.alert('Wakeword Update Failed', message);
    } finally {
      setUpdatingWord(null);
    }
  }, [applyModeResponse, authHeaders, baseUrl, fetchModes, supportsModeEndpoint]);

  const restartBackend = useCallback(async () => {
    if (!baseUrl) {
      Alert.alert('Backend URL Required', 'Set backend URL first.');
      return;
    }

    Alert.alert(
      'Restart Backend?',
      'This will briefly disconnect active sessions and streams.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Restart',
          style: 'destructive',
          onPress: async () => {
            setRestarting(true);
            try {
              const response = await fetch(`${baseUrl}/api/admin/system/restart-backend`, {
                method: 'POST',
                headers: authHeaders,
              });
              const body = await response.text();
              if (!response.ok) {
                throw new Error(body || `Restart failed (${response.status})`);
              }
              Alert.alert('Backend Restart Requested', 'Restart has been scheduled.');
            } catch (error) {
              const message = error instanceof Error ? error.message : 'Failed to restart backend';
              Alert.alert('Restart Failed', message);
            } finally {
              setRestarting(false);
            }
          },
        },
      ]
    );
  }, [authHeaders, baseUrl]);

  return (
    <Card title="System Controls">
      <Button
        variant="danger"
        size="md"
        fullWidth
        loading={restarting}
        disabled={restarting}
        onPress={restartBackend}
      >
        Restart Backend
      </Button>

      <Divider style={s.subSectionDivider} />

      <View style={s.subSection}>
        <View style={s.subSectionHeader}>
          <Text style={s.subSectionTitle}>Wakeword Modes</Text>
          <Button variant="link" size="sm" loading={loadingModes} onPress={fetchModes}>
            {loadingModes ? 'Refreshing...' : 'Refresh'}
          </Button>
        </View>

        {wakewordUnavailable ? (
          <Text style={s.emptyText}>Wakeword controls are not available on this backend.</Text>
        ) : null}

        {loadingModes ? (
          <ActivityIndicator size="small" color={t.color.accent.fg} />
        ) : wakewords.length === 0 && !wakewordUnavailable ? (
          <Text style={s.emptyText}>No wakewords found.</Text>
        ) : (
          wakewords.map((w) => (
            <CardWell key={w.name} style={s.row}>
              <Text style={s.wordName}>{w.name}</Text>
              <ButtonRow>
                {(['dispatch', 'collect_only', 'off'] as const).map((mode) => {
                  const isActive = w.mode === mode;
                  const isUpdating = updatingWord === w.name;
                  return (
                    <Button
                      key={mode}
                      variant={isActive ? 'primary' : 'secondary'}
                      size="sm"
                      onPress={() => setWordMode(w.name, mode)}
                      disabled={isUpdating}
                    >
                      {mode === 'collect_only' ? 'collect' : mode}
                    </Button>
                  );
                })}
              </ButtonRow>
            </CardWell>
          ))
        )}

        {modeError ? <Text style={s.errorText}>{modeError}</Text> : null}
      </View>
    </Card>
  );
};

const createStyles = (t: Theme) => StyleSheet.create({
  subSectionDivider: {
    marginTop: t.space[4],
  },
  subSection: {
    paddingTop: t.space[3],
  },
  subSectionHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: t.space[3],
  },
  subSectionTitle: {
    fontFamily: t.font.sans,
    ...t.type.base,
    fontWeight: t.weight.semibold,
    color: t.color.text.secondary,
  },
  row: {
    marginBottom: t.space[3],
  },
  wordName: {
    fontFamily: t.font.mono,
    ...t.type.sm,
    fontWeight: t.weight.semibold,
    color: t.color.text.primary,
    marginBottom: t.space[2],
  },
  emptyText: {
    fontFamily: t.font.sans,
    ...t.type.sm,
    color: t.color.text.muted,
    fontStyle: 'italic',
  },
  errorText: {
    fontFamily: t.font.sans,
    ...t.type.xs,
    color: t.color.status.danger.fg,
    marginTop: t.space[2],
  },
});

export default SystemAdminControls;
