import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Calendar, User, Trash2, RefreshCw, MoreVertical,
  RotateCcw, Zap, Play, Pause, Download,
  Save, X, Pencil, Brain, Clock, Database, Layers, Star, BarChart3
} from 'lucide-react'
import { annotationsApi, speakerApi, systemApi, BACKEND_URL } from '../services/api'
import {
  useConversationDetail, useConversationMemories,
  useDeleteConversation, useReprocessTranscript, useReprocessMemory, useReprocessSpeakers, useToggleStar
} from '../hooks/useConversations'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import { WaveformDisplay } from '../components/audio/WaveformDisplay'
import SpeakerNameDropdown from '../components/SpeakerNameDropdown'
import { getStorageKey } from '../utils/storage'

const SPEAKER_COLOR_PALETTE = [
  'text-blue-600 dark:text-blue-400',
  'text-green-600 dark:text-green-400',
  'text-purple-600 dark:text-purple-400',
  'text-orange-600 dark:text-orange-400',
  'text-pink-600 dark:text-pink-400',
  'text-indigo-600 dark:text-indigo-400',
  'text-red-600 dark:text-red-400',
  'text-yellow-600 dark:text-yellow-400',
  'text-teal-600 dark:text-teal-400',
  'text-cyan-600 dark:text-cyan-400',
]

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
  memory_count?: number
  audio_chunks_count?: number
  audio_total_duration?: number
  duration_seconds?: number
  has_memory?: boolean
  transcript?: string
  segments?: Segment[]
  active_transcript_version?: string
  active_memory_version?: string
  transcript_version_count?: number
  memory_version_count?: number
  active_transcript_version_number?: number
  active_memory_version_number?: number
  starred?: boolean
  starred_at?: string
}

export default function ConversationDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const {
    data: conversationData,
    isLoading: loading,
    error: queryError,
    refetch,
  } = useConversationDetail(id ?? null)

  const conversation = conversationData as Conversation | undefined

  const {
    data: memoriesData,
  } = useConversationMemories(id ?? null)

  const memories = (memoriesData as any)?.memories ?? []

  const error = queryError?.message ?? ((!loading && !conversation) ? 'Conversation not found' : null)

  // Dropdown menu state
  const [openDropdown, setOpenDropdown] = useState(false)

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

  // Audio playback state — chunk-based (10s windows loaded on demand)
  const CHUNK_WINDOW = 10 // seconds per audio chunk window
  const [playingSegment, setPlayingSegment] = useState<string | null>(null)
  const [audioCurrentTime, setAudioCurrentTime] = useState<number>(0)
  const audioRefs = useRef<{ [key: string]: HTMLAudioElement }>({})
  const [activeAudioKey, setActiveAudioKey] = useState<string | null>(null)
  const [isAudioPaused, setIsAudioPaused] = useState(false)
  const handleSeekRef = useRef<(time: number, totalDuration?: number) => void>(() => {})

  // Detailed summary expand
  const [showDetailedSummary, setShowDetailedSummary] = useState(false)

  // Title editing state
  const [editingTitle, setEditingTitle] = useState(false)
  const [editedTitle, setEditedTitle] = useState('')
  const [savingTitle, setSavingTitle] = useState(false)
  const [titleEditError, setTitleEditError] = useState<string | null>(null)

  // Diarization annotation state
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<Array<{speaker_id: string, name: string}>>([])
  const [diarizationAnnotations, setDiarizationAnnotations] = useState<any[]>([])

  // Track recently selected speakers in this session (most recent first)
  const [recentSpeakers, setRecentSpeakers] = useState<string[]>([])

  // Transcript segment editing state
  const [editingSegment, setEditingSegment] = useState<number | null>(null)
  const [editedSegmentText, setEditedSegmentText] = useState('')
  const [savingSegment, setSavingSegment] = useState(false)
  const [segmentEditError, setSegmentEditError] = useState<string | null>(null)
  const [transcriptAnnotations, setTranscriptAnnotations] = useState<any[]>([])

  // Load enrolled speakers on mount
  useEffect(() => {
    speakerApi.getEnrolledSpeakers()
      .then(res => setEnrolledSpeakers(res.data.speakers || []))
      .catch(() => {})
  }, [])

  // Load annotations when conversation loads
  useEffect(() => {
    if (!id) return
    annotationsApi.getDiarizationAnnotations(id)
      .then(res => setDiarizationAnnotations(res.data))
      .catch(() => {})
    annotationsApi.getTranscriptAnnotations(id)
      .then(res => setTranscriptAnnotations(res.data))
      .catch(() => {})
  }, [id, conversation])

  // Compute merged speaker list including annotation names
  const allSpeakers = useMemo(() => {
    const speakers = [...enrolledSpeakers]
    const existingNames = new Set(speakers.map(s => s.name))
    diarizationAnnotations.forEach(a => {
      if (a.corrected_speaker && !existingNames.has(a.corrected_speaker)) {
        speakers.push({ speaker_id: `annotation_${a.corrected_speaker}`, name: a.corrected_speaker })
        existingNames.add(a.corrected_speaker)
      }
    })
    return speakers
  }, [enrolledSpeakers, diarizationAnnotations])

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

  // Chunk-based seek handler: loads a 10s audio window on demand via waveform click
  const handleSeek = useCallback((time: number, totalDuration?: number) => {
    if (!id) return
    const windowStart = Math.floor(time / CHUNK_WINDOW) * CHUNK_WINDOW
    const cacheKey = `${id}_${windowStart}`

    // If time exceeds total duration, stop playback
    const dur = totalDuration ?? conversation?.audio_total_duration
    if (dur !== undefined && time >= dur) {
      setActiveAudioKey(null)
      setIsAudioPaused(false)
      return
    }

    // Pause any currently playing audio
    if (activeAudioKey && audioRefs.current[activeAudioKey]) {
      audioRefs.current[activeAudioKey].pause()
    }

    // Helper to play an audio element at the right offset
    const playAt = (audio: HTMLAudioElement, absoluteTime: number) => {
      const localTime = absoluteTime - windowStart
      const startPlaying = () => {
        audio.currentTime = Math.max(0, localTime)
        audio.play().catch(err => console.warn('Playback failed:', err))
      }
      if (audio.readyState >= 2) {
        startPlaying()
      } else {
        audio.addEventListener('canplay', startPlaying, { once: true })
      }
      setActiveAudioKey(cacheKey)
      setIsAudioPaused(false)
    }

    // Check cache first
    if (audioRefs.current[cacheKey]) {
      playAt(audioRefs.current[cacheKey], time)
      return
    }

    // Fetch audio chunk via authenticated API
    const token = localStorage.getItem(getStorageKey('token')) || ''
    fetch(`${BACKEND_URL}/api/conversations/${id}/audio-segments?start=${windowStart}&duration=${CHUNK_WINDOW}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => {
        if (!r.ok) throw new Error(`Audio fetch failed: ${r.status}`)
        return r.blob()
      })
      .then(blob => {
        const blobUrl = URL.createObjectURL(blob)
        const audio = new Audio(blobUrl)

        // Track playback position
        audio.addEventListener('timeupdate', () => {
          setAudioCurrentTime(windowStart + audio.currentTime)
        })

        // Auto-load next window on end for seamless playback
        audio.addEventListener('ended', () => {
          const nextStart = windowStart + CHUNK_WINDOW
          handleSeekRef.current(nextStart, dur)
        })

        // Cache it
        audioRefs.current[cacheKey] = audio
        playAt(audio, time)
      })
      .catch(err => console.warn('Failed to load audio chunk:', err))
  }, [id, activeAudioKey, conversation?.audio_total_duration])

  // Keep ref in sync
  handleSeekRef.current = handleSeek

  // Toggle play/pause for the conversation's active audio
  const handleTogglePlayback = useCallback(() => {
    if (!id) return
    if (activeAudioKey && activeAudioKey.startsWith(id + '_')) {
      const audio = audioRefs.current[activeAudioKey]
      if (audio) {
        if (audio.paused) {
          audio.play().catch(err => console.warn('Resume failed:', err))
          setIsAudioPaused(false)
        } else {
          audio.pause()
          setIsAudioPaused(true)
        }
        return
      }
    }
    // No active audio — start from beginning
    handleSeek(0, conversation?.audio_total_duration)
  }, [id, activeAudioKey, handleSeek, conversation?.audio_total_duration])

  // Segment play/pause
  const handleSegmentPlayPause = (segmentIndex: number, segment: Segment) => {
    if (!id) return
    const segmentId = `${id}-${segmentIndex}`

    if (playingSegment === segmentId) {
      const audio = audioRefs.current[segmentId]
      if (audio) audio.pause()
      setPlayingSegment(null)
      return
    }

    if (playingSegment) {
      const currentAudio = audioRefs.current[playingSegment]
      if (currentAudio) currentAudio.pause()
    }

    let audio = audioRefs.current[segmentId]
    if (!audio || audio.error) {
      const token = localStorage.getItem(getStorageKey('token')) || ''
      const audioUrl = `${BACKEND_URL}/api/audio/chunks/${id}?start_time=${segment.start}&end_time=${segment.end}&token=${token}`
      audio = new Audio(audioUrl)
      audioRefs.current[segmentId] = audio
      audio.addEventListener('ended', () => setPlayingSegment(null))
    }

    audio.play().then(() => setPlayingSegment(segmentId)).catch(() => setPlayingSegment(null))
  }

  // Action handlers
  const handleDownloadAudio = async () => {
    if (!id) return
    setOpenDropdown(false)
    try {
      const token = localStorage.getItem(getStorageKey('token')) || ''
      const resp = await fetch(`${BACKEND_URL}/api/audio/get_audio/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) throw new Error(`Download failed: ${resp.status}`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${conversation?.title || id}.wav`
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
      navigate('/conversations')
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

  // Speaker change handler
  const handleSpeakerChange = async (segmentIndex: number, originalSpeaker: string, newSpeaker: string, segmentStartTime: number) => {
    if (!id) return
    try {
      const existingAnnotation = diarizationAnnotations.find(
        a => a.segment_index === segmentIndex && !a.processed
      )
      if (existingAnnotation) {
        await annotationsApi.updateAnnotation(existingAnnotation.id, { corrected_speaker: newSpeaker })
      } else {
        await annotationsApi.createDiarizationAnnotation({
          conversation_id: id,
          segment_index: segmentIndex,
          original_speaker: originalSpeaker,
          corrected_speaker: newSpeaker,
          segment_start_time: segmentStartTime,
        })
      }
      setEnrolledSpeakers(prev => {
        if (newSpeaker === 'Unknown Speaker') return prev
        if (prev.some(s => s.name === newSpeaker)) return prev
        return [...prev, { speaker_id: `temp_${Date.now()}_${newSpeaker}`, name: newSpeaker }]
      })
      // Track as recently used speaker (move to front)
      setRecentSpeakers(prev => [newSpeaker, ...prev.filter(s => s !== newSpeaker)])
      const res = await annotationsApi.getDiarizationAnnotations(id)
      setDiarizationAnnotations(res.data)
    } catch (err: any) {
      setActionError('Failed to create speaker annotation')
    }
  }

  // Segment editing handlers
  const handleStartSegmentEdit = (segmentIndex: number, originalText: string) => {
    setEditingSegment(segmentIndex)
    setEditedSegmentText(originalText)
    setSegmentEditError(null)
  }

  const handleSaveSegmentEdit = async (segmentIndex: number, originalText: string) => {
    if (!id || !editedSegmentText.trim()) {
      setSegmentEditError('Segment text cannot be empty')
      return
    }
    if (editedSegmentText === originalText) {
      setEditingSegment(null)
      return
    }
    try {
      setSavingSegment(true)
      setSegmentEditError(null)
      const existing = transcriptAnnotations.find(a => a.segment_index === segmentIndex && !a.processed)
      if (existing) {
        await annotationsApi.updateAnnotation(existing.id, { corrected_text: editedSegmentText })
      } else {
        await annotationsApi.createTranscriptAnnotation({
          conversation_id: id,
          segment_index: segmentIndex,
          original_text: originalText,
          corrected_text: editedSegmentText,
        })
      }
      setEditingSegment(null)
      setEditedSegmentText('')
      const res = await annotationsApi.getTranscriptAnnotations(id)
      setTranscriptAnnotations(res.data)
    } catch (err: any) {
      setSegmentEditError(err.response?.data?.detail || err.message || 'Failed to save')
    } finally {
      setSavingSegment(false)
    }
  }

  const handleSegmentKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>, segmentIndex: number, originalText: string) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSaveSegmentEdit(segmentIndex, originalText)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setEditingSegment(null)
    }
  }

  // Cleanup audio on unmount
  useEffect(() => {
    return () => {
      Object.values(audioRefs.current).forEach(audio => audio.pause())
    }
  }, [])

  // Build speaker color map
  const speakerColorMap = useMemo(() => {
    const map: { [key: string]: string } = {}
    let colorIndex = 0
    conversation?.segments?.forEach(segment => {
      const speaker = segment.speaker || 'Unknown'
      if (!map[speaker]) {
        map[speaker] = SPEAKER_COLOR_PALETTE[colorIndex % SPEAKER_COLOR_PALETTE.length]
        colorIndex++
      }
    })
    return map
  }, [conversation?.segments])

  if (loading) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <button
          onClick={() => navigate('/conversations')}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          Back to Conversations
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
          onClick={() => navigate('/conversations')}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          Back to Conversations
        </button>
        <div className="border border-red-200 dark:border-red-800 rounded-lg p-8 text-center bg-red-50 dark:bg-red-900/20">
          <p className="text-red-600 dark:text-red-400">{error || 'Conversation not found'}</p>
        </div>
      </div>
    )
  }

  const segments = conversation.segments || []

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button
          onClick={() => navigate('/conversations')}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
          Back to Conversations
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
              {conversation.audio_chunks_count && conversation.audio_chunks_count > 0 && (
                <button
                  onClick={handleDownloadAudio}
                  className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2"
                >
                  <Download className="h-4 w-4" />
                  <span>Download Audio</span>
                </button>
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
          memory_count: conversation.memory_version_count || 0,
          active_transcript_version: conversation.active_transcript_version,
          active_memory_version: conversation.active_memory_version,
          active_transcript_version_number: conversation.active_transcript_version_number,
          active_memory_version_number: conversation.active_memory_version_number,
        }}
        onVersionChange={() => {
          refetch()
          queryClient.invalidateQueries({ queryKey: ['conversationMemories', id] })
        }}
      />

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column - Main Content */}
        <div className="lg:col-span-2 space-y-6">
          {/* Title */}
          <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
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

          {/* Audio Player — chunk-based (no full WAV download) */}
          {conversation.audio_chunks_count && conversation.audio_chunks_count > 0 && (
            <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
              <h2 className="font-medium text-gray-900 dark:text-gray-100 mb-3">Audio</h2>

              {conversation.conversation_id && conversation.audio_total_duration && (
                <WaveformDisplay
                  conversationId={conversation.conversation_id}
                  duration={conversation.audio_total_duration}
                  currentTime={audioCurrentTime}
                  onSeek={handleSeek}
                  height={80}
                />
              )}

              {/* Play/pause + time display */}
              <div className="flex items-center gap-3 mt-2">
                <button
                  onClick={handleTogglePlayback}
                  className="p-2 rounded-full bg-blue-600 hover:bg-blue-700 text-white transition-colors"
                  title={activeAudioKey && !isAudioPaused ? 'Pause' : 'Play'}
                >
                  {activeAudioKey && !isAudioPaused
                    ? <Pause className="w-4 h-4" />
                    : <Play className="w-4 h-4" />}
                </button>
                <span className="text-sm text-gray-600 dark:text-gray-400 font-mono">
                  {formatDuration(audioCurrentTime)}
                  {conversation.audio_total_duration ? ` / ${formatDuration(conversation.audio_total_duration)}` : ''}
                </span>
              </div>
            </div>
          )}

          {/* Transcript */}
          <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
            <h2 className="font-medium text-gray-900 dark:text-gray-100 mb-4">
              Transcript
              {segments.length > 0 && (
                <span className="text-sm text-gray-500 dark:text-gray-400 ml-2">
                  ({segments.length} segments)
                </span>
              )}
            </h2>

            {segments.length > 0 ? (
              <div className="space-y-1">
                {segments.map((segment, idx) => {
                  const speaker = segment.speaker || 'Unknown'
                  const speakerColor = speakerColorMap[speaker] || SPEAKER_COLOR_PALETTE[0]
                  const isEvent = segment.segment_type === 'event'
                  const isNote = segment.segment_type === 'note'
                  const isEditing = editingSegment === idx
                  const hasDiarAnnotation = diarizationAnnotations.some(a => a.segment_index === idx && !a.processed)
                  const hasTextAnnotation = transcriptAnnotations.some(a => a.segment_index === idx && !a.processed)

                  if (isEvent || isNote) {
                    return (
                      <div
                        key={idx}
                        className={`group flex items-center gap-2 py-1 px-3 rounded ${
                          isEvent
                            ? 'bg-yellow-50 dark:bg-yellow-900/20 border-l-2 border-yellow-400'
                            : 'bg-green-50 dark:bg-green-900/20 border-l-2 border-green-400'
                        }`}
                      >
                        {isEvent && (
                          <button
                            onClick={() => handleSegmentPlayPause(idx, segment)}
                            className="flex-shrink-0 p-0.5 rounded hover:bg-yellow-200 dark:hover:bg-yellow-800 opacity-0 group-hover:opacity-100 transition-opacity"
                            title={`Play ${formatDuration(segment.end - segment.start)}s`}
                          >
                            {playingSegment === `${id}-${idx}` ? (
                              <Pause className="h-3 w-3 text-yellow-600" />
                            ) : (
                              <Play className="h-3 w-3 text-yellow-600" />
                            )}
                          </button>
                        )}
                        <span className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mr-2">
                          {isEvent ? 'event' : 'note'}
                        </span>
                        <span className="text-sm text-gray-700 dark:text-gray-300 italic">
                          {segment.text}
                        </span>
                      </div>
                    )
                  }

                  return (
                    <div
                      key={idx}
                      className={`group flex items-start gap-2 py-1 rounded hover:bg-gray-50 dark:hover:bg-gray-700/50 ${
                        hasTextAnnotation ? 'bg-yellow-50 dark:bg-yellow-900/10' : ''
                      }`}
                    >
                      {/* Play button */}
                      <button
                        onClick={() => handleSegmentPlayPause(idx, segment)}
                        className="flex-shrink-0 mt-0.5 p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-600 opacity-0 group-hover:opacity-100 transition-opacity"
                        title={`Play ${formatDuration(segment.end - segment.start)}s`}
                      >
                        {playingSegment === `${id}-${idx}` ? (
                          <Pause className="h-3 w-3 text-blue-600" />
                        ) : (
                          <Play className="h-3 w-3 text-gray-500" />
                        )}
                      </button>

                      {/* Speaker name */}
                      <div className="flex-shrink-0 w-28">
                        <SpeakerNameDropdown
                          currentSpeaker={speaker}
                          enrolledSpeakers={allSpeakers}
                          onSpeakerChange={(newSpeaker) => handleSpeakerChange(idx, speaker, newSpeaker, segment.start)}
                          segmentIndex={idx}
                          conversationId={conversation.conversation_id}
                          annotated={hasDiarAnnotation}
                          speakerColor={speakerColor}
                          recentSpeakers={recentSpeakers}
                        />
                      </div>

                      {/* Segment text */}
                      <div className="flex-1 min-w-0">
                        {isEditing ? (
                          <div className="space-y-1">
                            <textarea
                              value={editedSegmentText}
                              onChange={(e) => setEditedSegmentText(e.target.value)}
                              onKeyDown={(e) => handleSegmentKeyDown(e, idx, segment.text)}
                              className="w-full px-2 py-1 text-sm border-2 border-blue-500 rounded focus:outline-none focus:ring-1 focus:ring-blue-500 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 resize-y"
                              autoFocus
                              disabled={savingSegment}
                              rows={2}
                            />
                            <div className="flex items-center gap-1">
                              <button
                                onClick={() => handleSaveSegmentEdit(idx, segment.text)}
                                disabled={savingSegment}
                                className="px-2 py-0.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                              >
                                {savingSegment ? 'Saving...' : 'Save'}
                              </button>
                              <button
                                onClick={() => setEditingSegment(null)}
                                className="px-2 py-0.5 text-xs bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-300 rounded hover:bg-gray-300"
                              >
                                Cancel
                              </button>
                              {segmentEditError && (
                                <span className="text-xs text-red-500">{segmentEditError}</span>
                              )}
                            </div>
                          </div>
                        ) : (
                          <p
                            className="text-sm text-gray-700 dark:text-gray-300 cursor-pointer hover:bg-yellow-50 dark:hover:bg-yellow-900/10 rounded px-1 transition-colors"
                            onClick={() => handleStartSegmentEdit(idx, segment.text)}
                            title="Click to edit"
                          >
                            {segment.text}
                          </p>
                        )}
                      </div>

                      {/* Timestamp */}
                      <span className="flex-shrink-0 text-xs text-gray-400 mt-0.5">
                        {formatDuration(segment.start)}
                      </span>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-sm text-gray-500 dark:text-gray-400 italic">No transcript segments available</p>
            )}
          </div>
        </div>

        {/* Right Column - Sidebar */}
        <div className="space-y-6">
          {/* Metadata Card */}
          <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
              Metadata
            </h3>
            <dl className="space-y-3 text-sm">
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

          {/* Extracted Memories Card */}
          <div className="bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3 flex items-center gap-2">
              <Brain className="w-4 h-4" />
              Memories ({memories.length})
            </h3>
            {memories.length > 0 ? (
              <div className="space-y-2">
                {memories.map((mem: any) => (
                  <div
                    key={mem.id}
                    onClick={() => navigate(`/memories/${mem.id}`)}
                    className="p-2 bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-600 cursor-pointer hover:border-purple-300 dark:hover:border-purple-600 transition-colors"
                  >
                    <p className="text-xs text-gray-700 dark:text-gray-300 line-clamp-2">
                      {mem.memory || mem.content}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-400 italic">No memories extracted yet</p>
            )}
          </div>

          {/* Version Info Card */}
          {((conversation.transcript_version_count || 0) > 0 || (conversation.memory_version_count || 0) > 0) && (
            <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
                Versions
              </h3>
              <dl className="space-y-3 text-sm">
                {(conversation.transcript_version_count || 0) > 0 && (
                  <div className="flex justify-between items-start">
                    <dt className="text-gray-600 dark:text-gray-400">Transcript</dt>
                    <dd className="text-gray-900 dark:text-gray-100">
                      v{conversation.active_transcript_version_number || 1} of {conversation.transcript_version_count}
                    </dd>
                  </div>
                )}
                {(conversation.memory_version_count || 0) > 0 && (
                  <div className="flex justify-between items-start">
                    <dt className="text-gray-600 dark:text-gray-400">Memory</dt>
                    <dd className="text-gray-900 dark:text-gray-100">
                      v{conversation.active_memory_version_number || 1} of {conversation.memory_version_count}
                    </dd>
                  </div>
                )}
              </dl>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
