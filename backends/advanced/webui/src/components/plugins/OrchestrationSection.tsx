import { Zap } from 'lucide-react'

interface OrchestrationConfig {
  enabled: boolean
  events: string[]
  condition: {
    type: 'always' | 'wake_word'
    wake_words?: string[]
  }
}

interface OrchestrationSectionProps {
  config: OrchestrationConfig
  onChange: (config: OrchestrationConfig) => void
  disabled?: boolean
}

const AVAILABLE_EVENTS = [
  { value: 'conversation.complete', label: 'Conversation Complete' },
  { value: 'transcript.streaming', label: 'Transcript Streaming' },
  { value: 'memory.created', label: 'Memory Created' },
]

export default function OrchestrationSection({
  config,
  onChange,
  disabled = false
}: OrchestrationSectionProps) {
  const handleEnabledChange = (enabled: boolean) => {
    onChange({ ...config, enabled })
  }

  const handleEventToggle = (event: string) => {
    const events = config.events.includes(event)
      ? config.events.filter((e) => e !== event)
      : [...config.events, event]
    onChange({ ...config, events })
  }

  const handleConditionTypeChange = (type: 'always' | 'wake_word') => {
    onChange({
      ...config,
      condition: {
        type,
        wake_words: type === 'wake_word' ? config.condition.wake_words || [] : undefined
      }
    })
  }

  const handleWakeWordsChange = (value: string) => {
    const wake_words = value.split(',').map((w) => w.trim()).filter(Boolean)
    onChange({
      ...config,
      condition: {
        ...config.condition,
        wake_words
      }
    })
  }

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
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
          Events
          <span className="text-red-500 ml-1">*</span>
        </label>
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
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Condition Type */}
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
          Condition
          <span className="text-red-500 ml-1">*</span>
        </label>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          When should this plugin execute?
        </p>
        <div className="space-y-2">
          <label
            className={`
              flex items-center space-x-3 p-3 border rounded-lg cursor-pointer transition-colors
              ${
                config.condition.type === 'always'
                  ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                  : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
              }
              ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
            `}
          >
            <input
              type="radio"
              name="condition"
              value="always"
              checked={config.condition.type === 'always'}
              onChange={() => !disabled && handleConditionTypeChange('always')}
              disabled={disabled}
              className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300"
            />
            <div>
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                Always
              </span>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Execute on every matching event
              </p>
            </div>
          </label>

          <label
            className={`
              flex items-center space-x-3 p-3 border rounded-lg cursor-pointer transition-colors
              ${
                config.condition.type === 'wake_word'
                  ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                  : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
              }
              ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
            `}
          >
            <input
              type="radio"
              name="condition"
              value="wake_word"
              checked={config.condition.type === 'wake_word'}
              onChange={() => !disabled && handleConditionTypeChange('wake_word')}
              disabled={disabled}
              className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300"
            />
            <div className="flex-1">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                Wake Word
              </span>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Execute only when specific wake words are detected
              </p>
            </div>
          </label>
        </div>
      </div>

      {/* Wake Words Input (conditional) */}
      {config.condition.type === 'wake_word' && (
        <div className="pl-7">
          <label
            htmlFor="wake-words"
            className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
          >
            Wake Words
            <span className="text-red-500 ml-1">*</span>
          </label>
          <input
            type="text"
            id="wake-words"
            value={config.condition.wake_words?.join(', ') || ''}
            onChange={(e) => !disabled && handleWakeWordsChange(e.target.value)}
            placeholder="e.g., hey jarvis, ok assistant"
            disabled={disabled}
            className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
          />
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Comma-separated list of wake words (case-insensitive)
          </p>
        </div>
      )}
    </div>
  )
}
