import { useState, useEffect } from 'react'
import { Settings as SettingsIcon, CheckCircle, AlertCircle, RefreshCw, Volume2, Sliders, Brain, Mic, Users, Cpu, Play, Loader2, X, Check } from 'lucide-react'
import { systemApi, speakerApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { useDiarizationSettings, useLLMOperations, useMemoryProvider, useMiscSettings } from '../hooks/useSystem'

interface DiarizationSettings {
  diarization_source: 'deepgram' | 'pyannote'
  similarity_threshold: number
  min_duration: number
  collar: number
  min_duration_off: number
  min_speakers: number
  max_speakers: number
}

export default function Settings() {
  const { isAdmin, user } = useAuth()

  // TanStack Query hooks
  const { data: diarizationData } = useDiarizationSettings()
  const { data: memoryProviderData } = useMemoryProvider()
  const { data: miscSettingsData } = useMiscSettings()
  const { data: llmOpsData, refetch: refetchLLMOps } = useLLMOperations()

  // Local state for editable settings
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

  const [miscSettings, setMiscSettings] = useState({
    always_persist_enabled: false,
    use_provider_segments: false,
    per_segment_speaker_id: false,
    transcription_job_timeout_seconds: 900
  })
  const [miscLoading, setMiscLoading] = useState(false)
  const [miscMessage, setMiscMessage] = useState('')

  // Sync query data into local editable state
  useEffect(() => {
    if (diarizationData) setDiarizationSettings(diarizationData)
  }, [diarizationData])

  useEffect(() => {
    if (memoryProviderData) {
      setCurrentProvider(memoryProviderData.currentProvider)
      setAvailableProviders(memoryProviderData.availableProviders)
      setSelectedProvider(memoryProviderData.currentProvider)
    }
  }, [memoryProviderData])

  useEffect(() => {
    if (miscSettingsData) setMiscSettings(miscSettingsData)
  }, [miscSettingsData])

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
        setProviderMessage('Provider updated successfully')
      } else {
        setProviderMessage('Failed to update provider')
      }
    } catch (err: any) {
      setProviderMessage('Error: ' + (err.response?.data?.error || err.message))
    } finally {
      setProviderLoading(false)
    }
  }

  const saveDiarizationSettings = async () => {
    try {
      setDiarizationLoading(true)
      const response = await systemApi.saveDiarizationSettings(diarizationSettings)
      if (response.data.status === 'success') {
        alert('Diarization settings saved successfully!')
      } else {
        alert(`Failed to save settings: ${response.data.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      alert(`Error saving settings: ${err.message}`)
    } finally {
      setDiarizationLoading(false)
    }
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <SettingsIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view settings.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center space-x-2 mb-6">
        <SettingsIcon className="h-6 w-6 text-blue-600" />
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Settings
        </h1>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Memory Provider */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Brain className="h-5 w-5 mr-2 text-blue-600" />
            Memory Provider
          </h3>
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
                providerMessage.includes('Error')
                  ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'
                  : providerMessage.includes('already')
                    ? 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-700 dark:text-yellow-300'
                    : 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
              }`}>
                {providerMessage}
              </div>
            )}
          </div>
        </div>

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

        {/* Processing Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Sliders className="h-5 w-5 mr-2 text-blue-600" />
            Processing Settings
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
                    ? 'Identify each segment individually -- better accuracy after fine-tuning'
                    : 'Majority vote per speaker label -- faster, groups segments by label'}
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

            {/* Transcription Job Timeout */}
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  Transcription Job Timeout
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  Max seconds for transcription jobs ({Math.round(miscSettings.transcription_job_timeout_seconds / 60)} min). Increase for slow local STT.
                </div>
              </div>
              <input
                type="number"
                min={60}
                max={7200}
                step={60}
                value={miscSettings.transcription_job_timeout_seconds}
                onChange={(e) => setMiscSettings(prev => ({
                  ...prev,
                  transcription_job_timeout_seconds: Math.max(60, Math.min(7200, parseInt(e.target.value) || 60))
                }))}
                className="ml-4 w-24 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
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
                {miscLoading ? 'Saving...' : 'Save Processing Settings'}
              </button>
            </div>
          </div>
        </div>

        {/* Speaker Configuration */}
        <SpeakerConfiguration user={user} />

        {/* AI Model Settings */}
        {llmOpsData && (
          <LLMOperationsCard
            data={llmOpsData}
            onSaved={refetchLLMOps}
          />
        )}
      </div>
    </div>
  )
}

const OPERATION_LABELS: Record<string, string> = {
  memory_extraction: 'Memory Extraction',
  memory_update: 'Memory Update',
  memory_reprocess: 'Memory Reprocess',
  title_summary: 'Title & Summary',
  detailed_summary: 'Detailed Summary',
  entity_extraction: 'Entity Extraction',
  chat: 'Chat',
  prompt_optimization: 'Prompt Optimization',
  plugin_assistant: 'Plugin Assistant',
}

interface LLMOpsData {
  operations: Record<string, { model: string | null; temperature: number | null; max_tokens: number | null; response_format: string | null }>
  available_models: Array<{ name: string; description: string; provider: string }>
  default_llm: string | null
}

function LLMOperationsCard({ data, onSaved }: { data: LLMOpsData; onSaved: () => void }) {
  const [ops, setOps] = useState<Record<string, any>>({})
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [testResults, setTestResults] = useState<Record<string, { loading: boolean; success?: boolean; latency?: number; error?: string }>>({})

  useEffect(() => {
    if (data?.operations) {
      setOps({ ...data.operations })
    }
  }, [data])

  const updateOp = (opName: string, field: string, value: any) => {
    setOps(prev => ({
      ...prev,
      [opName]: { ...prev[opName], [field]: value },
    }))
  }

  const handleSave = async () => {
    try {
      setSaving(true)
      setMessage('')
      const response = await systemApi.saveLLMOperations(ops)
      if (response.data.status === 'success') {
        setMessage('Settings saved successfully')
        onSaved()
        setTimeout(() => setMessage(''), 3000)
      } else {
        setMessage('Failed to save settings')
      }
    } catch (err: any) {
      setMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (opName: string) => {
    const modelName = ops[opName]?.model || null
    setTestResults(prev => ({ ...prev, [opName]: { loading: true } }))
    try {
      const response = await systemApi.testLLMModel(modelName)
      const d = response.data
      setTestResults(prev => ({
        ...prev,
        [opName]: { loading: false, success: d.success, latency: d.latency_ms, error: d.error },
      }))
    } catch (err: any) {
      setTestResults(prev => ({
        ...prev,
        [opName]: { loading: false, success: false, error: err.message },
      }))
    }
  }

  const operationNames = Object.keys(ops)
  if (operationNames.length === 0) return null

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 lg:col-span-2">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Cpu className="h-5 w-5 mr-2 text-blue-600" />
        AI Model Settings
      </h3>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-600">
              <th className="text-left py-2 pr-3 font-medium text-gray-700 dark:text-gray-300">Operation</th>
              <th className="text-left py-2 px-3 font-medium text-gray-700 dark:text-gray-300">Model</th>
              <th className="text-left py-2 px-3 font-medium text-gray-700 dark:text-gray-300 w-48">Temperature</th>
              <th className="text-left py-2 px-3 font-medium text-gray-700 dark:text-gray-300">Max Tokens</th>
              <th className="text-center py-2 px-3 font-medium text-gray-700 dark:text-gray-300">JSON</th>
              <th className="text-center py-2 pl-3 font-medium text-gray-700 dark:text-gray-300">Test</th>
            </tr>
          </thead>
          <tbody>
            {operationNames.map(opName => {
              const op = ops[opName] || {}
              const test = testResults[opName]
              return (
                <tr key={opName} className="border-b border-gray-100 dark:border-gray-700">
                  <td className="py-2 pr-3 font-medium text-gray-900 dark:text-gray-100 whitespace-nowrap">
                    {OPERATION_LABELS[opName] || opName}
                  </td>
                  <td className="py-2 px-3">
                    <select
                      value={op.model || ''}
                      onChange={e => updateOp(opName, 'model', e.target.value || null)}
                      className="w-full px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    >
                      <option value="">Default{data.default_llm ? ` (${data.default_llm})` : ''}</option>
                      {data.available_models.map(m => (
                        <option key={m.name} value={m.name}>{m.name} — {m.provider}</option>
                      ))}
                    </select>
                  </td>
                  <td className="py-2 px-3">
                    <div className="flex items-center gap-2">
                      <input
                        type="range"
                        min="0"
                        max="2"
                        step="0.05"
                        value={op.temperature ?? 0.2}
                        onChange={e => updateOp(opName, 'temperature', parseFloat(e.target.value))}
                        className="flex-1 h-1.5 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                      />
                      <span className="text-xs text-gray-500 dark:text-gray-400 w-8 text-right">
                        {(op.temperature ?? 0.2).toFixed(2)}
                      </span>
                    </div>
                  </td>
                  <td className="py-2 px-3">
                    <input
                      type="number"
                      min="1"
                      placeholder="—"
                      value={op.max_tokens ?? ''}
                      onChange={e => updateOp(opName, 'max_tokens', e.target.value ? parseInt(e.target.value) : null)}
                      className="w-20 px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    />
                  </td>
                  <td className="py-2 px-3 text-center">
                    <input
                      type="checkbox"
                      checked={op.response_format === 'json'}
                      onChange={e => updateOp(opName, 'response_format', e.target.checked ? 'json' : null)}
                      className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                    />
                  </td>
                  <td className="py-2 pl-3 text-center">
                    <button
                      onClick={() => handleTest(opName)}
                      disabled={test?.loading}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                      title="Test model connection"
                    >
                      {test?.loading ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : test?.success === true ? (
                        <Check className="h-3 w-3 text-green-500" />
                      ) : test?.success === false ? (
                        <X className="h-3 w-3 text-red-500" />
                      ) : (
                        <Play className="h-3 w-3" />
                      )}
                      {test?.latency ? `${test.latency}ms` : 'Test'}
                    </button>
                    {test?.error && (
                      <div className="text-xs text-red-500 mt-1 max-w-[150px] truncate" title={test.error}>
                        {test.error}
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Status Message */}
      {message && (
        <div className={`mt-4 p-2 rounded-md text-sm ${
          message.includes('Error') || message.includes('Failed')
            ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'
            : 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
        }`}>
          {message}
        </div>
      )}

      {/* Save Button */}
      <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
        <button
          onClick={handleSave}
          disabled={saving}
          className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving...' : 'Save AI Model Settings'}
        </button>
      </div>
    </div>
  )
}

// Speaker Configuration Component
function SpeakerConfiguration({ user }: { user: any }) {
  const [speakerServiceStatus, setSpeakerServiceStatus] = useState<any>(null)
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<any[]>([])
  const [primarySpeakers, setPrimarySpeakers] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    loadSpeakerData()
  }, [])

  const loadSpeakerData = async () => {
    setLoading(true)
    try {
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
      setMessage(`Saved! ${primarySpeakers.length} primary speakers configured.`)
      setTimeout(() => setMessage(''), 3000)
    } catch (error: any) {
      console.error('Error saving speaker configuration:', error)
      setMessage(`Failed to save: ${error.response?.data?.error || error.message}`)
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
                  message.includes('Failed') ? 'text-red-600' : 'text-green-600'
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
