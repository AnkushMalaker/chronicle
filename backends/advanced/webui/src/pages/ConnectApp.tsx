import { useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { Smartphone, Copy, Check } from 'lucide-react'
import { useTheme } from '../contexts/ThemeContext'

function getBackendHttpUrl(): string {
  const { protocol, hostname, port } = window.location

  const isStandardPort =
    (protocol === 'https:' && (port === '' || port === '443')) ||
    (protocol === 'http:' && (port === '' || port === '80'))

  const basePath = import.meta.env.BASE_URL
  if (isStandardPort && basePath && basePath !== '/') {
    // Caddy path-based routing — return full origin
    return `${protocol}//${hostname}`
  }

  if (import.meta.env.VITE_BACKEND_URL) {
    const url = import.meta.env.VITE_BACKEND_URL as string
    // If it's a relative URL, make it absolute
    if (url.startsWith('/') || url === '') {
      return `${protocol}//${hostname}${port ? `:${port}` : ''}`
    }
    return url
  }

  if (isStandardPort) {
    return `${protocol}//${hostname}`
  }

  if (port === '5173') {
    return `${protocol}//${hostname}:8000`
  }

  return `${protocol}//${hostname}${port ? `:${port}` : ''}`
}

export default function ConnectApp() {
  const { isDark } = useTheme()
  const [copied, setCopied] = useState(false)
  const backendUrl = getBackendHttpUrl()

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(backendUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Fallback for older browsers
      const textArea = document.createElement('textarea')
      textArea.value = backendUrl
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center space-x-3">
        <Smartphone className="h-6 w-6 text-blue-600" />
        <h2 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">
          Connect App
        </h2>
      </div>

      <p className="text-gray-600 dark:text-gray-400">
        Scan this QR code with the Chronicle mobile app to connect it to your backend.
      </p>

      {/* QR Code */}
      <div className="flex flex-col items-center space-y-4 py-6">
        <div className="p-4 bg-white rounded-xl shadow-sm border border-gray-200 dark:border-gray-600">
          <QRCodeSVG
            value={backendUrl}
            size={256}
            level="M"
            fgColor={isDark ? '#1f2937' : '#111827'}
            bgColor="#ffffff"
          />
        </div>

        {/* URL display + copy */}
        <div className="flex items-center space-x-2">
          <code className="px-3 py-1.5 bg-gray-100 dark:bg-gray-700 rounded text-sm text-gray-800 dark:text-gray-200 font-mono">
            {backendUrl}
          </code>
          <button
            onClick={handleCopy}
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-gray-600 dark:text-gray-300"
            title="Copy URL"
          >
            {copied ? (
              <Check className="h-4 w-4 text-green-500" />
            ) : (
              <Copy className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>

      {/* Instructions */}
      <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
        <h3 className="font-medium text-blue-900 dark:text-blue-200 mb-2">
          How to connect
        </h3>
        <ol className="list-decimal list-inside space-y-1.5 text-sm text-blue-800 dark:text-blue-300">
          <li>Open the Chronicle app on your phone</li>
          <li>Go to Settings and tap <strong>Scan QR Code</strong></li>
          <li>Point your camera at the QR code above</li>
          <li>The backend URL will be configured automatically</li>
        </ol>
      </div>
    </div>
  )
}
