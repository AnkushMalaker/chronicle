import { useState, useEffect } from 'react'
import { Puzzle, RefreshCw, CheckCircle, Save, RotateCcw, AlertCircle } from 'lucide-react'
import { systemApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'

interface PluginSettingsProps {
  className?: string
}

export default function PluginSettings({ className }: PluginSettingsProps) {
  const [configYaml, setConfigYaml] = useState('')
  const [loading, setLoading] = useState(false)
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const { isAdmin } = useAuth()

  useEffect(() => {
    loadPluginsConfig()
  }, [])

  const loadPluginsConfig = async () => {
    setLoading(true)
    setError('')
    setMessage('')

    try {
      const response = await systemApi.getPluginsConfigRaw()
      setConfigYaml(response.data.config_yaml || response.data)
      setMessage('Configuration loaded successfully')
      setTimeout(() => setMessage(''), 3000)
    } catch (err: any) {
      const status = err.response?.status
      if (status === 401) {
        setError('Unauthorized: admin privileges required')
      } else {
        setError(err.response?.data?.error || 'Failed to load configuration')
      }
    } finally {
      setLoading(false)
    }
  }

  const validateConfig = async () => {
    if (!configYaml.trim()) {
      setError('Configuration cannot be empty')
      return
    }

    setValidating(true)
    setError('')
    setMessage('')

    try {
      const response = await systemApi.validatePluginsConfig(configYaml)
      if (response.data.valid) {
        setMessage('✅ Configuration is valid')
      } else {
        setError(response.data.error || 'Validation failed')
      }
      setTimeout(() => setMessage(''), 3000)
    } catch (err: any) {
      setError(err.response?.data?.error || 'Validation failed')
    } finally {
      setValidating(false)
    }
  }

  const saveConfig = async () => {
    if (!configYaml.trim()) {
      setError('Configuration cannot be empty')
      return
    }

    setSaving(true)
    setError('')
    setMessage('')

    try {
      await systemApi.updatePluginsConfigRaw(configYaml)
      setMessage('✅ Configuration saved successfully. Restart backend for changes to take effect.')
      setTimeout(() => setMessage(''), 5000)
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }

  const resetConfig = () => {
    loadPluginsConfig()
    setMessage('Configuration reset to file version')
    setTimeout(() => setMessage(''), 3000)
  }

  if (!isAdmin) {
    return null
  }

  return (
    <div className={className}>
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center space-x-2">
            <Puzzle className="h-5 w-5 text-blue-600" />
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Plugin Configuration
            </h3>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={resetConfig}
              disabled={loading || saving}
              className="flex items-center space-x-1 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 disabled:opacity-50"
            >
              <RotateCcw className="h-4 w-4" />
              <span>Reset</span>
            </button>
            <button
              onClick={loadPluginsConfig}
              disabled={loading || saving}
              className="flex items-center space-x-1 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 disabled:opacity-50"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              <span>Reload</span>
            </button>
          </div>
        </div>

        {/* Messages */}
        {message && (
          <div className="mb-4 p-3 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md flex items-start space-x-2">
            <CheckCircle className="h-5 w-5 text-green-600 dark:text-green-400 mt-0.5" />
            <p className="text-sm text-green-700 dark:text-green-300">{message}</p>
          </div>
        )}

        {error && (
          <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md flex items-start space-x-2">
            <AlertCircle className="h-5 w-5 text-red-600 dark:text-red-400 mt-0.5" />
            <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
          </div>
        )}

        {/* Editor */}
        <div className="mb-4">
          <textarea
            value={configYaml}
            onChange={(e) => setConfigYaml(e.target.value)}
            disabled={loading || saving}
            className="w-full h-96 p-4 font-mono text-sm bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-y"
            placeholder="Loading configuration..."
            spellCheck={false}
          />
        </div>

        {/* Actions */}
        <div className="flex space-x-3">
          <button
            onClick={validateConfig}
            disabled={loading || validating || saving}
            className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
          >
            <CheckCircle className="h-4 w-4" />
            <span>{validating ? 'Validating...' : 'Validate'}</span>
          </button>

          <button
            onClick={saveConfig}
            disabled={loading || saving || validating}
            className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50"
          >
            <Save className="h-4 w-4" />
            <span>{saving ? 'Saving...' : 'Save Changes'}</span>
          </button>
        </div>

        {/* Help text */}
        <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md">
          <h4 className="text-sm font-medium text-blue-900 dark:text-blue-100 mb-2">
            Configuration Help
          </h4>
          <ul className="text-sm text-blue-700 dark:text-blue-300 space-y-1 list-disc list-inside">
            <li>Define enabled plugins and their trigger types</li>
            <li>Configure wake words for command-based plugins</li>
            <li>Set plugin URLs and authentication tokens</li>
            <li>Changes require backend restart to take effect</li>
          </ul>
        </div>
      </div>
    </div>
  )
}
