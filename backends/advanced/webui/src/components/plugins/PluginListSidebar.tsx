import { StateBadge } from '../ui'

interface Plugin {
  plugin_id: string
  name: string
  description: string
  enabled: boolean
  status: 'active' | 'disabled' | 'error'
}

interface ConnectivityResult {
  ok: boolean
  message: string
  latency_ms?: number
}

interface PluginListSidebarProps {
  plugins: Plugin[]
  selectedPluginId: string | null
  onSelectPlugin: (pluginId: string) => void
  onToggleEnabled: (pluginId: string, enabled: boolean) => void
  loading?: boolean
  connectivity?: Record<string, ConnectivityResult>
}

export default function PluginListSidebar({
  plugins,
  selectedPluginId,
  onSelectPlugin,
  onToggleEnabled,
  loading = false,
  connectivity = {}
}: PluginListSidebarProps) {
  const getStatusDot = (plugin: Plugin) => {
    if (!plugin.enabled) {
      return (
        <span title="Disabled" className="relative flex h-3 w-3">
          <span className="h-3 w-3 rounded-full bg-gray-400" />
        </span>
      )
    }

    const conn = connectivity[plugin.plugin_id]

    if (!conn) {
      // No connectivity data yet — gray dot
      return (
        <span title="Checking..." className="relative flex h-3 w-3">
          <span className="h-3 w-3 rounded-full bg-gray-400" />
        </span>
      )
    }

    if (conn.ok) {
      const tooltip = conn.latency_ms != null
        ? `Connected (${conn.latency_ms}ms)`
        : conn.message
      return (
        <span title={tooltip} className="relative flex h-3 w-3">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500" />
        </span>
      )
    }

    // Not ok
    return (
      <span title={conn.message} className="relative flex h-3 w-3">
        <span className="h-3 w-3 rounded-full bg-red-500" />
      </span>
    )
  }

  const getStatusBadge = (plugin: Plugin) => {
    if (!plugin.enabled) {
      return <StateBadge tone="neutral">Disabled</StateBadge>
    }

    const conn = connectivity[plugin.plugin_id]
    if (conn?.ok) {
      const label = conn.latency_ms != null ? `Active (${conn.latency_ms}ms)` : 'Active'
      return <StateBadge tone="success">{label}</StateBadge>
    }

    if (conn && !conn.ok) {
      return <StateBadge tone="danger">Error</StateBadge>
    }

    // Fallback to status-based badge
    switch (plugin.status) {
      case 'active':
        return <StateBadge tone="success">Active</StateBadge>
      case 'error':
        return <StateBadge tone="danger">Error</StateBadge>
      default:
        return <StateBadge tone="neutral">Unknown</StateBadge>
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
              <div className="flex items-center space-x-2 flex-1">
                {getStatusDot(plugin)}
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
              {getStatusBadge(plugin)}

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
