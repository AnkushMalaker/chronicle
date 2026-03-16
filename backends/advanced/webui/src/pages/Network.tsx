import { useState } from 'react'
import { Network as NetworkIcon, RefreshCw, CheckCircle, XCircle, Wifi, WifiOff, Radio, Search, Server, Smartphone } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import { systemApi } from '../services/api'

interface DiscoveredService {
  name: string
  url: string | null
  reachable: boolean
  error?: string
  labels?: Record<string, string>
  host?: string
}

interface AdvertisedService {
  name: string
  port: number
  label?: string
}

interface ConnectedDevice {
  client_id: string
  device_name: string
  user_email?: string
  connected: boolean
  has_active_conversation: boolean
}

interface NetworkData {
  tailscale_available: boolean
  advertising: AdvertisedService[]
  discovered_services: DiscoveredService[]
  connected_devices?: ConnectedDevice[]
  error?: string
}

const SERVICE_DISPLAY: Record<string, { label: string; description: string }> = {
  'chronicle-backend': { label: 'Chronicle Backend', description: 'Core API and audio processing' },
  'chronicle-speaker': { label: 'Speaker Recognition', description: 'Voice identification service' },
  'chronicle-asr': { label: 'ASR Service', description: 'Offline speech-to-text' },
  'chronicle-openmemory': { label: 'OpenMemory MCP', description: 'Cross-client memory server' },
  'chronicle-llm': { label: 'Local LLM', description: 'Local LLM via llama.cpp' },
  'chronicle-tts': { label: 'Text-to-Speech', description: 'TTS synthesis service' },
  'chronicle-relay': { label: 'HAVPE Relay', description: 'ESP32 audio bridge' },
}

function getServiceDisplay(name: string) {
  if (SERVICE_DISPLAY[name]) return SERVICE_DISPLAY[name]
  // Derive a label from unknown chronicle-* names
  const suffix = name.replace('chronicle-', '')
  return { label: suffix.charAt(0).toUpperCase() + suffix.slice(1), description: '' }
}

// Group discovered services by host
function groupByHost(services: DiscoveredService[]): Record<string, DiscoveredService[]> {
  const groups: Record<string, DiscoveredService[]> = {}
  for (const svc of services) {
    const host = svc.host || svc.url?.replace(/^https?:\/\//, '').replace(/:\d+$/, '') || 'unknown'
    if (!groups[host]) groups[host] = []
    groups[host].push(svc)
  }
  return groups
}

export default function Network() {
  const { isAdmin } = useAuth()
  const [isScanning, setIsScanning] = useState(false)

  const { data, isLoading, refetch, dataUpdatedAt } = useQuery<NetworkData>({
    queryKey: ['system', 'network'],
    queryFn: async () => {
      const response = await systemApi.getNetworkDiscovery()
      return response.data
    },
    enabled: isAdmin,
    staleTime: 60_000,
  })

  const handleScan = async () => {
    setIsScanning(true)
    try {
      await refetch()
    } finally {
      setIsScanning(false)
    }
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <NetworkIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view network status.
        </p>
      </div>
    )
  }

  const loading = isLoading || isScanning
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null
  const advertisedNames = new Set(data?.advertising?.map(s => s.name) || [])
  const nodeGroups = data?.discovered_services ? groupByHost(data.discovered_services) : {}

  return (
    <div>
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <NetworkIcon className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Network
          </h1>
        </div>
        <div className="flex items-center space-x-4">
          {lastUpdated && (
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Last scan: {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <button
            onClick={handleScan}
            disabled={loading}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            {loading ? (
              <RefreshCw className="h-4 w-4 animate-spin" />
            ) : (
              <Search className="h-4 w-4" />
            )}
            <span>{loading ? 'Scanning...' : 'Scan Network'}</span>
          </button>
        </div>
      </div>

      {/* Tailscale Status */}
      <div className={`rounded-lg p-4 border mb-6 ${
        data?.tailscale_available
          ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
          : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-200 dark:border-yellow-800'
      }`}>
        <div className="flex items-center space-x-3">
          {data?.tailscale_available ? (
            <>
              <Wifi className="h-5 w-5 text-green-600 dark:text-green-400" />
              <div>
                <span className="font-medium text-green-800 dark:text-green-200">Tailscale Connected</span>
                <p className="text-sm text-green-600 dark:text-green-400">
                  Service discovery via minidisc is active. Services on your Tailnet can find each other automatically.
                </p>
              </div>
            </>
          ) : (
            <>
              <WifiOff className="h-5 w-5 text-yellow-600 dark:text-yellow-400" />
              <div>
                <span className="font-medium text-yellow-800 dark:text-yellow-200">Tailscale Not Detected</span>
                <p className="text-sm text-yellow-600 dark:text-yellow-400">
                  {data?.error
                    ? data.error
                    : 'Install Tailscale and mount the socket to enable automatic service discovery across machines.'}
                </p>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Advertising (This Node) */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Radio className="h-5 w-5 mr-2 text-blue-600" />
          This Node is Advertising
        </h3>
        {data?.advertising && data.advertising.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {data.advertising.map((svc) => {
              const display = getServiceDisplay(svc.name)
              const displayLabel = svc.label || display.label
              return (
                <div key={svc.name} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div>
                    <div className="font-medium text-gray-900 dark:text-gray-100">{displayLabel}</div>
                    <div className="text-sm text-gray-500 dark:text-gray-400">Port {svc.port}</div>
                  </div>
                  <span className="text-xs px-2 py-1 bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200 rounded">
                    broadcasting
                  </span>
                </div>
              )
            })}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-center py-4">
            {data?.tailscale_available
              ? 'No services being advertised'
              : 'Tailscale required for service advertisement'}
          </p>
        )}
      </div>

      {/* Discovered Services — grouped by node */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Search className="h-5 w-5 mr-2 text-blue-600" />
          Discovered on Tailnet
        </h3>
        {Object.keys(nodeGroups).length > 0 ? (
          <div className="space-y-4">
            {Object.entries(nodeGroups).map(([host, services]) => {
              const hasEdge = services.some(s => s.labels?.type === 'edge')
              return (
                <div key={host} className="border border-gray-200 dark:border-gray-600 rounded-lg overflow-hidden">
                  {/* Node header */}
                  <div className="flex items-center justify-between px-4 py-2.5 bg-gray-100 dark:bg-gray-700">
                    <div className="flex items-center space-x-2">
                      <Server className="h-4 w-4 text-gray-500 dark:text-gray-400" />
                      <span className="font-medium text-gray-900 dark:text-gray-100 font-mono text-sm">{host}</span>
                      {hasEdge && (
                        <span className="text-xs px-1.5 py-0.5 bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300 rounded">
                          edge
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                      {services.length} service{services.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  {/* Services on this node */}
                  <div className="divide-y divide-gray-100 dark:divide-gray-700">
                    {services.map((svc) => {
                      const display = getServiceDisplay(svc.name)
                      const isLocal = advertisedNames.has(svc.name)
                      return (
                        <div key={svc.name} className={`p-3 ${!svc.url ? 'opacity-60' : ''}`}>
                          <div className="flex items-center justify-between">
                            <div className="flex-1">
                              <div className="flex items-center space-x-2">
                                <span className="font-medium text-gray-900 dark:text-gray-100">
                                  {display.label}
                                </span>
                                {isLocal && svc.url && (
                                  <span className="text-xs px-1.5 py-0.5 bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 rounded">
                                    local
                                  </span>
                                )}
                              </div>
                              {display.description && (
                                <div className="text-sm text-gray-500 dark:text-gray-400">
                                  {display.description}
                                </div>
                              )}
                              {svc.url && (
                                <code className="text-xs text-gray-600 dark:text-gray-400 font-mono mt-1 block">
                                  {svc.url}
                                </code>
                              )}
                            </div>
                            <div className="ml-3 flex-shrink-0">
                              {svc.url ? (
                                svc.reachable ? (
                                  <CheckCircle className="h-5 w-5 text-green-500" />
                                ) : (
                                  <XCircle className="h-5 w-5 text-yellow-500" />
                                )
                              ) : (
                                <span className="text-xs text-gray-400 dark:text-gray-500">not found</span>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-center py-4">
            {loading
              ? 'Scanning Tailnet...'
              : data?.tailscale_available
                ? 'No services discovered. Make sure other machines are running Chronicle services.'
                : 'Tailscale required for service discovery'}
          </p>
        )}
      </div>

      {/* Connected Devices */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Smartphone className="h-5 w-5 mr-2 text-blue-600" />
          Connected Devices
        </h3>
        {data?.connected_devices && data.connected_devices.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {data.connected_devices.map((device) => (
              <div key={device.client_id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                <div>
                  <div className="font-medium text-gray-900 dark:text-gray-100">{device.device_name}</div>
                  {device.user_email && (
                    <div className="text-sm text-gray-500 dark:text-gray-400">{device.user_email}</div>
                  )}
                  <code className="text-xs text-gray-400 dark:text-gray-500 font-mono">{device.client_id}</code>
                </div>
                <div className="ml-3 flex-shrink-0 flex flex-col items-end space-y-1">
                  {device.connected ? (
                    <span className="text-xs px-2 py-1 bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 rounded">
                      connected
                    </span>
                  ) : (
                    <span className="text-xs px-2 py-1 bg-gray-100 text-gray-600 dark:bg-gray-600 dark:text-gray-300 rounded">
                      disconnected
                    </span>
                  )}
                  {device.has_active_conversation && (
                    <span className="text-xs px-2 py-1 bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200 rounded">
                      streaming
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-center py-4">
            No devices connected via WebSocket
          </p>
        )}
      </div>

      {/* Legend */}
      <div className="bg-gray-50 dark:bg-gray-700 rounded-lg border border-gray-200 dark:border-gray-600 p-4">
        <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Status Legend</h4>
        <div className="flex flex-wrap gap-4 text-sm text-gray-600 dark:text-gray-400">
          <div className="flex items-center space-x-1.5">
            <CheckCircle className="h-4 w-4 text-green-500" />
            <span>Reachable</span>
          </div>
          <div className="flex items-center space-x-1.5">
            <XCircle className="h-4 w-4 text-yellow-500" />
            <span>Found but /health unreachable</span>
          </div>
          <div className="flex items-center space-x-1.5">
            <span className="text-xs px-1.5 py-0.5 bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300 rounded">edge</span>
            <span>Remote edge node</span>
          </div>
          <div className="flex items-center space-x-1.5">
            <span className="text-xs text-gray-400">not found</span>
            <span>Not discovered on Tailnet</span>
          </div>
        </div>
      </div>
    </div>
  )
}
