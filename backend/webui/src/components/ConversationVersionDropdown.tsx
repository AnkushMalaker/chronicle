import { useState, useEffect } from 'react'
import { ChevronDown, CheckCircle, Loader2 } from 'lucide-react'
import { conversationsApi } from '../services/api'
import { MetadataChip } from './ui'

interface TranscriptVersion {
  version_id: string
  transcript: string
  segments: any[]
  provider: string
  model?: string
  created_at: string
  processing_time_seconds?: number
  // How speaker labels were produced: "provider" (ASR self-diarized),
  // "pyannote" (speaker-recognition service), or null/undefined (no diarization).
  diarization_source?: string | null
  metadata?: any
}

interface VersionHistory {
  transcript_versions: TranscriptVersion[]
  active_transcript_version: string
}

interface ConversationVersionDropdownProps {
  conversationId: string
  versionInfo?: {
    transcript_count: number
    active_transcript_version?: string
    active_transcript_version_number?: number
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
  const [activating, setActivating] = useState<string | null>(null)
  const [showTranscriptDropdown, setShowTranscriptDropdown] = useState(false)

  // Close dropdowns when clicking outside
  useEffect(() => {
    const handleClickOutside = () => {
      setShowTranscriptDropdown(false)
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

  const handleActivateVersion = async (versionId: string) => {
    try {
      setActivating(versionId)
      await conversationsApi.activateTranscriptVersion(conversationId, versionId)
      setShowTranscriptDropdown(false)

      // Reload version history to update active version
      await loadVersionHistory()

      // Notify parent component to refresh conversation data
      onVersionChange()
    } catch (err: any) {
      console.error('Failed to activate transcript version:', err)
    } finally {
      setActivating(null)
    }
  }

  const formatVersionLabel = (version: TranscriptVersion, index: number) => {
    return `v${index + 1} (${version.provider}${version.model ? ` ${version.model}` : ''})`
  }

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString()
  }

  // Human label for where speaker labels came from (descriptive metadata).
  const diarizationLabel = (source?: string | null) => {
    switch (source) {
      case 'provider':
        return 'diarized: ASR provider'
      case 'pyannote':
        return 'diarized: speaker-recognition'
      default:
        return 'no diarization'
    }
  }

  // Don't show anything unless there are multiple transcript versions
  if (!versionInfo || (versionInfo.transcript_count || 0) <= 1) {
    return null
  }

  return (
    <div className="flex items-center space-x-4 text-sm">
      {/* Transcript Version Dropdown */}
      <div className="relative">
        <button
          onClick={(e) => {
            e.stopPropagation()
            const isOpening = !showTranscriptDropdown
            setShowTranscriptDropdown(isOpening)
            // Load version history on first click
            if (isOpening && !versionHistory) {
              loadVersionHistory()
            }
          }}
          className="inline-flex items-center gap-0.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
        >
          <span>
            Transcript {versionHistory ?
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
                  onClick={() => handleActivateVersion(version.version_id)}
                  disabled={activating === version.version_id}
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
                    <div className="text-xs text-gray-500 dark:text-gray-400 mt-1 flex items-center flex-wrap gap-1">
                      <span>{formatDate(version.created_at)} • {version.segments?.length || 0} segments</span>
                      <MetadataChip className="text-[10px]">
                        {diarizationLabel(version.diarization_source)}
                      </MetadataChip>
                    </div>
                  </div>
                  {activating === version.version_id && (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  )}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {loading && (
        <div className="flex items-center space-x-1 text-gray-500 dark:text-gray-400">
          <Loader2 className="h-3 w-3 animate-spin" />
          <span className="text-xs">Loading versions...</span>
        </div>
      )}
    </div>
  )
}
