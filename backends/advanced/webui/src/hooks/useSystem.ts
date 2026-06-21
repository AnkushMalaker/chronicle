import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { systemApi } from '../services/api'

export function useSystemData(isAdmin: boolean) {
  return useQuery({
    queryKey: ['system', 'data'],
    queryFn: async () => {
      const [health, readiness, metrics, diagnostics, processor, clients] = await Promise.allSettled([
        systemApi.getHealth(),
        systemApi.getReadiness(),
        systemApi.getMetrics().catch(() => ({ data: null })),
        systemApi.getConfigDiagnostics().catch(() => ({ data: null })),
        systemApi.getProcessorStatus().catch(() => ({ data: null })),
        systemApi.getActiveClients().catch(() => ({ data: [] })),
      ])

      return {
        healthData: health.status === 'fulfilled' ? health.value.data : null,
        readinessData: readiness.status === 'fulfilled' ? readiness.value.data : null,
        metricsData: metrics.status === 'fulfilled' ? metrics.value.data : null,
        configDiagnostics: diagnostics.status === 'fulfilled' ? diagnostics.value.data : null,
        processorStatus: processor.status === 'fulfilled' ? processor.value.data : null,
        activeClients: clients.status === 'fulfilled' ? clients.value.data || [] : [],
      }
    },
    enabled: isAdmin,
    staleTime: 60_000,
  })
}

export function useDiarizationSettings() {
  return useQuery({
    queryKey: ['system', 'diarizationSettings'],
    queryFn: async () => {
      const response = await systemApi.getDiarizationSettings()
      if (response.data.status === 'success') {
        return response.data.settings
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export function useMemoryProvider() {
  return useQuery({
    queryKey: ['system', 'memoryProvider'],
    queryFn: async () => {
      const response = await systemApi.getMemoryProvider()
      if (response.data.status === 'success') {
        return {
          currentProvider: response.data.current_provider,
          availableProviders: response.data.available_providers,
        }
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export function useMiscSettings() {
  return useQuery({
    queryKey: ['system', 'miscSettings'],
    queryFn: async () => {
      const response = await systemApi.getMiscSettings()
      if (response.data.status === 'success') {
        return response.data.settings
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export function useLLMOperations() {
  return useQuery({
    queryKey: ['system', 'llmOperations'],
    queryFn: async () => {
      const response = await systemApi.getLLMOperations()
      if (response.data.status === 'success') {
        return response.data
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export interface ExternalServiceProvider {
  env_key: string
  current: string
  streaming_current?: string
  available: { key: string; label: string }[]
  // Streaming-lane (stt_stream) provider options; present only for asr-services.
  streaming_available?: { key: string; label: string }[]
}

export interface ExternalService {
  name: string
  description: string
  ports: string[]
  enabled: boolean
  health: 'healthy' | 'partial' | 'unhealthy' | 'stopped' | 'starting'
  health_detail: string
  provider: ExternalServiceProvider | null
  // The node (host) this service runs on, and whether it's a remote cluster node.
  node?: string | null
  remote?: boolean
}

export interface ServiceOperation {
  id: string
  service: string
  action: string
  status: 'running' | 'done' | 'failed'
  ok: boolean | null
  log: string
  phase?: string
  // Node the operation runs on — used to poll the owning node's agent.
  node?: string | null
}

export interface ExternalServicesData {
  available: boolean
  reason?: string
  detail?: string
  services?: ExternalService[]
  operation?: ServiceOperation | null
}

export function useExternalServices(isAdmin: boolean, pollWhileBusy: boolean) {
  return useQuery<ExternalServicesData>({
    queryKey: ['system', 'externalServices'],
    queryFn: async () => {
      const response = await systemApi.getExternalServices()
      return response.data
    },
    enabled: isAdmin,
    staleTime: 30_000,
    // Poll while an operation runs or a service is still booting (model loading),
    // so health flips to healthy without a manual refresh.
    refetchInterval: query => {
      if (pollWhileBusy) return 3_000
      const services = query.state.data?.services
      return services?.some(s => s.health === 'starting') ? 5_000 : false
    },
  })
}

export function useRestartWorkers() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => systemApi.restartWorkers(),
    onSuccess: () => {
      // Workers take a few seconds to restart; refresh system data after delay
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ['system'] }), 5000)
    },
  })
}

export function useRestartBackend() {
  return useMutation({
    mutationFn: () => systemApi.restartBackend(),
    // No auto-invalidation — the backend is going down
  })
}
