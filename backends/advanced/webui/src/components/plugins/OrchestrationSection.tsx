import { useEffect, useState } from 'react'
import { Zap } from 'lucide-react'
import { wakewordApi } from '../../services/api'
import { Checkbox, Input, Label } from '../ui'

type ConditionType = 'always' | 'wake_word' | 'keyword_anywhere' | 'acoustic_wake_word'

interface OrchestrationConfig {
  enabled: boolean
  events: string[]
  condition: {
    type: ConditionType
    wake_words?: string[]
    keywords?: string[]
    threshold?: number
  }
}

interface OrchestrationSectionProps {
  config: OrchestrationConfig
  onChange: (config: OrchestrationConfig) => void
  disabled?: boolean
}

// Keep in sync with backend PluginEvent enum (plugins/events.py)
const AVAILABLE_EVENTS: { value: string; label: string; note?: string }[] = [
  { value: 'conversation.complete', label: 'Conversation Complete' },
  { value: 'transcript.streaming', label: 'Transcript Streaming' },
  { value: 'memory.processed', label: 'Memory Processed' },
  { value: 'transcript.batch', label: 'Transcript Batch', note: 'file upload' },
  { value: 'wake_word.detected', label: 'Acoustic Wake Word', note: 'wakeword-service' },
  { value: 'button.single_press', label: 'Button Single Press', note: 'from OMI' },
  { value: 'button.double_press', label: 'Button Double Press', note: 'from OMI' },
]

const DEFAULT_ACOUSTIC_THRESHOLD = 0.9

export default function OrchestrationSection({
  config,
  onChange,
  disabled = false
}: OrchestrationSectionProps) {
  // Wake-word models the service actually has on disk (for the acoustic picker).
  const [models, setModels] = useState<string[]>([])
  const [modelsError, setModelsError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    wakewordApi
      .getModels()
      .then((res) => {
        if (!cancelled) {
          setModels(res.data.available || [])
          setModelsError(null)
        }
      })
      .catch(() => {
        if (!cancelled) setModelsError('Wake-word service unreachable — start it to pick a wake word.')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleEnabledChange = (enabled: boolean) => {
    onChange({ ...config, enabled })
  }

  const handleEventToggle = (event: string) => {
    const events = config.events.includes(event)
      ? config.events.filter((e) => e !== event)
      : [...config.events, event]
    onChange({ ...config, events })
  }

  const handleConditionTypeChange = (type: ConditionType) => {
    onChange({
      ...config,
      condition: {
        type,
        wake_words:
          type === 'wake_word' || type === 'acoustic_wake_word'
            ? config.condition.wake_words || []
            : undefined,
        keywords: type === 'keyword_anywhere' ? config.condition.keywords || [] : undefined,
        threshold:
          type === 'acoustic_wake_word'
            ? config.condition.threshold ?? DEFAULT_ACOUSTIC_THRESHOLD
            : undefined,
      }
    })
  }

  const handleWakeWordsChange = (value: string) => {
    const wake_words = value.split(',').map((w) => w.trim()).filter(Boolean)
    onChange({ ...config, condition: { ...config.condition, wake_words } })
  }

  const handleKeywordsChange = (value: string) => {
    const keywords = value.split(',').map((w) => w.trim()).filter(Boolean)
    onChange({ ...config, condition: { ...config.condition, keywords } })
  }

  const handleThresholdChange = (value: string) => {
    const threshold = parseFloat(value)
    onChange({
      ...config,
      condition: { ...config.condition, threshold: isNaN(threshold) ? undefined : threshold }
    })
  }

  const handleAcousticWakeToggle = (model: string) => {
    const current = config.condition.wake_words || []
    const wake_words = current.includes(model)
      ? current.filter((w) => w !== model)
      : [...current, model]
    onChange({ ...config, condition: { ...config.condition, wake_words } })
  }

  const conditionOptions: { value: ConditionType; label: string; desc: string }[] = [
    { value: 'always', label: 'Always', desc: 'Execute on every matching event, no filtering' },
    {
      value: 'wake_word',
      label: 'Wake Word (start of sentence)',
      desc: 'Triggers when the transcript starts with the wake word'
    },
    {
      value: 'keyword_anywhere',
      label: 'Keyword Anywhere',
      desc: 'Triggers when keyword appears anywhere in the transcript'
    },
    {
      value: 'acoustic_wake_word',
      label: 'Acoustic Wake Word',
      desc: 'Triggers only on the acoustic wake word from the wakeword-service (not text)'
    },
  ]

  return (
    <div className="space-y-4">
      {/* Section Header */}
      <div className="flex items-center space-x-2 pb-2 border-b border-gray-200 dark:border-gray-700">
        <Zap className="h-5 w-5 text-blue-600" />
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Orchestration
        </h3>
      </div>

      {/* Enable Plugin Toggle */}
      <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
        <div>
          <label
            htmlFor="plugin-enabled"
            className="text-sm font-medium text-gray-700 dark:text-gray-300"
          >
            Enable Plugin
          </label>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Activate this plugin for event processing
          </p>
        </div>
        <label className="flex items-center space-x-2 cursor-pointer">
          <div
            className={`
              relative inline-flex h-6 w-11 items-center rounded-full transition-colors
              ${config.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'}
              ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
            `}
            onClick={() => !disabled && handleEnabledChange(!config.enabled)}
          >
            <span
              className={`
                inline-block h-5 w-5 transform rounded-full bg-white transition-transform
                ${config.enabled ? 'translate-x-6' : 'translate-x-0.5'}
              `}
            />
          </div>
        </label>
      </div>

      {/* Events Selection */}
      <div>
        <Label className="mb-2">
          Events
          <span className="text-red-500 ml-1">*</span>
        </Label>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          Select which events should trigger this plugin
        </p>
        <div className="space-y-2">
          {AVAILABLE_EVENTS.map((event) => (
            <label
              key={event.value}
              className={`
                flex items-center space-x-3 p-3 border rounded-lg cursor-pointer transition-colors
                ${
                  config.events.includes(event.value)
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                    : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
                }
                ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
              `}
            >
              <input
                type="checkbox"
                checked={config.events.includes(event.value)}
                onChange={() => !disabled && handleEventToggle(event.value)}
                disabled={disabled}
                className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded"
              />
              <span className="text-sm text-gray-900 dark:text-gray-100">
                {event.label}
                {event.note && (
                  <span className="ml-1.5 text-xs text-gray-400 dark:text-gray-500 italic">
                    ({event.note})
                  </span>
                )}
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Condition Type */}
      <div>
        <Label className="mb-2">
          Condition
          <span className="text-red-500 ml-1">*</span>
        </Label>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          When should this plugin execute?
        </p>
        <div className="space-y-2">
          {conditionOptions.map((opt) => (
            <label
              key={opt.value}
              className={`
                flex items-center space-x-3 p-3 border rounded-lg cursor-pointer transition-colors
                ${
                  config.condition.type === opt.value
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                    : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
                }
                ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
              `}
            >
              <input
                type="radio"
                name="condition"
                value={opt.value}
                checked={config.condition.type === opt.value}
                onChange={() => !disabled && handleConditionTypeChange(opt.value)}
                disabled={disabled}
                className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300"
              />
              <div className="flex-1">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {opt.label}
                </span>
                <p className="text-xs text-gray-500 dark:text-gray-400">{opt.desc}</p>
              </div>
            </label>
          ))}
        </div>
      </div>

      {/* Wake Words Input (conditional) */}
      {config.condition.type === 'wake_word' && (
        <div className="pl-7">
          <Label htmlFor="wake-words" className="mb-1">
            Wake Words
            <span className="text-red-500 ml-1">*</span>
          </Label>
          <Input
            type="text"
            id="wake-words"
            value={config.condition.wake_words?.join(', ') || ''}
            onChange={(e) => !disabled && handleWakeWordsChange(e.target.value)}
            placeholder="e.g., hey jarvis, ok assistant"
            disabled={disabled}
          />
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Comma-separated list of wake words. The transcript must start with one of these words (case-insensitive).
          </p>
        </div>
      )}

      {/* Keywords Input (conditional) */}
      {config.condition.type === 'keyword_anywhere' && (
        <div className="pl-7">
          <Label htmlFor="keywords" className="mb-1">
            Keywords
            <span className="text-red-500 ml-1">*</span>
          </Label>
          <Input
            type="text"
            id="keywords"
            value={config.condition.keywords?.join(', ') || ''}
            onChange={(e) => !disabled && handleKeywordsChange(e.target.value)}
            placeholder="e.g., hermes, hey chronicle"
            disabled={disabled}
          />
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Comma-separated list of keywords. Triggers when any keyword appears anywhere in the transcript (case-insensitive).
          </p>
        </div>
      )}

      {/* Acoustic Wake Word config (conditional) */}
      {config.condition.type === 'acoustic_wake_word' && (
        <div className="pl-7 space-y-4">
          {/* Wake-word model picker — limited to models the service has */}
          <div>
            <Label className="mb-1">
              Wake Word
            </Label>
            {modelsError ? (
              <p className="text-xs text-amber-600 dark:text-amber-400">{modelsError}</p>
            ) : models.length === 0 ? (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                No wake-word models found in the service.
              </p>
            ) : (
              <div className="space-y-1.5">
                {models.map((model) => (
                  <Checkbox
                    key={model}
                    checked={config.condition.wake_words?.includes(model) || false}
                    onChange={() => !disabled && handleAcousticWakeToggle(model)}
                    disabled={disabled}
                    label={<span className="text-gray-900 dark:text-gray-100">{model}</span>}
                  />
                ))}
              </div>
            )}
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Only wake words the wakeword-service has trained models for can be selected.
            </p>
          </div>

          {/* Detection threshold */}
          <div>
            <Label htmlFor="acoustic-threshold" className="mb-1">
              Detection threshold
            </Label>
            <input
              type="number"
              id="acoustic-threshold"
              min={0}
              max={1}
              step={0.01}
              value={config.condition.threshold ?? DEFAULT_ACOUSTIC_THRESHOLD}
              onChange={(e) => !disabled && handleThresholdChange(e.target.value)}
              disabled={disabled}
              className="w-32 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Minimum acoustic confidence (0–1) required to fire. Higher = fewer false triggers.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
