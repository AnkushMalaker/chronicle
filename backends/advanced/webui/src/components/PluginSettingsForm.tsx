import { useState, useEffect } from 'react'
import { RefreshCw, AlertCircle } from 'lucide-react'
import { systemApi } from '../services/api'
import PluginListSidebar from './plugins/PluginListSidebar'
import PluginConfigPanel from './plugins/PluginConfigPanel'

interface PluginMetadata {
  plugin_id: string
  name: string
  description: string
  enabled: boolean
  status: 'active' | 'disabled' | 'error'
  supports_testing: boolean
  config_schema: {
    orchestration: any
    settings: Record<string, any>
    env_vars: Record<string, any>
  }
}

interface PluginConfig {
  orchestration: {
    enabled: boolean
    events: string[]
    condition: {
      type: 'always' | 'wake_word'
      wake_words?: string[]
    }
  }
  settings: Record<string, any>
  env_vars: Record<string, any>
}

interface PluginSettingsFormProps {
  className?: string
}

export default function PluginSettingsForm({ className }: PluginSettingsFormProps) {
  const [plugins, setPlugins] = useState<PluginMetadata[]>([])
  const [selectedPluginId, setSelectedPluginId] = useState<string | null>(null)
  const [currentConfig, setCurrentConfig] = useState<PluginConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<PluginConfig | null>(null)
  const [loading, setLoading] = useState(false)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState<any>(null)

  const selectedPlugin = plugins.find((p) => p.plugin_id === selectedPluginId)

  useEffect(() => {
    loadPlugins()
  }, [])

  useEffect(() => {
    if (selectedPluginId) {
      loadPluginConfig(selectedPluginId)
    }
  }, [selectedPluginId])

  const loadPlugins = async () => {
    setLoading(true)
    setError('')
    setMessage('')

    try {
      const response = await systemApi.getPluginsMetadata()
      const pluginsData = response.data.plugins || []
      setPlugins(pluginsData)

      // Auto-select first plugin if none selected
      if (!selectedPluginId && pluginsData.length > 0) {
        setSelectedPluginId(pluginsData[0].plugin_id)
      }

      setMessage('Plugins loaded successfully')
      setTimeout(() => setMessage(''), 3000)
    } catch (err: any) {
      const status = err.response?.status
      if (status === 401) {
        setError('Unauthorized: admin privileges required')
      } else if (status === 404 || status === 405) {
        setError('Backend does not expose plugin configuration endpoints')
      } else {
        setError(err.response?.data?.detail || 'Failed to load plugins')
      }
    } finally {
      setLoading(false)
    }
  }

  const loadPluginConfig = (pluginId: string) => {
    const plugin = plugins.find((p) => p.plugin_id === pluginId)
    if (!plugin) return

    // Extract current configuration from plugin metadata
    const config: PluginConfig = {
      orchestration: {
        enabled: plugin.enabled || false,
        events: [],
        condition: { type: 'always' }
      },
      settings: {},
      env_vars: {}
    }

    // Load settings with defaults
    Object.keys(plugin.config_schema.settings || {}).forEach((key) => {
      const schema = plugin.config_schema.settings[key]
      config.settings[key] = schema.default ?? ''
    })

    // Load env vars (will be masked values from backend)
    Object.keys(plugin.config_schema.env_vars || {}).forEach((key) => {
      const schema = plugin.config_schema.env_vars[key]
      config.env_vars[key] = schema.value ?? ''
    })

    setCurrentConfig(config)
    setOriginalConfig(JSON.parse(JSON.stringify(config)))
    setErrors({})
    setTestResult(null)
  }

  const handlePluginSelect = (pluginId: string) => {
    setSelectedPluginId(pluginId)
  }

  const handleToggleEnabled = async (pluginId: string, enabled: boolean) => {
    try {
      // Update the plugin's enabled state
      const plugin = plugins.find((p) => p.plugin_id === pluginId)
      if (!plugin) return

      await systemApi.updatePluginConfigStructured(pluginId, {
        orchestration: {
          enabled,
          events: plugin.config_schema.orchestration?.events || [],
          condition: plugin.config_schema.orchestration?.condition || { type: 'always' }
        }
      })

      // Reload plugins to reflect changes
      await loadPlugins()
      setMessage(`Plugin ${enabled ? 'enabled' : 'disabled'} successfully`)
      setTimeout(() => setMessage(''), 3000)
    } catch (err: any) {
      setError(err.response?.data?.detail || `Failed to ${enabled ? 'enable' : 'disable'} plugin`)
    }
  }

  const handleConfigChange = (config: PluginConfig) => {
    setCurrentConfig(config)
    setErrors({})
  }

  const handleTestConnection = async () => {
    if (!selectedPluginId || !currentConfig) return

    setTesting(true)
    setTestResult(null)
    setError('')

    try {
      const response = await systemApi.testPluginConnection(selectedPluginId, {
        orchestration: currentConfig.orchestration,
        settings: currentConfig.settings,
        env_vars: currentConfig.env_vars
      })

      setTestResult(response.data)

      if (response.data.success) {
        setMessage('Connection test successful')
        setTimeout(() => setMessage(''), 3000)
      }
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || 'Connection test failed'
      setTestResult({
        success: false,
        message: errorMessage
      })
      setError(errorMessage)
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async () => {
    if (!selectedPluginId || !currentConfig) return

    setSaving(true)
    setError('')
    setMessage('')
    setErrors({})

    try {
      // Filter out masked env vars (don't send unchanged secrets)
      const envVarsToSend: Record<string, any> = {}
      Object.keys(currentConfig.env_vars).forEach((key) => {
        const value = currentConfig.env_vars[key]
        // Only send if value is not masked
        if (typeof value !== 'string' || !value.includes('••••')) {
          envVarsToSend[key] = value
        }
      })

      await systemApi.updatePluginConfigStructured(selectedPluginId, {
        orchestration: currentConfig.orchestration,
        settings: currentConfig.settings,
        env_vars: Object.keys(envVarsToSend).length > 0 ? envVarsToSend : undefined
      })

      setMessage('Configuration saved successfully. Restart backend to apply changes.')
      setTimeout(() => setMessage(''), 5000)

      // Reload plugins to reflect changes
      await loadPlugins()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    if (originalConfig) {
      setCurrentConfig(JSON.parse(JSON.stringify(originalConfig)))
      setErrors({})
      setTestResult(null)
      setMessage('Configuration reset to original values')
      setTimeout(() => setMessage(''), 3000)
    }
  }

  const handleRefresh = async () => {
    await loadPlugins()
  }

  return (
    <div className={className}>
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-gray-200 dark:border-gray-700">
          <div>
            <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Plugin Configuration
            </h2>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              Configure plugins, manage orchestration, and test connections
            </p>
          </div>
          <button
            onClick={handleRefresh}
            disabled={loading}
            className="flex items-center space-x-2 px-4 py-2 bg-gray-600 text-white rounded-md hover:bg-gray-700 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>

        {/* Status Messages */}
        {message && (
          <div className="mx-6 mt-4 p-3 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md">
            <p className="text-sm text-green-700 dark:text-green-300">{message}</p>
          </div>
        )}

        {error && (
          <div className="mx-6 mt-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md">
            <div className="flex">
              <AlertCircle className="h-5 w-5 text-red-400 mr-2 flex-shrink-0" />
              <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
            </div>
          </div>
        )}

        {/* Main Content */}
        <div className="flex h-[600px]">
          {/* Sidebar */}
          <div className="w-1/3 border-r border-gray-200 dark:border-gray-700 overflow-y-auto">
            <PluginListSidebar
              plugins={plugins}
              selectedPluginId={selectedPluginId}
              onSelectPlugin={handlePluginSelect}
              onToggleEnabled={handleToggleEnabled}
              loading={loading}
            />
          </div>

          {/* Config Panel */}
          <div className="flex-1 overflow-y-auto">
            {selectedPlugin && currentConfig ? (
              <PluginConfigPanel
                plugin={selectedPlugin}
                config={currentConfig}
                onChange={handleConfigChange}
                onTestConnection={selectedPlugin.supports_testing ? handleTestConnection : undefined}
                onSave={handleSave}
                onReset={handleReset}
                errors={errors}
                testResult={testResult}
                testing={testing}
                saving={saving}
                disabled={loading}
              />
            ) : (
              <div className="flex items-center justify-center h-full text-gray-500 dark:text-gray-400">
                <p>Select a plugin to configure</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
