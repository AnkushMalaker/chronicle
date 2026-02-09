import { useState, useEffect } from 'react'
import { Settings, RefreshCw, CheckCircle, XCircle, AlertCircle, Activity, Users, Database, Server, Volume2, Mic, Brain, Sliders } from 'lucide-react'
import { systemApi, speakerApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import MemorySettings from '../components/MemorySettings'
import ChatSettings from '../components/ChatSettings'

interface HealthData {
  status: 'healthy' | 'partial' | 'unhealthy'
  services: Record<string, {
    healthy: boolean
    message?: string
  }>
  timestamp?: string
}

interface MetricsData {
  debug_tracker?: {
    total_files: number
    processed_files: number
    failed_files: number
  }
}

interface ProcessorStatus {
  audio_queue_size: number
  transcription_queue_size: number
  memory_queue_size: number
  active_tasks: number
}

interface ActiveClient {
  id: string
  user_id: string
  connected_at: string
  last_activity: string
}

interface DiarizationSettings {
  diarization_source: 'deepgram' | 'pyannote'
  similarity_threshold: number
  min_duration: number
  collar: number
  min_duration_off: number
  min_speakers: number
  max_speakers: number
}

interface DiagnosticIssue {
  component: string
  severity: 'error' | 'warning' | 'info'
  message: string
  resolution?: string
}

interface ConfigDiagnostics {
  timestamp: string
  overall_status: 'healthy' | 'partial' | 'unhealthy'
  issues: DiagnosticIssue[]
  warnings: DiagnosticIssue[]
  info: DiagnosticIssue[]
  components: Record<string, {
    status: string
    message: string
    details?: any
  }>
}

export default function System() {
  const [healthData, setHealthData] = useState<HealthData | null>(null)
  const [readinessData, setReadinessData] = useState<any>(null)
  const [metricsData, setMetricsData] = useState<MetricsData | null>(null)
  const [configDiagnostics, setConfigDiagnostics] = useState<ConfigDiagnostics | null>(null)
  const [processorStatus, setProcessorStatus] = useState<ProcessorStatus | null>(null)
  const [activeClients, setActiveClients] = useState<ActiveClient[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [diarizationSettings, setDiarizationSettings] = useState<DiarizationSettings>({
    diarization_source: 'pyannote',
    similarity_threshold: 0.15,
    min_duration: 0.5,
    collar: 2.0,
    min_duration_off: 1.5,
    min_speakers: 2,
    max_speakers: 6
  })
  const [diarizationLoading, setDiarizationLoading] = useState(false)
  const [currentProvider, setCurrentProvider] = useState<string>('')
  const [availableProviders, setAvailableProviders] = useState<string[]>([])
  const [selectedProvider, setSelectedProvider] = useState<string>('')
  const [providerLoading, setProviderLoading] = useState(false)
  const [providerMessage, setProviderMessage] = useState('')

  // Miscellaneous settings state
  const [miscSettings, setMiscSettings] = useState({
    always_persist_enabled: false,
    use_provider_segments: false,
    per_segment_speaker_id: false
  })
  const [miscLoading, setMiscLoading] = useState(false)
  const [miscMessage, setMiscMessage] = useState('')

  const { isAdmin } = useAuth()

  const loadSystemData = async () => {
    if (!isAdmin) return

    try {
      setLoading(true)
      setError(null)

      const [health, readiness, metrics, diagnostics, processor, clients] = await Promise.allSettled([
        systemApi.getHealth(),
        systemApi.getReadiness(),
        systemApi.getMetrics().catch(() => ({ data: null })), // Optional endpoint
        systemApi.getConfigDiagnostics().catch(() => ({ data: null })), // Optional endpoint
        systemApi.getProcessorStatus().catch(() => ({ data: null })), // Optional endpoint
        systemApi.getActiveClients().catch(() => ({ data: [] })), // Optional endpoint
      ])

      if (health.status === 'fulfilled') {
        setHealthData(health.value.data)
      }
      if (readiness.status === 'fulfilled') {
        setReadinessData(readiness.value.data)
      }
      if (metrics.status === 'fulfilled' && metrics.value.data) {
        setMetricsData(metrics.value.data)
      }
      if (diagnostics.status === 'fulfilled' && diagnostics.value.data) {
        setConfigDiagnostics(diagnostics.value.data)
      }
      if (processor.status === 'fulfilled' && processor.value.data) {
        setProcessorStatus(processor.value.data)
      }
      if (clients.status === 'fulfilled' && clients.value.data) {
        setActiveClients(clients.value.data)
      }

      setLastUpdated(new Date())
    } catch (err: any) {
      setError(err.message || 'Failed to load system data')
    } finally {
      setLoading(false)
    }
  }

  const loadDiarizationSettings = async () => {
    try {
      setDiarizationLoading(true)
      const response = await systemApi.getDiarizationSettings()
      if (response.data.status === 'success') {
        setDiarizationSettings(response.data.settings)
      }
    } catch (err: any) {
      console.error('Failed to load diarization settings:', err)
    } finally {
      setDiarizationLoading(false)
    }
  }

  const loadMemoryProvider = async () => {
    try {
      setProviderLoading(true)
      const response = await systemApi.getMemoryProvider()
      if (response.data.status === 'success') {
        setCurrentProvider(response.data.current_provider)
        setAvailableProviders(response.data.available_providers)
        setSelectedProvider(response.data.current_provider)
      }
    } catch (err: any) {
      console.error('Failed to load memory provider:', err)
    } finally {
      setProviderLoading(false)
    }
  }

  const loadMiscSettings = async () => {
    try {
      setMiscLoading(true)
      const response = await systemApi.getMiscSettings()
      if (response.data.status === 'success') {
        setMiscSettings(response.data.settings)
      }
    } catch (err: any) {
      console.error('Failed to load misc settings:', err)
    } finally {
      setMiscLoading(false)
    }
  }

  const saveMiscSettings = async () => {
    try {
      setMiscLoading(true)
      setMiscMessage('')
      const response = await systemApi.saveMiscSettings(miscSettings)
      if (response.data.status === 'success') {
        setMiscMessage('Settings saved successfully')
        setTimeout(() => setMiscMessage(''), 3000)
      } else {
        setMiscMessage('Failed to save settings')
      }
    } catch (err: any) {
      setMiscMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setMiscLoading(false)
    }
  }

  const saveMemoryProvider = async () => {
    if (selectedProvider === currentProvider) {
      setProviderMessage('Provider is already set to ' + selectedProvider)
      setTimeout(() => setProviderMessage(''), 3000)
      return
    }

    try {
      setProviderLoading(true)
      setProviderMessage('')
      const response = await systemApi.setMemoryProvider(selectedProvider)
      if (response.data.status === 'success') {
        setCurrentProvider(selectedProvider)
        setProviderMessage('✅ ' + response.data.message)
      } else {
        setProviderMessage('❌ Failed to update provider')
      }
    } catch (err: any) {
      setProviderMessage('❌ Error: ' + (err.response?.data?.error || err.message))
    } finally {
      setProviderLoading(false)
    }
  }

  const saveDiarizationSettings = async () => {
    try {
      setDiarizationLoading(true)
      const response = await systemApi.saveDiarizationSettings(diarizationSettings)
      if (response.data.status === 'success') {
        alert('✅ Diarization settings saved successfully!')
      } else {
        alert(`❌ Failed to save settings: ${response.data.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      alert(`❌ Error saving settings: ${err.message}`)
    } finally {
      setDiarizationLoading(false)
    }
  }

  useEffect(() => {
    loadSystemData()
    loadDiarizationSettings()
    loadMemoryProvider()
    loadMiscSettings()
  }, [isAdmin])

  const getStatusIcon = (healthy: boolean) => {
    return healthy 
      ? <CheckCircle className="h-5 w-5 text-green-500" />
      : <XCircle className="h-5 w-5 text-red-500" />
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'healthy': return 'text-green-600'
      case 'partial': return 'text-yellow-600'
      default: return 'text-red-600'
    }
  }

  const getServiceDisplayName = (service: string) => {
    const displayNames: Record<string, string> = {
      'mongodb': 'MONGODB',
      'redis': 'REDIS & RQ WORKERS',
      'audioai': 'AUDIOAI',
      'mem0': 'MEM0',
      'memory_service': 'MEMORY SERVICE',
      'speech_to_text': 'SPEECH TO TEXT',
      'speaker_recognition': 'SPEAKER RECOGNITION',
      'openmemory_mcp': 'OPENMEMORY MCP'
    }
    return displayNames[service] || service.replace('_', ' ').toUpperCase()
  }

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString()
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <Settings className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view system monitoring.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <Settings className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            System Monitoring
          </h1>
        </div>
        <div className="flex items-center space-x-4">
          {lastUpdated && (
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Last updated: {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <button
            onClick={loadSystemData}
            disabled={loading}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-4 mb-6">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      {/* Overall Health Status */}
      {healthData && (
        <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-6 border border-gray-200 dark:border-gray-600 mb-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <Activity className="h-6 w-6 text-blue-600" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                System Health
              </h2>
            </div>
            <div className="flex items-center space-x-2">
              {healthData.status === 'healthy' && <CheckCircle className="h-6 w-6 text-green-500" />}
              {healthData.status === 'partial' && <AlertCircle className="h-6 w-6 text-yellow-500" />}
              {healthData.status === 'unhealthy' && <XCircle className="h-6 w-6 text-red-500" />}
              <span className={`font-semibold ${getStatusColor(healthData.status)}`}>
                {healthData.status.toUpperCase()}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Configuration Diagnostics */}
      {configDiagnostics && (configDiagnostics.issues.length > 0 || configDiagnostics.warnings.length > 0 || configDiagnostics.info.length > 0) && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
              <AlertCircle className="h-5 w-5 mr-2 text-blue-600" />
              Configuration Diagnostics
            </h3>
            <div className="flex items-center space-x-2">
              {configDiagnostics.overall_status === 'healthy' && <CheckCircle className="h-5 w-5 text-green-500" />}
              {configDiagnostics.overall_status === 'partial' && <AlertCircle className="h-5 w-5 text-yellow-500" />}
              {configDiagnostics.overall_status === 'unhealthy' && <XCircle className="h-5 w-5 text-red-500" />}
              <span className={`text-sm font-semibold ${getStatusColor(configDiagnostics.overall_status)}`}>
                {configDiagnostics.overall_status.toUpperCase()}
              </span>
            </div>
          </div>

          <div className="space-y-3">
            {/* Errors */}
            {configDiagnostics.issues.map((issue, idx) => (
              <div key={`error-${idx}`} className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3">
                <div className="flex items-start space-x-2">
                  <XCircle className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
                  <div className="flex-1">
                    <div className="flex items-center space-x-2 mb-1">
                      <span className="text-xs font-semibold text-red-700 dark:text-red-300 uppercase">
                        {issue.component}
                      </span>
                      <span className="text-xs px-2 py-0.5 bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200 rounded">
                        ERROR
                      </span>
                    </div>
                    <p className="text-sm text-red-700 dark:text-red-300 mb-1">
                      {issue.message}
                    </p>
                    {issue.resolution && (
                      <p className="text-xs text-red-600 dark:text-red-400">
                        💡 {issue.resolution}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            ))}

            {/* Warnings */}
            {configDiagnostics.warnings.map((warning, idx) => (
              <div key={`warning-${idx}`} className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-md p-3">
                <div className="flex items-start space-x-2">
                  <AlertCircle className="h-5 w-5 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
                  <div className="flex-1">
                    <div className="flex items-center space-x-2 mb-1">
                      <span className="text-xs font-semibold text-yellow-700 dark:text-yellow-300 uppercase">
                        {warning.component}
                      </span>
                      <span className="text-xs px-2 py-0.5 bg-yellow-200 dark:bg-yellow-800 text-yellow-800 dark:text-yellow-200 rounded">
                        WARNING
                      </span>
                    </div>
                    <p className="text-sm text-yellow-700 dark:text-yellow-300 mb-1">
                      {warning.message}
                    </p>
                    {warning.resolution && (
                      <p className="text-xs text-yellow-600 dark:text-yellow-400">
                        💡 {warning.resolution}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            ))}

            {/* Info */}
            {configDiagnostics.info.map((info, idx) => (
              <div key={`info-${idx}`} className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md p-3">
                <div className="flex items-start space-x-2">
                  <CheckCircle className="h-5 w-5 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                  <div className="flex-1">
                    <div className="flex items-center space-x-2 mb-1">
                      <span className="text-xs font-semibold text-blue-700 dark:text-blue-300 uppercase">
                        {info.component}
                      </span>
                      <span className="text-xs px-2 py-0.5 bg-blue-200 dark:bg-blue-800 text-blue-800 dark:text-blue-200 rounded">
                        INFO
                      </span>
                    </div>
                    <p className="text-sm text-blue-700 dark:text-blue-300">
                      {info.message}
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Services Status */}
        {healthData?.services && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
              <Database className="h-5 w-5 mr-2 text-blue-600" />
              Services Status
            </h3>
            <div className="space-y-3">
              {Object.entries(healthData.services).map(([service, status]) => (
                <div key={service} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div className="flex items-center space-x-3">
                    {getStatusIcon(status.healthy)}
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {getServiceDisplayName(service)}
                    </span>
                  </div>
                  <div className="text-right">
                    {status.message && (
                      <span className="text-sm text-gray-600 dark:text-gray-400 block">
                        {status.message}
                      </span>
                    )}
                    {(status as any).status && (
                      <span className="text-xs text-gray-500 dark:text-gray-500">
                        {(status as any).status}
                      </span>
                    )}
                    {(status as any).provider && (
                      <span className="text-xs text-blue-600 dark:text-blue-400">
                        ({(status as any).provider})
                      </span>
                    )}
                    {service === 'redis' && (status as any).worker_count !== undefined && (
                      <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                        Workers: {(status as any).worker_count} total
                        ({(status as any).active_workers || 0} active, {(status as any).idle_workers || 0} idle)
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Memory Provider Selector */}
              <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                <div className="flex items-center space-x-3 mb-3">
                  <Brain className="h-5 w-5 text-blue-600" />
                  <span className="font-medium text-gray-900 dark:text-gray-100">
                    Memory Provider
                  </span>
                </div>
                <div className="space-y-3">
                  {/* Current Provider Display */}
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-gray-600 dark:text-gray-400">Current:</span>
                    <span className="font-semibold text-blue-600 dark:text-blue-400">
                      {currentProvider || 'Loading...'}
                    </span>
                  </div>

                  {/* Provider Selector */}
                  <div className="space-y-2">
                    <select
                      value={selectedProvider}
                      onChange={(e) => setSelectedProvider(e.target.value)}
                      disabled={providerLoading || availableProviders.length === 0}
                      className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {availableProviders.map((provider) => (
                        <option key={provider} value={provider}>
                          {provider === 'chronicle' && 'Chronicle mem'}
                          {provider === 'openmemory_mcp' && 'OpenMemory (mem0)'}
                          {provider === 'mycelia' && 'Mycelia mem'}
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={saveMemoryProvider}
                      disabled={providerLoading || selectedProvider === currentProvider}
                      className="w-full px-3 py-2 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {providerLoading ? 'Saving...' : selectedProvider === currentProvider ? 'No Changes' : 'Update Provider'}
                    </button>
                  </div>

                  {/* Status Message */}
                  {providerMessage && (
                    <div className={`p-2 rounded-md text-xs ${
                      providerMessage.startsWith('✅')
                        ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
                        : 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-700 dark:text-yellow-300'
                    }`}>
                      {providerMessage}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Processor Status */}
        {processorStatus && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
              <Server className="h-5 w-5 mr-2 text-blue-600" />
              Processor Status
            </h3>
            <div className="grid grid-cols-2 gap-4 mb-6">
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Audio Queue</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.audio_queue_size}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Transcription Queue</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.transcription_queue_size}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Memory Queue</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.memory_queue_size}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Active Tasks</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {processorStatus.active_tasks}
                </div>
              </div>
            </div>

            {/* Worker Information */}
            {(processorStatus as any).workers && (
              <div className="mt-4 border-t border-gray-200 dark:border-gray-600 pt-4">
                <div className="flex items-center justify-between mb-3">
                  <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">
                    RQ Workers
                  </h4>
                  <span className="text-sm text-gray-600 dark:text-gray-400">
                    {(processorStatus as any).workers.active} / {(processorStatus as any).workers.total} active
                  </span>
                </div>
                <div className="space-y-2">
                  {(processorStatus as any).workers.details?.map((worker: any, idx: number) => (
                    <div key={idx} className="flex items-center justify-between bg-gray-50 dark:bg-gray-700 rounded px-3 py-2 text-sm">
                      <div className="flex items-center space-x-3">
                        <span className={`w-2 h-2 rounded-full ${worker.state === 'idle' ? 'bg-green-500' : 'bg-blue-500'}`}></span>
                        <div className="flex flex-col">
                          <span className="text-gray-900 dark:text-gray-100 font-medium">RQ Worker #{idx + 1}</span>
                          <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">{worker.name.substring(0, 8)}...</span>
                        </div>
                      </div>
                      <div className="flex items-center space-x-3">
                        <span className="text-gray-600 dark:text-gray-400 text-xs">{worker.queues?.join(', ')}</span>
                        <span className={`px-2 py-0.5 rounded text-xs ${
                          worker.state === 'idle'
                            ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                            : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
                        }`}>
                          {worker.state}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Diarization Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Volume2 className="h-5 w-5 mr-2 text-blue-600" />
            Diarization Settings
          </h3>
          
          <div className="space-y-4">
            {/* Diarization Source Selector */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
                Diarization Source
              </label>
              <div className="space-y-2">
                <label className="flex items-center">
                  <input
                    type="radio"
                    name="diarization_source"
                    value="deepgram"
                    checked={diarizationSettings.diarization_source === 'deepgram'}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      diarization_source: e.target.value as 'deepgram' | 'pyannote'
                    }))}
                    className="mr-2"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    <strong>Deepgram</strong> - Use cloud-based diarization (requires API key)
                  </span>
                </label>
                <label className="flex items-center">
                  <input
                    type="radio"
                    name="diarization_source"
                    value="pyannote"
                    checked={diarizationSettings.diarization_source === 'pyannote'}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      diarization_source: e.target.value as 'deepgram' | 'pyannote'
                    }))}
                    className="mr-2"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    <strong>Pyannote</strong> - Use local diarization with configurable parameters
                  </span>
                </label>
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                {diarizationSettings.diarization_source === 'deepgram' 
                  ? 'Deepgram handles diarization automatically. The parameters below apply only to speaker identification.'
                  : 'Pyannote provides local diarization with full parameter control.'
                }
              </div>
            </div>

            {/* Warning for Deepgram with Pyannote params */}
            {diarizationSettings.diarization_source === 'deepgram' && (
              <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-700 rounded-md p-3">
                <div className="flex">
                  <AlertCircle className="h-5 w-5 text-yellow-400 mr-2 flex-shrink-0" />
                  <div>
                    <h4 className="text-sm font-medium text-yellow-800 dark:text-yellow-300">
                      Note: Deepgram Diarization Mode
                    </h4>
                    <p className="text-sm text-yellow-700 dark:text-yellow-400 mt-1">
                      Ignored parameters hidden: speaker count, collar, timing settings. 
                      Only similarity threshold applies to speaker identification.
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* Similarity Threshold (always shown) */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Similarity Threshold: {diarizationSettings.similarity_threshold}
              </label>
              <input
                type="range"
                min="0.05"
                max="0.5"
                step="0.01"
                value={diarizationSettings.similarity_threshold}
                onChange={(e) => setDiarizationSettings(prev => ({
                  ...prev,
                  similarity_threshold: parseFloat(e.target.value)
                }))}
                className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
              />
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Lower values = more sensitive speaker identification
              </div>
            </div>

            {/* Pyannote-specific parameters (conditionally shown) */}
            {diarizationSettings.diarization_source === 'pyannote' && (
              <>
                {/* Min Duration */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Min Duration: {diarizationSettings.min_duration}s
                  </label>
                  <input
                    type="range"
                    min="0.1"
                    max="2.0"
                    step="0.1"
                    value={diarizationSettings.min_duration}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      min_duration: parseFloat(e.target.value)
                    }))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Minimum speech segment duration
                  </div>
                </div>

                {/* Collar */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Collar: {diarizationSettings.collar}s
                  </label>
                  <input
                    type="range"
                    min="0.5"
                    max="5.0"
                    step="0.1"
                    value={diarizationSettings.collar}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      collar: parseFloat(e.target.value)
                    }))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Buffer around speaker segments
                  </div>
                </div>

                {/* Min Duration Off */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Min Duration Off: {diarizationSettings.min_duration_off}s
                  </label>
                  <input
                    type="range"
                    min="0.5"
                    max="3.0"
                    step="0.1"
                    value={diarizationSettings.min_duration_off}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      min_duration_off: parseFloat(e.target.value)
                    }))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Minimum silence between speakers
                  </div>
                </div>

                {/* Speaker Count Range */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                      Min Speakers: {diarizationSettings.min_speakers}
                    </label>
                    <input
                      type="range"
                      min="1"
                      max="6"
                      step="1"
                      value={diarizationSettings.min_speakers}
                      onChange={(e) => setDiarizationSettings(prev => ({
                        ...prev,
                        min_speakers: parseInt(e.target.value)
                      }))}
                      className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                      Max Speakers: {diarizationSettings.max_speakers}
                    </label>
                    <input
                      type="range"
                      min="2"
                      max="10"
                      step="1"
                      value={diarizationSettings.max_speakers}
                      onChange={(e) => setDiarizationSettings(prev => ({
                        ...prev,
                        max_speakers: parseInt(e.target.value)
                      }))}
                      className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                    />
                  </div>
                </div>
              </>
            )}

            {/* Save Button */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
              <button
                onClick={saveDiarizationSettings}
                disabled={diarizationLoading}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {diarizationLoading ? 'Saving...' : 'Save Diarization Settings'}
              </button>
            </div>
          </div>
        </div>

        {/* Miscellaneous Configuration */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Sliders className="h-5 w-5 mr-2 text-blue-600" />
            Miscellaneous Configuration
          </h3>

          <div className="space-y-4">
            {/* Always Persist Audio Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Always Persist Audio
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  Create conversations for all audio sessions, even when no speech is detected
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  checked={miscSettings.always_persist_enabled}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    always_persist_enabled: e.target.checked
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
              </label>
            </div>

            {/* Use Provider Segments Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Use Provider Segments
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  Use speech segments from transcription provider instead of speaker service diarization
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  checked={miscSettings.use_provider_segments}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    use_provider_segments: e.target.checked
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
              </label>
            </div>

            {/* Speaker Identification Mode Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Speaker Identification Mode
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  {miscSettings.per_segment_speaker_id
                    ? 'Identify each segment individually — better accuracy after fine-tuning'
                    : 'Majority vote per speaker label — faster, groups segments by label'}
                </div>
              </div>
              <div className="flex items-center ml-4 gap-2">
                <span className={`text-xs font-medium ${!miscSettings.per_segment_speaker_id ? 'text-blue-600 dark:text-blue-400' : 'text-gray-400 dark:text-gray-500'}`}>
                  Voting
                </span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={miscSettings.per_segment_speaker_id}
                    onChange={(e) => setMiscSettings(prev => ({
                      ...prev,
                      per_segment_speaker_id: e.target.checked
                    }))}
                    className="sr-only peer"
                  />
                  <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
                </label>
                <span className={`text-xs font-medium ${miscSettings.per_segment_speaker_id ? 'text-blue-600 dark:text-blue-400' : 'text-gray-400 dark:text-gray-500'}`}>
                  Per Segment
                </span>
              </div>
            </div>

            {/* Status Message */}
            {miscMessage && (
              <div className={`p-2 rounded-md text-sm ${
                miscMessage.includes('Error') || miscMessage.includes('Failed')
                  ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'
                  : 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
              }`}>
                {miscMessage}
              </div>
            )}

            {/* Save Button */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
              <button
                onClick={saveMiscSettings}
                disabled={miscLoading}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {miscLoading ? 'Saving...' : 'Save Miscellaneous Settings'}
              </button>
            </div>
          </div>
        </div>

        {/* Speaker Configuration */}
        <SpeakerConfiguration />

        {/* Active Clients */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Users className="h-5 w-5 mr-2 text-blue-600" />
            Active Clients ({activeClients.length})
          </h3>
          {activeClients.length > 0 ? (
            <div className="space-y-2 max-h-48 overflow-y-auto">
              {activeClients.map((client) => (
                <div key={client.id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div>
                    <div className="font-medium text-gray-900 dark:text-gray-100">{client.id}</div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      User: {client.user_id}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      Connected: {formatDate(client.connected_at)}
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      Last: {formatDate(client.last_activity)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-gray-500 dark:text-gray-400 text-center py-4">
              No active clients
            </p>
          )}
        </div>

        {/* Debug Metrics */}
        {metricsData?.debug_tracker && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
              Debug Metrics
            </h3>
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Total Files</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {metricsData.debug_tracker.total_files}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Processed</div>
                <div className="text-2xl font-bold text-green-600">
                  {metricsData.debug_tracker.processed_files}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Failed</div>
                <div className="text-2xl font-bold text-red-600">
                  {metricsData.debug_tracker.failed_files}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Chat Configuration - Full Width Section */}
      <div className="mt-6">
        <ChatSettings />
      </div>

      {/* Memory Configuration - Full Width Section */}
      <div className="mt-6">
        <MemorySettings />
      </div>

      {/* Raw Data (Debug) */}
      {readinessData && (
        <div className="mt-6 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <details>
            <summary className="cursor-pointer text-lg font-semibold text-gray-900 dark:text-gray-100 hover:text-blue-600">
              View Raw Readiness Data
            </summary>
            <pre className="mt-4 p-4 bg-gray-100 dark:bg-gray-700 rounded-md text-sm overflow-x-auto">
              {JSON.stringify(readinessData, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  )
}

// Speaker Configuration Component
function SpeakerConfiguration() {
  const [speakerServiceStatus, setSpeakerServiceStatus] = useState<any>(null)
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<any[]>([])
  const [primarySpeakers, setPrimarySpeakers] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const { user } = useAuth()

  useEffect(() => {
    loadSpeakerData()
  }, [])

  const loadSpeakerData = async () => {
    setLoading(true)
    try {
      // Load current configuration and enrolled speakers in parallel
      const [configResponse, speakersResponse, statusResponse] = await Promise.allSettled([
        speakerApi.getSpeakerConfiguration(),
        speakerApi.getEnrolledSpeakers(),
        user?.is_superuser ? speakerApi.getSpeakerServiceStatus() : Promise.resolve({ data: null })
      ])

      if (configResponse.status === 'fulfilled') {
        setPrimarySpeakers(configResponse.value.data.primary_speakers || [])
      }

      if (speakersResponse.status === 'fulfilled') {
        setEnrolledSpeakers(speakersResponse.value.data.speakers || [])
      }

      if (statusResponse.status === 'fulfilled' && statusResponse.value.data) {
        setSpeakerServiceStatus(statusResponse.value.data)
      }

    } catch (error) {
      console.error('Error loading speaker data:', error)
      setMessage('Failed to load speaker configuration')
    } finally {
      setLoading(false)
    }
  }

  const togglePrimarySpeaker = (speaker: any) => {
    const isSelected = primarySpeakers.some(ps => ps.speaker_id === speaker.id)
    
    if (isSelected) {
      setPrimarySpeakers(prev => prev.filter(ps => ps.speaker_id !== speaker.id))
    } else {
      setPrimarySpeakers(prev => [...prev, {
        speaker_id: speaker.id,
        name: speaker.name,
        user_id: speaker.user_id
      }])
    }
  }

  const saveSpeakerConfiguration = async () => {
    setSaving(true)
    setMessage('')
    
    try {
      await speakerApi.updateSpeakerConfiguration(primarySpeakers)
      setMessage(`✅ Saved! ${primarySpeakers.length} primary speakers configured.`)
      
      // Auto-hide success message after 3 seconds
      setTimeout(() => setMessage(''), 3000)
    } catch (error: any) {
      console.error('Error saving speaker configuration:', error)
      setMessage(`❌ Failed to save: ${error.response?.data?.error || error.message}`)
    } finally {
      setSaving(false)
    }
  }

  const resetConfiguration = () => {
    setPrimarySpeakers([])
    setMessage('Configuration reset. Click Save to apply changes.')
  }

  // Don't show the section if speaker service is explicitly disabled or unavailable
  const shouldShowSection = speakerServiceStatus !== null || enrolledSpeakers.length > 0 || loading

  if (!shouldShowSection) {
    return null
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Mic className="h-5 w-5 mr-2 text-blue-600" />
        Speaker Processing Filter
        {speakerServiceStatus && (
          <span className={`ml-2 px-2 py-1 text-xs rounded-full ${
            speakerServiceStatus.healthy 
              ? 'bg-green-100 text-green-800' 
              : 'bg-red-100 text-red-800'
          }`}>
            {speakerServiceStatus.healthy ? 'Service Available' : 'Service Unavailable'}
          </span>
        )}
      </h3>

      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Select primary speakers for memory processing. Only conversations where these speakers are detected will have memories extracted.
        Leave empty to process all conversations.
      </p>

      {/* Service Status Info */}
      {speakerServiceStatus && !speakerServiceStatus.healthy && (
        <div className="mb-4 p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-700 rounded-md">
          <div className="flex">
            <AlertCircle className="h-5 w-5 text-yellow-400 mr-2 flex-shrink-0" />
            <div>
              <h4 className="text-sm font-medium text-yellow-800 dark:text-yellow-300">Speaker Service Unavailable</h4>
              <p className="text-sm text-yellow-700 dark:text-yellow-400 mt-1">
                {speakerServiceStatus.message}. Speaker filtering will be disabled until service is available.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="flex items-center justify-center py-8">
          <RefreshCw className="h-6 w-6 animate-spin text-blue-600 mr-2" />
          <span className="text-gray-600 dark:text-gray-400">Loading speaker data...</span>
        </div>
      )}

      {/* No Speakers Available */}
      {!loading && enrolledSpeakers.length === 0 && (
        <div className="text-center py-8">
          <Users className="h-12 w-12 text-gray-400 mx-auto mb-4" />
          <p className="text-gray-500 dark:text-gray-400">
            No enrolled speakers found. Enroll speakers in the speaker recognition service to configure primary users.
          </p>
        </div>
      )}

      {/* Speaker Selection */}
      {!loading && enrolledSpeakers.length > 0 && (
        <div className="space-y-4">
          {/* Current Configuration */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Primary speakers selected: {primarySpeakers.length}
            </span>
            <button
              onClick={resetConfiguration}
              className="text-sm text-red-600 hover:text-red-800 font-medium"
            >
              Reset
            </button>
          </div>

          {/* Speaker List */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 max-h-60 overflow-y-auto">
            {enrolledSpeakers.map((speaker) => {
              const isSelected = primarySpeakers.some(ps => ps.speaker_id === speaker.id)
              return (
                <div
                  key={speaker.id}
                  className={`p-3 border rounded-lg cursor-pointer transition-colors ${
                    isSelected
                      ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 text-blue-900 dark:text-blue-300'
                      : 'border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:border-gray-300 dark:hover:border-gray-500'
                  }`}
                  onClick={() => togglePrimarySpeaker(speaker)}
                >
                  <div className="flex items-center">
                    <div className={`w-4 h-4 mr-3 rounded border-2 flex items-center justify-center ${
                      isSelected ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-500'
                    }`}>
                      {isSelected && <CheckCircle className="h-3 w-3 text-white" />}
                    </div>
                    <div>
                      <div className="font-medium">{speaker.name}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        {speaker.audio_sample_count || 0} samples
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Save Button */}
          <div className="flex items-center justify-between pt-4 border-t border-gray-200 dark:border-gray-600">
            <div className="flex-1">
              {message && (
                <p className={`text-sm ${
                  message.startsWith('✅') ? 'text-green-600' : 'text-red-600'
                }`}>
                  {message}
                </p>
              )}
            </div>
            <button
              onClick={saveSpeakerConfiguration}
              disabled={saving}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? 'Saving...' : 'Save Configuration'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}