import { useState, useEffect } from 'react'
import { Circle } from 'lucide-react'
import axios from 'axios'

interface BackendStatus {
  status: 'connected' | 'disconnected' | 'error'
  device?: string
  version?: string
  speakers?: number
  lastChecked?: Date
  backendUrl?: string
}

export default function ConnectionStatus() {
  const [status, setStatus] = useState<BackendStatus>({ status: 'disconnected' })

  const getBackendUrl = () => {
    return `${window.location.origin}/api`
  }

  const checkBackendConnection = async () => {
    const backendUrl = getBackendUrl()

    try {
      const response = await axios.get('/health', {
        timeout: 5000
      })

      if (response.status === 200) {
        setStatus({
          status: 'connected',
          device: response.data.device,
          version: response.data.version,
          speakers: response.data.speakers,
          lastChecked: new Date(),
          backendUrl
        })
      }
    } catch (error) {
      console.warn('Backend connection check failed:', error)
      setStatus({
        status: 'error',
        lastChecked: new Date(),
        backendUrl
      })
    }
  }

  useEffect(() => {
    // Initial check
    checkBackendConnection()

    // Set up periodic checks every 15 seconds
    const interval = setInterval(checkBackendConnection, 15000)

    return () => clearInterval(interval)
  }, [])

  const getStatusColor = () => {
    switch (status.status) {
      case 'connected':
        return 'text-green-500'
      case 'error':
        return 'text-red-500'
      default:
        return 'text-gray-400'
    }
  }

  const getStatusText = () => {
    switch (status.status) {
      case 'connected':
        return `Backend: ${status.backendUrl} ${status.device ? `(${status.device.toUpperCase()})` : ''}`
      case 'error':
        return `Backend: ${status.backendUrl} (Error)`
      default:
        return `Backend: ${status.backendUrl} (Connecting...)`
    }
  }

  const getTooltipText = () => {
    if (status.status === 'connected') {
      return `✓ Connected to ${status.backendUrl}\nVersion: ${status.version || 'Unknown'}\nDevice: ${status.device || 'Unknown'}\nSpeakers: ${status.speakers || 0}\nLast checked: ${status.lastChecked?.toLocaleTimeString() || 'Never'}`
    }
    return `✗ Failed to connect to ${status.backendUrl}\nLast checked: ${status.lastChecked?.toLocaleTimeString() || 'Never'}\nClick to retry`
  }

  return (
    <div
      className="flex items-center space-x-2 text-sm cursor-pointer"
      title={getTooltipText()}
      onClick={() => checkBackendConnection()}
    >
      <Circle className={`h-3 w-3 ${getStatusColor()} ${status.status === 'connected' ? 'fill-current' : ''}`} />
      <span className="text-gray-500 dark:text-gray-400">{getStatusText()}</span>
    </div>
  )
}
