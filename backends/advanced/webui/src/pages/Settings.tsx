import { useState, useEffect } from 'react'
import { Settings as SettingsIcon, CheckCircle, AlertCircle, RefreshCw, Volume2, Sliders, Mic, Users, Cpu, Play, Loader2, X, Check, UserCircle } from 'lucide-react'
import { systemApi, speakerApi, authApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { useDiarizationSettings, useLLMOperations, useMiscSettings } from '../hooks/useSystem'

interface DiarizationSettings {
  diarization_source: 'provider' | 'pyannote'
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
  const { data: miscSettingsData } = useMiscSettings()
  const { data: llmOpsData, refetch: refetchLLMOps } = useLLMOperations()

  // Local state for editable settings
  const [diarizationSettings, setDiarizationSettings] = useState<DiarizationSettings>({
    diarization_source: 'provider',
    similarity_threshold: 0.15,
    min_duration: 0.5,
    collar: 2.0,
    min_duration_off: 1.5,
    min_speakers: 2,
    max_speakers: 6
  })
  const [diarizationLoading, setDiarizationLoading] = useState(false)

  const [miscSettings, setMiscSettings] = useState({
    always_persist_enabled: false,
    per_segment_speaker_id: false,
    streaming_fallback_timeout_seconds: 120,
    always_batch_retranscribe: false,
    live_segmentation: 'streaming_stt' as 'streaming_stt' | 'windowed_batch' | 'off',
  })
  const [miscLoading, setMiscLoading] = useState(false)
  const [miscMessage, setMiscMessage] = useState('')

  // Identity settings (how the user/assistant are labeled when extracting chat memories)
  const [displayName, setDisplayName] = useState('')
  const [assistantName, setAssistantName] = useState('')
  const [identityLoading, setIdentityLoading] = useState(false)
  const [identityMessage, setIdentityMessage] = useState('')

  // Sync query data into local editable state
  useEffect(() => {
    if (diarizationData) setDiarizationSettings(diarizationData)
  }, [diarizationData])


  useEffect(() => {
    if (miscSettingsData) setMiscSettings(miscSettingsData)
  }, [miscSettingsData])

  // Load current identity from the user's profile
  useEffect(() => {
    authApi.getMe()
      .then((res) => {
        setDisplayName(res.data.display_name || '')
        setAssistantName(res.data.assistant_name || '')
      })
      .catch(() => {
        // Non-fatal: leave fields blank if profile can't be loaded
      })
  }, [])

  const saveIdentity = async () => {
    try {
      setIdentityLoading(true)
      setIdentityMessage('')
      await authApi.updateMe({
        display_name: displayName.trim(),
        assistant_name: assistantName.trim(),
      })
      setIdentityMessage('Identity saved successfully')
      setTimeout(() => setIdentityMessage(''), 3000)
    } catch (err: any) {
      setIdentityMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setIdentityLoading(false)
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
        {/* Identity */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <UserCircle className="h-5 w-5 mr-2 text-blue-600" />
            Identity
          </h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
            Names used to label who is speaking when extracting memories from chat.
            Leave blank to fall back to the generic "User" and "Assistant".
          </p>
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Your name
              </label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="e.g. Ankush"
                className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Assistant name
              </label>
              <input
                type="text"
                value={assistantName}
                onChange={(e) => setAssistantName(e.target.value)}
                placeholder="e.g. Chronicle"
                className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
            <button
              onClick={saveIdentity}
              disabled={identityLoading}
              className="w-full px-3 py-2 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {identityLoading ? 'Saving...' : 'Save Identity'}
            </button>
            {identityMessage && (
              <div className={`p-2 rounded-md text-xs ${
                identityMessage.includes('Error')
                  ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'
                  : 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
              }`}>
                {identityMessage}
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
                    value="provider"
                    checked={diarizationSettings.diarization_source === 'provider'}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      diarization_source: e.target.value as 'provider' | 'pyannote'
                    }))}
                    className="mr-2"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    <strong>Provider</strong> - Trust speaker segments from the transcription provider when available
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
                      diarization_source: e.target.value as 'provider' | 'pyannote'
                    }))}
                    className="mr-2"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    <strong>Pyannote</strong> - Always re-diarize locally with configurable parameters
                  </span>
                </label>
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                {diarizationSettings.diarization_source === 'provider'
                  ? 'Diarized segments from the transcription provider are used as-is; Pyannote runs as a fallback when the provider does not diarize. The parameters below apply to the fallback and speaker identification.'
                  : 'Pyannote re-diarizes every transcript locally with full parameter control.'
                }
              </div>
            </div>

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

            {/* Pyannote parameters (apply when pyannote diarizes, including provider fallback) */}
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
                {miscSettings.live_segmentation === 'off' && !miscSettings.always_persist_enabled && (
                  <div className="mt-2 flex items-start gap-1.5 text-sm font-medium text-red-600 dark:text-red-400">
                    <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                    <span>
                      Overridden to <strong>on</strong> for this backend: with Live Segmentation set to <strong>Off</strong> there is no live transcript to detect speech on, so audio must always be persisted — otherwise batch transcription at conversation end would have nothing to read. Your saved setting is unchanged and takes effect again if you enable a live transcript mode.
                    </span>
                  </div>
                )}
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

            {/* Always Batch Re-Transcribe Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Always Batch Re-Transcribe
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  After each streaming conversation, re-transcribe with the batch provider for higher quality. Streaming transcript is shown immediately as a preview; memories and summaries are only generated from the batch result.
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  checked={miscSettings.always_batch_retranscribe}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    always_batch_retranscribe: e.target.checked
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
              </label>
            </div>

            {/* Pseudo-streaming via batch Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Pseudo-streaming via batch
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  {miscSettings.live_segmentation === 'streaming_stt'
                    ? 'A real streaming STT provider is configured (stt_stream), so live transcripts come from it directly. This batch-window preview does not apply.'
                    : 'Show a live transcript preview during recording by batch-transcribing fixed audio windows -- for batch ASR (e.g. VibeVoice) that has no true streaming. When off, no live transcript is shown; the full transcript is produced by batch transcription when the conversation ends. Changing this restarts the workers.'}
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  disabled={miscSettings.live_segmentation === 'streaming_stt'}
                  checked={miscSettings.live_segmentation === 'windowed_batch'}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    live_segmentation: e.target.checked ? 'windowed_batch' : 'off'
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600 peer-disabled:opacity-40 peer-disabled:cursor-not-allowed"></div>
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
                  Streaming Fallback Timeout
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  Max seconds for streaming fallback check ({Math.round(miscSettings.streaming_fallback_timeout_seconds / 60)} min). How long to wait before giving up on batch transcription fallback.
                </div>
              </div>
              <input
                type="number"
                min={60}
                max={7200}
                step={60}
                value={miscSettings.streaming_fallback_timeout_seconds}
                onChange={(e) => setMiscSettings(prev => ({
                  ...prev,
                  streaming_fallback_timeout_seconds: Math.max(60, Math.min(7200, parseInt(e.target.value) || 60))
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
