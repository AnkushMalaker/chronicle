import { useState, useEffect } from 'react'
import { ChevronDown, CheckCircle, Loader2 } from 'lucide-react'
import { conversationsApi } from '../services/api'

interface TranscriptVersion {
  version_id: string
  transcript: string
  segments: any[]
  provider: string
  model?: string
  created_at: string
  processing_time_seconds?: number
  metadata?: any
}

interface MemoryVersion {
  version_id: string
  memory_count: number
  transcript_version_id: string
  provider: string
  model?: string
  created_at: string
  processing_time_seconds?: number
  metadata?: any
}

interface VersionHistory {
  transcript_versions: TranscriptVersion[]
  memory_versions: MemoryVersion[]
  active_transcript_version: string
  active_memory_version: string
}

interface ConversationVersionDropdownProps {
  conversationId: string
  versionInfo?: {
    transcript_count: number
    memory_count: number
    active_transcript_version?: string
    active_memory_version?: string
    active_transcript_version_number?: number
    active_memory_version_number?: number
  }
  onVersionChange: () => void
}

export default function ConversationVersionDropdown({
  conversationId,
  versionInfo,
  onVersionChange
}: ConversationVersionDropdownProps) {
  const [versionHistory, setVersionHistory] = useState<VersionHistory | null>(null)
  const [loading, setLoading] = useState(false)
  const [activating, setActivating] = useState<{ type: 'transcript' | 'memory', versionId: string } | null>(null)
  const [showTranscriptDropdown, setShowTranscriptDropdown] = useState(false)
  const [showMemoryDropdown, setShowMemoryDropdown] = useState(false)

  // Close dropdowns when clicking outside
  useEffect(() => {
    const handleClickOutside = () => {
      setShowTranscriptDropdown(false)
      setShowMemoryDropdown(false)
    }
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

  const loadVersionHistory = async () => {
    try {
      setLoading(true)
      const response = await conversationsApi.getVersionHistory(conversationId)
      setVersionHistory(response.data)
    } catch (err: any) {
      console.error('Failed to load version history:', err)
    } finally {
      setLoading(false)
    }
  }

  // Don't auto-load version history - only load when dropdown is opened
  // This prevents API spam when rendering many conversations in a list

  const handleActivateVersion = async (type: 'transcript' | 'memory', versionId: string) => {
    try {
      setActivating({ type, versionId })

      if (type === 'transcript') {
        await conversationsApi.activateTranscriptVersion(conversationId, versionId)
        setShowTranscriptDropdown(false)
      } else {
        await conversationsApi.activateMemoryVersion(conversationId, versionId)
        setShowMemoryDropdown(false)
      }

      // Reload version history to update active version
      await loadVersionHistory()

      // Notify parent component to refresh conversation data
      onVersionChange()

    } catch (err: any) {
      console.error(`Failed to activate ${type} version:`, err)
    } finally {
      setActivating(null)
    }
  }

  const formatVersionLabel = (version: TranscriptVersion | MemoryVersion, index: number) => {
    return `v${index + 1} (${version.provider}${version.model ? ` ${version.model}` : ''})`
  }

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString()
  }

  // Don't show anything if there are no multiple versions
  if (!versionInfo || ((versionInfo.transcript_count || 0) <= 1 && (versionInfo.memory_count || 0) <= 1)) {
    return null
  }

  return (
    <div className="flex items-center space-x-4 text-sm">
      {/* Transcript Version Dropdown */}
      {(versionInfo.transcript_count || 0) > 1 && (
        <div className="relative">
          <button
            onClick={(e) => {
              e.stopPropagation()
              const isOpening = !showTranscriptDropdown
              setShowTranscriptDropdown(isOpening)
              setShowMemoryDropdown(false)
              // Load version history on first click
              if (isOpening && !versionHistory) {
                loadVersionHistory()
              }
            }}
            className="flex items-center space-x-1 px-3 py-1 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-600 rounded text-blue-700 dark:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-900/30"
          >
            <span>
              Transcript: {versionHistory ?
                `v${versionHistory.transcript_versions.findIndex(v => v.version_id === versionHistory.active_transcript_version) + 1}` :
                (versionInfo?.active_transcript_version_number ? `v${versionInfo.active_transcript_version_number}` : '-')
              }
            </span>
            <ChevronDown className="h-3 w-3" />
          </button>

          {showTranscriptDropdown && versionHistory && (
            <div
              className="absolute top-full left-0 mt-1 w-64 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg z-10"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="py-1">
                {versionHistory.transcript_versions.map((version, index) => (
                  <button
                    key={version.version_id}
                    onClick={() => handleActivateVersion('transcript', version.version_id)}
                    disabled={activating?.type === 'transcript' && activating?.versionId === version.version_id}
                    className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 ${
                      version.version_id === versionHistory.active_transcript_version ? 'bg-blue-50 dark:bg-blue-900/20' : ''
                    }`}
                  >
                    <div className="flex-1">
                      <div className="flex items-center space-x-2">
                        {version.version_id === versionHistory.active_transcript_version && (
                          <CheckCircle className="h-3 w-3 text-blue-600 dark:text-blue-400" />
                        )}
                        <span className="font-medium">{formatVersionLabel(version, index)}</span>
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        {formatDate(version.created_at)} • {version.segments?.length || 0} segments
                      </div>
                    </div>
                    {activating?.type === 'transcript' && activating?.versionId === version.version_id && (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Memory Version Dropdown */}
      {(versionInfo.memory_count || 0) > 1 && (
        <div className="relative">
          <button
            onClick={(e) => {
              e.stopPropagation()
              const isOpening = !showMemoryDropdown
              setShowMemoryDropdown(isOpening)
              setShowTranscriptDropdown(false)
              // Load version history on first click
              if (isOpening && !versionHistory) {
                loadVersionHistory()
              }
            }}
            className="flex items-center space-x-1 px-3 py-1 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-600 rounded text-green-700 dark:text-green-300 hover:bg-green-100 dark:hover:bg-green-900/30"
          >
            <span>
              Memory: {versionHistory ?
                `v${versionHistory.memory_versions.findIndex(v => v.version_id === versionHistory.active_memory_version) + 1}` :
                (versionInfo?.active_memory_version_number ? `v${versionInfo.active_memory_version_number}` : '-')
              }
            </span>
            <ChevronDown className="h-3 w-3" />
          </button>

          {showMemoryDropdown && versionHistory && (
            <div
              className="absolute top-full left-0 mt-1 w-64 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg z-10"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="py-1">
                {versionHistory.memory_versions.map((version, index) => (
                  <button
                    key={version.version_id}
                    onClick={() => handleActivateVersion('memory', version.version_id)}
                    disabled={activating?.type === 'memory' && activating?.versionId === version.version_id}
                    className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 ${
                      version.version_id === versionHistory.active_memory_version ? 'bg-green-50 dark:bg-green-900/20' : ''
                    }`}
                  >
                    <div className="flex-1">
                      <div className="flex items-center space-x-2">
                        {version.version_id === versionHistory.active_memory_version && (
                          <CheckCircle className="h-3 w-3 text-green-600 dark:text-green-400" />
                        )}
                        <span className="font-medium">{formatVersionLabel(version, index)}</span>
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        {formatDate(version.created_at)} • {version.memory_count} memories
                      </div>
                    </div>
                    {activating?.type === 'memory' && activating?.versionId === version.version_id && (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {loading && (
        <div className="flex items-center space-x-1 text-gray-500 dark:text-gray-400">
          <Loader2 className="h-3 w-3 animate-spin" />
          <span className="text-xs">Loading versions...</span>
        </div>
      )}
    </div>
  )
}
