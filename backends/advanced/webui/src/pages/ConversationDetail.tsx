import { useState, useRef, useMemo, useEffect } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Calendar, User, Trash2, RefreshCw, MoreVertical,
  RotateCcw, Zap, Download, Scissors,
  Save, X, Pencil, Clock, Database, Layers, Star, BarChart3, Hash, AudioLines
} from 'lucide-react'
import { annotationsApi, speakerApi, systemApi, BACKEND_URL } from '../services/api'
import {
  useConversationDetail,
  useDeleteConversation, useReprocessTranscript, useReprocessMemory, useReprocessSpeakers, useToggleStar
} from '../hooks/useConversations'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import MemoryAuditCard from '../components/MemoryAuditCard'
import ConversationContextLens from '../components/ConversationContextLens'
import { useGaplessPlayer } from '../hooks/useGaplessPlayer'
import { AUDIO_FORMAT } from '../utils/audioFormat'
import TranscriptEditor from '../components/transcript/TranscriptEditor'
import { useWaveformZoomDisabled } from '../components/transcript/useWaveformZoom'
import SplitConversationModal from '../components/dataAudit/SplitConversationModal'
import { getStorageKey } from '../utils/storage'

interface Segment {
  text: string
  speaker: string
  segment_type?: string
  start: number
  end: number
  confidence?: number
}

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  detailed_summary?: string
  created_at?: string
  client_id: string
  segment_count?: number
  audio_chunks_count?: number
  audio_total_duration?: number
  duration_seconds?: number
  transcript?: string
  segments?: Segment[]
  active_transcript_version?: string
  transcript_version_count?: number
  active_transcript_version_number?: number
  starred?: boolean
  starred_at?: string
}

export default function ConversationDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  // Pages that link here pass their own path in location.state.from so "Back"
  // returns to where the user actually came from (e.g. Data Audit).
  const backTo: string = location.state?.from || '/conversations'
  const backLabel = backTo === '/data-audit' ? 'Back to Data Audit' : 'Back to Conversations'

  const {
    data: conversationData,
    isLoading: loading,
    error: queryError,
    refetch,
  } = useConversationDetail(id ?? null)

  const conversation = conversationData as Conversation | undefined
  const isLive = conversation?.active_transcript_version === 'live-v0'

  // Auto-scroll to bottom of transcript when live segments update
  const transcriptEndRef = useRef<HTMLDivElement>(null)
  const segments = useMemo(() => conversation?.segments ?? [], [conversation?.segments])
  useEffect(() => {
    if (isLive && transcriptEndRef.current) {
      transcriptEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [segments.length, isLive])

  const error = queryError?.message ?? ((!loading && !conversation) ? 'Conversation not found' : null)

  // Dropdown menu state
  const [openDropdown, setOpenDropdown] = useState(false)
  const [waveformZoomDisabled, setWaveformZoomDisabled] = useWaveformZoomDisabled()

  // Split modal state
  const [showSplitModal, setShowSplitModal] = useState(false)

  // Langfuse observability link
  const [langfuseSessionUrl, setLangfuseSessionUrl] = useState<string | null>(null)
  useEffect(() => {
    systemApi.getObservabilityConfig().then(res => {
      const cfg = res.data?.langfuse
      if (cfg?.enabled && cfg?.session_base_url) {
        setLangfuseSessionUrl(cfg.session_base_url)
      }
    }).catch(() => {})
  }, [])

  // Reprocessing state
  const [reprocessingTranscript, setReprocessingTranscript] = useState(false)
  const [reprocessingMemory, setReprocessingMemory] = useState(false)
  const [reprocessingSpeakers, setReprocessingSpeakers] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const toggleStarMutation = useToggleStar()

  const handleToggleStar = async () => {
    if (!id) return
    try {
      await toggleStarMutation.mutateAsync(id)
    } catch (err: any) {
      setActionError(err?.response?.data?.error || 'Failed to toggle star')
    }
  }

  // Audio playback is owned by the app-wide gapless scheduler (Web Audio).
  const player = useGaplessPlayer()

  // Detailed summary expand
  const [showDetailedSummary, setShowDetailedSummary] = useState(false)

  // Title editing state
  const [editingTitle, setEditingTitle] = useState(false)
  const [editedTitle, setEditedTitle] = useState('')
  const [savingTitle, setSavingTitle] = useState(false)
  const [titleEditError, setTitleEditError] = useState<string | null>(null)

  // Diarization annotation state
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<Array<{speaker_id: string, name: string}>>([])

  // Load enrolled speakers on mount
  useEffect(() => {
    speakerApi.getEnrolledSpeakers()
      .then(res => setEnrolledSpeakers(res.data.speakers || []))
      .catch(() => {})
  }, [])


  // Close dropdown on outside click
  useEffect(() => {
    const handleClickOutside = () => setOpenDropdown(false)
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

  // Mutations
  const deleteConversationMutation = useDeleteConversation()
  const reprocessTranscriptMutation = useReprocessTranscript()
  const reprocessMemoryMutation = useReprocessMemory()
  const reprocessSpeakersMutation = useReprocessSpeakers()

  const formatDate = (timestamp: number | string) => {
    if (typeof timestamp === 'string') {
      const isoString = timestamp.endsWith('Z') || timestamp.includes('+') || (timestamp.includes('T') && timestamp.split('T')[1].includes('-'))
        ? timestamp
        : timestamp + 'Z'
      return new Date(isoString).toLocaleString()
    }
    if (timestamp === 0) return 'Unknown date'
    return new Date(timestamp * 1000).toLocaleString()
  }

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }

  // Action handlers
  const handleDownloadAudio = async () => {
    if (!id) return
    setOpenDropdown(false)
    try {
      const token = localStorage.getItem(getStorageKey('token')) || ''
      const resp = await fetch(`${BACKEND_URL}/api/audio/get_audio/${id}?format=${AUDIO_FORMAT}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) throw new Error(`Download failed: ${resp.status}`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      // Derive the extension from what the server actually returned, not from
      // the requested format — the body is ogg/opus for audio/ogg, RIFF wav
      // for audio/wav. Naming opus bytes ".wav" produces files that won't play.
      const contentType = resp.headers.get('Content-Type') || ''
      const ext = contentType.includes('ogg') ? 'ogg' : 'wav'
      a.download = `${conversation?.title || id}.${ext}`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err: any) {
      setActionError(`Failed to download audio: ${err.message || 'Unknown error'}`)
    }
  }

  const handleDelete = async () => {
    if (!id) return
    const confirmed = window.confirm('Are you sure you want to delete this conversation?')
    if (!confirmed) return
    try {
      await deleteConversationMutation.mutateAsync(id)
      navigate(backTo)
    } catch (err: any) {
      setActionError(`Failed to delete: ${err.message || 'Unknown error'}`)
    }
  }

  const handleReprocessTranscript = async () => {
    if (!id) return
    setReprocessingTranscript(true)
    setOpenDropdown(false)
    try {
      await reprocessTranscriptMutation.mutateAsync(id)
      refetch()
    } catch (err: any) {
      setActionError(`Failed to reprocess transcript: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingTranscript(false)
    }
  }

  const handleReprocessMemory = async () => {
    if (!id) return
    setReprocessingMemory(true)
    setOpenDropdown(false)
    try {
      await reprocessMemoryMutation.mutateAsync({ conversationId: id })
      refetch()
    } catch (err: any) {
      setActionError(`Failed to reprocess memory: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingMemory(false)
    }
  }

  const handleReprocessSpeakers = async () => {
    if (!id) return
    setReprocessingSpeakers(true)
    setOpenDropdown(false)
    try {
      await reprocessSpeakersMutation.mutateAsync({ conversationId: id, transcriptVersionId: 'active' })
      refetch()
    } catch (err: any) {
      setActionError(`Failed to reprocess speakers: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingSpeakers(false)
    }
  }

  // Title editing
  const handleStartTitleEdit = () => {
    if (conversation) {
      setEditedTitle(conversation.title || 'Conversation')
      setEditingTitle(true)
      setTitleEditError(null)
    }
  }

  const handleSaveTitleEdit = async () => {
    if (!id || !conversation) return
    const originalTitle = conversation.title || 'Conversation'
    if (!editedTitle.trim()) {
      setTitleEditError('Title cannot be empty')
      return
    }
    if (editedTitle === originalTitle) {
      setEditingTitle(false)
      return
    }
    try {
      setSavingTitle(true)
      setTitleEditError(null)
      await annotationsApi.createTitleAnnotation({
        conversation_id: id,
        original_text: originalTitle,
        corrected_text: editedTitle.trim(),
      })
      // Optimistic update
      queryClient.setQueryData(['conversation', id], {
        ...conversation,
        title: editedTitle.trim(),
      })
      // Also invalidate conversations list cache
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
      setEditingTitle(false)
      setEditedTitle('')
    } catch (err: any) {
      setTitleEditError(err.response?.data?.detail || err.message || 'Failed to save title')
    } finally {
      setSavingTitle(false)
    }
  }

  const handleCancelTitleEdit = () => {
    setEditingTitle(false)
    setEditedTitle('')
    setTitleEditError(null)
  }

  const handleTitleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleSaveTitleEdit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      handleCancelTitleEdit()
    }
  }


  // Stop playback when leaving the page.
  useEffect(() => {
    return () => {
      if (id && player.isActive(id)) player.stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])


  if (loading) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <button
          onClick={() => navigate(backTo)}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          {backLabel}
        </button>
        <div className="flex items-center justify-center py-12">
          <RefreshCw className="w-6 h-6 animate-spin text-blue-600" />
          <span className="ml-3 text-gray-600 dark:text-gray-400">Loading conversation...</span>
        </div>
      </div>
    )
  }

  if (error || !conversation) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <button
          onClick={() => navigate(backTo)}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          {backLabel}
        </button>
        <div className="border border-red-200 dark:border-red-800 rounded-lg p-8 text-center bg-red-50 dark:bg-red-900/20">
          <p className="text-red-600 dark:text-red-400">{error || 'Conversation not found'}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-7xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button
          onClick={() => navigate(backTo)}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
          {backLabel}
        </button>

        <div className="flex items-center space-x-1">
          {langfuseSessionUrl && conversation.conversation_id && (
            <a
              href={`${langfuseSessionUrl}/${conversation.conversation_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="p-2 rounded-full hover:bg-blue-100 dark:hover:bg-blue-900/30 transition-colors"
              title="View traces in Langfuse"
            >
              <BarChart3 className="h-5 w-5 text-gray-400 dark:text-gray-500 hover:text-blue-500 dark:hover:text-blue-400" />
            </a>
          )}
          <button
            onClick={handleToggleStar}
            className="p-2 rounded-full hover:bg-yellow-100 dark:hover:bg-yellow-900/30 transition-colors"
            title={conversation.starred ? 'Unstar conversation' : 'Star conversation'}
          >
            <Star className={`h-5 w-5 ${conversation.starred ? 'fill-yellow-400 text-yellow-400' : 'text-gray-400 dark:text-gray-500'}`} />
          </button>
        <div className="relative">
          <button
            onClick={(e) => {
              e.stopPropagation()
              setOpenDropdown(!openDropdown)
            }}
            className="p-2 rounded-full hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
            title="Actions"
          >
            <MoreVertical className="h-5 w-5 text-gray-500 dark:text-gray-400" />
          </button>

          {openDropdown && (
            <div className="absolute right-0 top-10 w-52 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-600 py-2 z-10">
              <button
                onClick={handleReprocessTranscript}
                disabled={reprocessingTranscript}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50"
              >
                {reprocessingTranscript ? <RefreshCw className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                <span>Reprocess Transcript</span>
              </button>
              <button
                onClick={handleReprocessMemory}
                disabled={reprocessingMemory}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50"
              >
                {reprocessingMemory ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
                <span>Reprocess Memory</span>
              </button>
              <button
                onClick={handleReprocessSpeakers}
                disabled={reprocessingSpeakers}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50"
                title="Re-identify speakers"
              >
                {reprocessingSpeakers ? <RefreshCw className="h-4 w-4 animate-spin" /> : <User className="h-4 w-4" />}
                <span>Reprocess Speakers</span>
              </button>
              <button
                onClick={() => setWaveformZoomDisabled(!waveformZoomDisabled)}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2"
                title="When on, editing a segment auto-zooms the waveform so you can adjust its timing"
              >
                <AudioLines className="h-4 w-4" />
                <span>{waveformZoomDisabled ? 'Enable waveform zoom' : 'Disable waveform zoom'}</span>
              </button>
              {conversation.audio_chunks_count && conversation.audio_chunks_count > 0 && (
                <>
                  <button
                    onClick={handleDownloadAudio}
                    className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2"
                  >
                    <Download className="h-4 w-4" />
                    <span>Download Audio</span>
                  </button>
                  <button
                    onClick={() => {
                      setOpenDropdown(false)
                      setShowSplitModal(true)
                    }}
                    className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2"
                    title="Split this conversation at long silence gaps"
                  >
                    <Scissors className="h-4 w-4" />
                    <span>Split Conversation…</span>
                  </button>
                </>
              )}
              <div className="border-t border-gray-200 dark:border-gray-600 my-1"></div>
              <button
                onClick={handleDelete}
                className="w-full text-left px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 flex items-center space-x-2"
              >
                <Trash2 className="h-4 w-4" />
                <span>Delete Conversation</span>
              </button>
            </div>
          )}
        </div>
        </div>
      </div>

      {/* Action Error Banner */}
      {actionError && (
        <div className="p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-sm text-red-600 dark:text-red-400 flex justify-between items-center">
          <span>{actionError}</span>
          <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-600">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Version Selector */}
      <ConversationVersionHeader
        conversationId={conversation.conversation_id}
        versionInfo={{
          transcript_count: conversation.transcript_version_count || 0,
          active_transcript_version: conversation.active_transcript_version,
          active_transcript_version_number: conversation.active_transcript_version_number,
        }}
        onVersionChange={() => {
          refetch()
        }}
      />

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Left Column - Main Content */}
        <div className="lg:col-span-3 space-y-6">
          {/* Title */}
          <div id="transcript" className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6 scroll-mt-6">
            {editingTitle ? (
              <div className="space-y-2">
                <div className="flex items-center space-x-2">
                  <input
                    type="text"
                    value={editedTitle}
                    onChange={(e) => setEditedTitle(e.target.value)}
                    onKeyDown={handleTitleKeyDown}
                    className="text-2xl font-bold px-2 py-1 border-2 border-blue-500 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 flex-1"
                    autoFocus
                    disabled={savingTitle}
                  />
                  <button
                    onClick={handleSaveTitleEdit}
                    disabled={savingTitle || editedTitle === (conversation.title || 'Conversation')}
                    className="inline-flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    <Save className="w-3.5 h-3.5" />
                    {savingTitle ? 'Saving...' : 'Save'}
                  </button>
                  <button
                    onClick={handleCancelTitleEdit}
                    disabled={savingTitle}
                    className="inline-flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-200 dark:bg-gray-600 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-500 disabled:opacity-50 transition-colors"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
                {titleEditError && (
                  <span className="text-xs text-red-600 dark:text-red-400">{titleEditError}</span>
                )}
              </div>
            ) : (
              <h1
                className="text-2xl font-bold text-gray-900 dark:text-gray-100 group cursor-pointer hover:bg-yellow-100 dark:hover:bg-yellow-900/30 px-1 rounded transition-colors inline-flex items-center gap-2"
                onClick={handleStartTitleEdit}
                title="Click to edit title"
              >
                {conversation.title || 'Conversation'}
                <Pencil className="w-4 h-4 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity" />
              </h1>
            )}

            {/* Summary */}
            {conversation.summary && (
              <p className="mt-3 text-gray-600 dark:text-gray-400 italic">
                {conversation.summary}
              </p>
            )}

            {/* Detailed Summary */}
            {conversation.detailed_summary && (
              <div className="mt-3">
                <button
                  onClick={() => setShowDetailedSummary(!showDetailedSummary)}
                  className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center space-x-1"
                >
                  <span>{showDetailedSummary ? '\u25BC' : '\u25B6'} Detailed Summary</span>
                </button>
                {showDetailedSummary && (
                  <div className="mt-2 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800">
                    <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                      {conversation.detailed_summary}
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Audio + transcript — one shared editor. The waveform doubles as the
              timing editor (auto-zooms when you edit a segment). */}
          <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-medium text-gray-900 dark:text-gray-100">
                Transcript
                {isLive && (
                  <span className="inline-flex items-center gap-1.5 ml-2">
                    <span className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
                    <span className="text-xs text-red-600 dark:text-red-400 font-medium">LIVE</span>
                  </span>
                )}
                {segments.length > 0 && (
                  <span className="text-sm text-gray-500 dark:text-gray-400 ml-2">({segments.length} segments)</span>
                )}
              </h2>
            </div>
            <TranscriptEditor
              conversationId={conversation.conversation_id!}
              segments={segments}
              duration={conversation.audio_total_duration}
              hasAudio={!!conversation.audio_chunks_count && conversation.audio_chunks_count > 0}
              showWaveform
              isLive={isLive}
              enrolledSpeakers={enrolledSpeakers}
              onChanged={refetch}
            />
          </div>

        </div>

        {/* Right Column - Sidebar */}
        <div className="space-y-6 lg:sticky lg:top-6 lg:self-start">
          {/* Metadata Card */}
          <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
              Metadata
            </h3>
            <dl className="space-y-3 text-sm">
              <div className="flex justify-between items-start gap-2">
                <dt className="text-gray-600 dark:text-gray-400 flex items-center gap-1.5 shrink-0">
                  <Hash className="w-3.5 h-3.5" /> ID
                </dt>
                <dd className="text-gray-900 dark:text-gray-100 text-right font-mono text-xs break-all">
                  {conversation.conversation_id}
                </dd>
              </div>
              <div className="flex justify-between items-start">
                <dt className="text-gray-600 dark:text-gray-400 flex items-center gap-1.5">
                  <Calendar className="w-3.5 h-3.5" /> Date
                </dt>
                <dd className="text-gray-900 dark:text-gray-100 text-right">
                  {formatDate(conversation.created_at || '')}
                </dd>
              </div>
              <div className="flex justify-between items-start">
                <dt className="text-gray-600 dark:text-gray-400 flex items-center gap-1.5">
                  <User className="w-3.5 h-3.5" /> Client
                </dt>
                <dd className="text-gray-900 dark:text-gray-100 text-right font-mono text-xs">
                  {conversation.client_id}
                </dd>
              </div>
              {conversation.duration_seconds && conversation.duration_seconds > 0 && (
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400 flex items-center gap-1.5">
                    <Clock className="w-3.5 h-3.5" /> Duration
                  </dt>
                  <dd className="text-gray-900 dark:text-gray-100">
                    {formatDuration(conversation.duration_seconds)}
                  </dd>
                </div>
              )}
              {(conversation.segment_count || segments.length > 0) && (
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400 flex items-center gap-1.5">
                    <Layers className="w-3.5 h-3.5" /> Segments
                  </dt>
                  <dd className="text-gray-900 dark:text-gray-100">
                    {segments.length || conversation.segment_count}
                  </dd>
                </div>
              )}
              {conversation.audio_chunks_count && conversation.audio_chunks_count > 0 && (
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400 flex items-center gap-1.5">
                    <Database className="w-3.5 h-3.5" /> Audio Chunks
                  </dt>
                  <dd className="text-gray-900 dark:text-gray-100">
                    {conversation.audio_chunks_count}
                  </dd>
                </div>
              )}
            </dl>
          </div>

          <a
            href="#memory-history"
            className="flex w-full items-center justify-between rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm font-medium text-blue-700 transition-colors hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-300 dark:hover:bg-blue-900/35"
          >
            <span>Memory history</span>
            <span aria-hidden="true">↓</span>
          </a>

        </div>
      </div>

      {/* Memory change history is intentionally full-width: paths, summaries, and
          timestamps become unreadable in the narrow metadata rail. */}
      <ConversationContextLens conversationId={conversation.conversation_id} />
      <MemoryAuditCard conversationId={conversation.conversation_id} />

      {/* Split modal — on success the conversation is soft-deleted, so leave */}
      {showSplitModal && (
        <SplitConversationModal
          conversation={{
            conversation_id: conversation.conversation_id,
            title: conversation.title || null,
            duration_seconds: conversation.audio_total_duration || conversation.duration_seconds || 0,
          }}
          onClose={() => setShowSplitModal(false)}
          onDone={() => navigate(backTo)}
        />
      )}
    </div>
  )
}
