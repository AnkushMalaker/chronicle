import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, StyleSheet, Text, TouchableOpacity, View, ActivityIndicator } from 'react-native';
import { useTheme, ThemeColors } from '../theme';

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
  const { colors } = useTheme();
  const s = createStyles(colors);

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
    <View style={s.section}>
      <Text style={s.sectionTitle}>System Controls</Text>

      <TouchableOpacity
        style={[s.restartButton, restarting ? s.disabledButton : null]}
        onPress={restartBackend}
        disabled={restarting}
      >
        {restarting ? <ActivityIndicator size="small" color="#fff" /> : <Text style={s.restartButtonText}>Restart Backend</Text>}
      </TouchableOpacity>

      <View style={s.subSection}>
        <View style={s.subSectionHeader}>
          <Text style={s.subSectionTitle}>Wakeword Modes</Text>
          <TouchableOpacity onPress={fetchModes} disabled={loadingModes}>
            <Text style={s.refreshText}>{loadingModes ? 'Refreshing...' : 'Refresh'}</Text>
          </TouchableOpacity>
        </View>

        {wakewordUnavailable ? (
          <Text style={s.emptyText}>Wakeword controls are not available on this backend.</Text>
        ) : null}

        {loadingModes ? (
          <ActivityIndicator size="small" color={colors.primary} />
        ) : wakewords.length === 0 && !wakewordUnavailable ? (
          <Text style={s.emptyText}>No wakewords found.</Text>
        ) : (
          wakewords.map((w) => (
            <View key={w.name} style={s.row}>
              <Text style={s.wordName}>{w.name}</Text>
              <View style={s.modeButtons}>
                {(['dispatch', 'collect_only', 'off'] as const).map((mode) => {
                  const isActive = w.mode === mode;
                  const isUpdating = updatingWord === w.name;
                  return (
                    <TouchableOpacity
                      key={mode}
                      style={[s.modeButton, isActive ? s.modeButtonActive : null, isUpdating ? s.disabledButton : null]}
                      onPress={() => setWordMode(w.name, mode)}
                      disabled={isUpdating}
                    >
                      <Text style={[s.modeButtonText, isActive ? s.modeButtonTextActive : null]}>
                        {mode === 'collect_only' ? 'collect' : mode}
                      </Text>
                    </TouchableOpacity>
                  );
                })}
              </View>
            </View>
          ))
        )}

        {modeError ? <Text style={s.errorText}>{modeError}</Text> : null}
      </View>
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
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 12,
    color: colors.text,
  },
  restartButton: {
    backgroundColor: colors.danger,
    paddingVertical: 10,
    paddingHorizontal: 14,
    borderRadius: 8,
    alignItems: 'center',
  },
  restartButtonText: {
    color: '#fff',
    fontSize: 14,
    fontWeight: '700',
  },
  subSection: {
    marginTop: 16,
    borderTopWidth: 1,
    borderTopColor: colors.separator,
    paddingTop: 12,
  },
  subSectionHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 10,
  },
  subSectionTitle: {
    fontSize: 15,
    fontWeight: '600',
    color: colors.textSecondary,
  },
  refreshText: {
    fontSize: 13,
    color: colors.primary,
    fontWeight: '600',
  },
  row: {
    marginBottom: 10,
    padding: 10,
    borderWidth: 1,
    borderColor: colors.separator,
    borderRadius: 8,
  },
  wordName: {
    fontSize: 14,
    fontWeight: '600',
    color: colors.text,
    marginBottom: 8,
  },
  modeButtons: {
    flexDirection: 'row',
    gap: 8,
  },
  modeButton: {
    paddingVertical: 6,
    paddingHorizontal: 10,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    backgroundColor: colors.inputBackground,
  },
  modeButtonActive: {
    backgroundColor: colors.primary,
    borderColor: colors.primary,
  },
  modeButtonText: {
    color: colors.textSecondary,
    fontSize: 12,
    fontWeight: '600',
  },
  modeButtonTextActive: {
    color: '#fff',
  },
  emptyText: {
    color: colors.textTertiary,
    fontSize: 13,
    fontStyle: 'italic',
  },
  errorText: {
    marginTop: 8,
    fontSize: 12,
    color: colors.danger,
  },
  disabledButton: {
    opacity: 0.6,
  },
});

export default SystemAdminControls;
