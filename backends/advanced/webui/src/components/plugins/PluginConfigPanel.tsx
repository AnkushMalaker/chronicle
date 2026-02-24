import { useState } from 'react'
import { Settings, CheckCircle, XCircle, Loader2 } from 'lucide-react'
import OrchestrationSection from './OrchestrationSection'
import EnvVarsSection from './EnvVarsSection'
import FormField, { FieldSchema } from './FormField'

interface PluginMetadata {
  plugin_id: string
  name: string
  description: string
  enabled: boolean
  status: string
  supports_testing: boolean
  config_schema: {
    settings: Record<string, FieldSchema>
    env_vars: Record<string, FieldSchema>
  }
}

interface PluginConfig {
  orchestration: {
    enabled: boolean
    events: string[]
    condition: {
      type: 'always' | 'wake_word' | 'keyword_anywhere'
      wake_words?: string[]
      keywords?: string[]
    }
  }
  settings: Record<string, any>
  env_vars: Record<string, any>
}

interface PluginConfigPanelProps {
  plugin: PluginMetadata
  config: PluginConfig
  onChange: (config: PluginConfig) => void
  onTestConnection?: () => Promise<void>
  onSave: () => Promise<void>
  onReset: () => void
  errors?: Record<string, string>
  testResult?: { success: boolean; message: string; details?: any } | null
  testing?: boolean
  saving?: boolean
  disabled?: boolean
}

export default function PluginConfigPanel({
  plugin,
  config,
  onChange,
  onTestConnection,
  onSave,
  onReset,
  errors = {},
  testResult = null,
  testing = false,
  saving = false,
  disabled = false
}: PluginConfigPanelProps) {
  const [activeTab, setActiveTab] = useState<'orchestration' | 'settings' | 'secrets'>('orchestration')

  const handleOrchestrationChange = (orchestration: any) => {
    onChange({ ...config, orchestration })
  }

  const handleSettingsChange = (key: string, value: any) => {
    onChange({
      ...config,
      settings: { ...config.settings, [key]: value }
    })
  }

  const handleEnvVarsChange = (envVars: Record<string, any>) => {
    onChange({ ...config, env_vars: envVars })
  }

  const settingsKeys = Object.keys(plugin.config_schema.settings || {})
  const hasSettings = settingsKeys.length > 0
  const hasEnvVars = Object.keys(plugin.config_schema.env_vars || {}).length > 0

  return (
    <div className="h-full flex flex-col">
      {/* Plugin Header */}
      <div className="p-6 border-b border-gray-200 dark:border-gray-700">
        <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-2">
          {plugin.name}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {plugin.description}
        </p>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-gray-200 dark:border-gray-700 px-6">
        <button
          onClick={() => setActiveTab('orchestration')}
          className={`
            px-4 py-3 text-sm font-medium border-b-2 transition-colors
            ${
              activeTab === 'orchestration'
                ? 'border-blue-600 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }
          `}
        >
          Orchestration
        </button>
        {hasSettings && (
          <button
            onClick={() => setActiveTab('settings')}
            className={`
              px-4 py-3 text-sm font-medium border-b-2 transition-colors
              ${
                activeTab === 'settings'
                  ? 'border-blue-600 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
              }
            `}
          >
            Settings
          </button>
        )}
        {hasEnvVars && (
          <button
            onClick={() => setActiveTab('secrets')}
            className={`
              px-4 py-3 text-sm font-medium border-b-2 transition-colors
              ${
                activeTab === 'secrets'
                  ? 'border-blue-600 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
              }
            `}
          >
            Secrets
          </button>
        )}
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-y-auto p-6">
        {activeTab === 'orchestration' && (
          <OrchestrationSection
            config={config.orchestration}
            onChange={handleOrchestrationChange}
            disabled={disabled}
          />
        )}

        {activeTab === 'settings' && hasSettings && (
          <div className="space-y-4">
            <div className="flex items-center space-x-2 pb-2 border-b border-gray-200 dark:border-gray-700">
              <Settings className="h-5 w-5 text-blue-600" />
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                Plugin Settings
              </h3>
            </div>

            <div className="space-y-4">
              {settingsKeys.map((key) => {
                const fieldSchema = plugin.config_schema.settings[key]
                const value = config.settings[key]
                const error = errors[`settings.${key}`]

                return (
                  <FormField
                    key={key}
                    fieldKey={key}
                    schema={fieldSchema}
                    value={value}
                    onChange={(newValue) => handleSettingsChange(key, newValue)}
                    error={error}
                    disabled={disabled}
                  />
                )
              })}
            </div>
          </div>
        )}

        {activeTab === 'secrets' && hasEnvVars && (
          <EnvVarsSection
            schema={plugin.config_schema.env_vars}
            values={config.env_vars}
            onChange={handleEnvVarsChange}
            errors={errors}
            disabled={disabled}
          />
        )}
      </div>

      {/* Test Result Display */}
      {testResult && (
        <div className="px-6 pb-4">
          <div
            className={`
              p-4 rounded-lg border flex items-start space-x-3
              ${
                testResult.success
                  ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
                  : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
              }
            `}
          >
            {testResult.success ? (
              <CheckCircle className="h-5 w-5 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
            ) : (
              <XCircle className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
            )}
            <div className="flex-1">
              <p
                className={`
                  text-sm font-medium
                  ${
                    testResult.success
                      ? 'text-green-800 dark:text-green-200'
                      : 'text-red-800 dark:text-red-200'
                  }
                `}
              >
                {testResult.message}
              </p>
              {testResult.details && (
                <pre className="mt-2 text-xs text-gray-600 dark:text-gray-400 overflow-x-auto">
                  {JSON.stringify(testResult.details, null, 2)}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Action Buttons */}
      <div className="p-6 border-t border-gray-200 dark:border-gray-700">
        <div className="flex flex-wrap items-center gap-3">
          {plugin.supports_testing && onTestConnection && (
            <button
              onClick={onTestConnection}
              disabled={testing || disabled}
              className="flex items-center justify-center space-x-2 px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-w-[160px]"
            >
              {testing ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>Testing...</span>
                </>
              ) : (
                <>
                  <CheckCircle className="h-4 w-4" />
                  <span>Test Connection</span>
                </>
              )}
            </button>
          )}

          <button
            onClick={onReset}
            disabled={disabled || saving}
            className="flex items-center justify-center space-x-2 px-4 py-2 bg-gray-600 text-white rounded-md hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-w-[120px]"
          >
            <XCircle className="h-4 w-4" />
            <span>Reset</span>
          </button>

          <button
            onClick={onSave}
            disabled={saving || disabled}
            className="flex items-center justify-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-w-[160px]"
          >
            {saving ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>Saving...</span>
              </>
            ) : (
              <>
                <CheckCircle className="h-4 w-4" />
                <span>Save Changes</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
