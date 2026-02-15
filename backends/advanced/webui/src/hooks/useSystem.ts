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
