import { CheckCircle, Circle, AlertTriangle } from 'lucide-react'

interface Plugin {
  plugin_id: string
  name: string
  description: string
  enabled: boolean
  status: 'active' | 'disabled' | 'error'
}

interface PluginListSidebarProps {
  plugins: Plugin[]
  selectedPluginId: string | null
  onSelectPlugin: (pluginId: string) => void
  onToggleEnabled: (pluginId: string, enabled: boolean) => void
  loading?: boolean
}

export default function PluginListSidebar({
  plugins,
  selectedPluginId,
  onSelectPlugin,
  onToggleEnabled,
  loading = false
}: PluginListSidebarProps) {
  const getStatusIcon = (status: string, enabled: boolean) => {
    if (!enabled) {
      return <Circle className="h-4 w-4 text-gray-400" />
    }

    switch (status) {
      case 'active':
        return <CheckCircle className="h-4 w-4 text-green-500" />
      case 'error':
        return <AlertTriangle className="h-4 w-4 text-red-500" />
      default:
        return <Circle className="h-4 w-4 text-gray-400" />
    }
  }

  const getStatusBadge = (status: string, enabled: boolean) => {
    if (!enabled) {
      return (
        <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400">
          Disabled
        </span>
      )
    }

    switch (status) {
      case 'active':
        return (
          <span className="text-xs px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300">
            Active
          </span>
        )
      case 'error':
        return (
          <span className="text-xs px-2 py-0.5 rounded-full bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300">
            Error
          </span>
        )
      default:
        return (
          <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400">
            Unknown
          </span>
        )
    }
  }

  if (loading) {
    return (
      <div className="space-y-2 p-4">
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-20 bg-gray-200 dark:bg-gray-700 rounded-lg animate-pulse"
          />
        ))}
      </div>
    )
  }

  if (plugins.length === 0) {
    return (
      <div className="p-4 text-center text-gray-500 dark:text-gray-400">
        <p className="text-sm">No plugins found</p>
      </div>
    )
  }

  return (
    <div className="space-y-2 p-4 overflow-y-auto">
      {plugins.map((plugin) => {
        const isSelected = selectedPluginId === plugin.plugin_id

        return (
          <div
            key={plugin.plugin_id}
            onClick={() => onSelectPlugin(plugin.plugin_id)}
            className={`
              p-4 rounded-lg border cursor-pointer transition-all
              ${
                isSelected
                  ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                  : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 bg-white dark:bg-gray-800'
              }
            `}
          >
            {/* Plugin Header */}
            <div className="flex items-start justify-between mb-2">
              <div className="flex items-start space-x-2 flex-1">
                {getStatusIcon(plugin.status, plugin.enabled)}
                <div className="flex-1 min-w-0">
                  <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">
                    {plugin.name}
                  </h4>
                </div>
              </div>
            </div>

            {/* Plugin Description */}
            <p className="text-xs text-gray-600 dark:text-gray-400 mb-3 line-clamp-2">
              {plugin.description}
            </p>

            {/* Plugin Status and Toggle */}
            <div className="flex items-center justify-between">
              {getStatusBadge(plugin.status, plugin.enabled)}

              <label
                className="flex items-center space-x-2 cursor-pointer"
                onClick={(e) => {
                  e.stopPropagation()
                  onToggleEnabled(plugin.plugin_id, !plugin.enabled)
                }}
              >
                <span className="text-xs text-gray-600 dark:text-gray-400">
                  {plugin.enabled ? 'Enabled' : 'Disabled'}
                </span>
                <div
                  className={`
                    relative inline-flex h-5 w-9 items-center rounded-full transition-colors
                    ${plugin.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'}
                  `}
                >
                  <span
                    className={`
                      inline-block h-4 w-4 transform rounded-full bg-white transition-transform
                      ${plugin.enabled ? 'translate-x-5' : 'translate-x-0.5'}
                    `}
                  />
                </div>
              </label>
            </div>
          </div>
        )
      })}
    </div>
  )
}
