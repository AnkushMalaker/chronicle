import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Calendar, User, Trash2, RefreshCw, MoreVertical,
  RotateCcw, Zap, Play, Pause, Download, Scissors,
  Save, X, Pencil, Clock, Database, Layers, Star, BarChart3, Hash, Check
} from 'lucide-react'
import { annotationsApi, speakerApi, systemApi, BACKEND_URL } from '../services/api'
import {
  useConversationDetail,
  useDeleteConversation, useReprocessTranscript, useReprocessMemory, useReprocessSpeakers, useToggleStar
} from '../hooks/useConversations'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import MemoryAuditCard from '../components/MemoryAuditCard'
import { WaveformDisplay } from '../components/audio/WaveformDisplay'
import SpeakerNameDropdown from '../components/SpeakerNameDropdown'
import SplitConversationModal from '../components/dataAudit/SplitConversationModal'
import { getStorageKey } from '../utils/storage'

// Detect browser opus/ogg playback support once at module load
const SUPPORTS_OPUS = (() => {
  try {
    const a = document.createElement('audio')
    return a.canPlayType('audio/ogg; codecs=opus') !== ''
  } catch { return false }
})()
const AUDIO_FORMAT = SUPPORTS_OPUS ? 'opus' : 'wav'

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

  // Audio playback state — chunk-based (10s windows loaded on demand)
  const CHUNK_WINDOW = 10 // seconds per audio chunk window
  const [playingSegment, setPlayingSegment] = useState<string | null>(null)
  const [audioCurrentTime, setAudioCurrentTime] = useState<number>(0)
  const audioRefs = useRef<{ [key: string]: HTMLAudioElement }>({})
  // Exactly one audio element may play (and drive the waveform cursor) at a time.
  // `activeAudioRef` is the single source of truth for "who owns playback" — checked by
  // identity in every timeupdate/ended/async-start so stale elements can never move the
  // cursor or keep playing. `playSeqRef` supersedes in-flight loads when a newer play starts.
  const activeAudioRef = useRef<HTMLAudioElement | null>(null)
  const playSeqRef = useRef(0)
  const [activeAudioKey, setActiveAudioKey] = useState<string | null>(null)
  const [isAudioPaused, setIsAudioPaused] = useState(false)
  const handleSeekRef = useRef<(time: number, totalDuration?: number) => void>(() => {})
  // Transcript segment markers on the waveform: playing/last-played band + hover preview band
  const [segmentMarker, setSegmentMarker] = useState<{ start: number; end: number; playing: boolean } | null>(null)
  const [hoverMarker, setHoverMarker] = useState<{ start: number; end: number } | null>(null)

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

  // Pending annotation apply/clear state
  const [applyingAnnotations, setApplyingAnnotations] = useState(false)
  const [clearingAnnotations, setClearingAnnotations] = useState(false)

  const pendingDiarAnnotations = useMemo(
    () => diarizationAnnotations.filter(a => !a.processed),
    [diarizationAnnotations]
  )
  const pendingTextAnnotations = useMemo(
    () => transcriptAnnotations.filter(a => !a.processed),
    [transcriptAnnotations]
  )
  const totalPendingAnnotations = pendingDiarAnnotations.length + pendingTextAnnotations.length

  const reloadAnnotations = useCallback(async () => {
    if (!id) return
    const [diar, text] = await Promise.all([
      annotationsApi.getDiarizationAnnotations(id),
      annotationsApi.getTranscriptAnnotations(id),
    ])
    setDiarizationAnnotations(diar.data)
    setTranscriptAnnotations(text.data)
  }, [id])

  const handleApplyAnnotations = async () => {
    if (!id) return
    try {
      setApplyingAnnotations(true)
      await annotationsApi.applyAllAnnotations(id)
      await refetch()
      await reloadAnnotations()
    } catch (err: any) {
      setActionError(`Error applying annotations: ${err.message || 'Unknown error'}`)
    } finally {
      setApplyingAnnotations(false)
    }
  }

  const handleClearAnnotations = async () => {
    if (!id) return
    if (!confirm(`Discard ${totalPendingAnnotations} pending correction(s)?`)) return
    try {
      setClearingAnnotations(true)
      await Promise.all(
        [...pendingDiarAnnotations, ...pendingTextAnnotations].map(a =>
          annotationsApi.deleteAnnotation(a.id)
        )
      )
      await reloadAnnotations()
    } catch (err: any) {
      setActionError(`Error clearing annotations: ${err.message || 'Unknown error'}`)
    } finally {
      setClearingAnnotations(false)
    }
  }

  const handleDeleteAnnotation = async (annotationId: string) => {
    try {
      await annotationsApi.deleteAnnotation(annotationId)
      await reloadAnnotations()
    } catch {
      setActionError('Failed to revert annotation')
    }
  }

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

  // Stop whatever is currently playing. The ref always points at the truly-active element
  // (never stale React state), so this reliably silences the previous source before a new
  // one starts — preventing two elements from playing and fighting over the cursor.
  const stopActiveAudio = useCallback(() => {
    if (activeAudioRef.current) {
      activeAudioRef.current.pause()
      activeAudioRef.current = null
    }
  }, [])

  // Chunk-based seek handler: loads a 10s audio window on demand via waveform click
  const handleSeek = useCallback((time: number, totalDuration?: number) => {
    if (!id) return
    const windowStart = Math.floor(time / CHUNK_WINDOW) * CHUNK_WINDOW
    const cacheKey = `${id}_${windowStart}`

    // This call supersedes any earlier in-flight load; stop current audio immediately.
    const seq = ++playSeqRef.current
    stopActiveAudio()
    if (playingSegment) {
      setPlayingSegment(null)
      setSegmentMarker(prev => (prev ? { ...prev, playing: false } : prev))
    }

    // If time exceeds total duration, stop playback
    const dur = totalDuration ?? conversation?.audio_total_duration
    if (dur !== undefined && time >= dur) {
      setActiveAudioKey(null)
      setIsAudioPaused(false)
      return
    }

    // Helper to play an audio element at the right offset. Claims ownership via activeAudioRef.
    const playAt = (audio: HTMLAudioElement, absoluteTime: number) => {
      if (playSeqRef.current !== seq) return // superseded by a newer click
      const localTime = absoluteTime - windowStart
      activeAudioRef.current = audio
      const startPlaying = () => {
        if (activeAudioRef.current !== audio) return // no longer the active element
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
    fetch(`${BACKEND_URL}/api/conversations/${id}/audio-segments?start=${windowStart}&duration=${CHUNK_WINDOW}&format=${AUDIO_FORMAT}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => {
        if (!r.ok) throw new Error(`Audio fetch failed: ${r.status}`)
        return r.blob()
      })
      .then(blob => {
        if (playSeqRef.current !== seq) return // a newer click won while we were loading
        const blobUrl = URL.createObjectURL(blob)
        const audio = new Audio(blobUrl)

        // Track playback position — only the active element may move the cursor
        audio.addEventListener('timeupdate', () => {
          if (activeAudioRef.current !== audio) return
          setAudioCurrentTime(windowStart + audio.currentTime)
        })

        // Auto-load next window on end for seamless playback
        audio.addEventListener('ended', () => {
          if (activeAudioRef.current !== audio) return
          const nextStart = windowStart + CHUNK_WINDOW
          handleSeekRef.current(nextStart, dur)
        })

        // Cache it
        audioRefs.current[cacheKey] = audio
        playAt(audio, time)
      })
      .catch(err => console.warn('Failed to load audio chunk:', err))
  }, [id, playingSegment, conversation?.audio_total_duration, stopActiveAudio])

  // Keep ref in sync
  handleSeekRef.current = handleSeek

  // Toggle play/pause for the conversation's active audio
  const handleTogglePlayback = useCallback(() => {
    if (!id) return
    if (activeAudioKey && activeAudioKey.startsWith(id + '_')) {
      const audio = audioRefs.current[activeAudioKey]
      if (audio) {
        if (audio.paused) {
          // Resume — reclaim cursor ownership for this element
          activeAudioRef.current = audio
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
      stopActiveAudio()
      setPlayingSegment(null)
      // Keep the band as a faint anchor so the segment stays locatable on the waveform
      setSegmentMarker(prev => (prev ? { ...prev, playing: false } : prev))
      return
    }

    // This call supersedes any earlier in-flight load; stop current audio immediately.
    const seq = ++playSeqRef.current
    stopActiveAudio()
    setActiveAudioKey(null)
    setIsAudioPaused(false)

    let audio = audioRefs.current[segmentId]
    if (!audio || audio.error) {
      const token = localStorage.getItem(getStorageKey('token')) || ''
      const audioUrl = `${BACKEND_URL}/api/audio/chunks/${id}?start_time=${segment.start}&end_time=${segment.end}&format=${AUDIO_FORMAT}&token=${token}`
      audio = new Audio(audioUrl)
      audioRefs.current[segmentId] = audio
      // Sync the waveform playhead: segment clips start at 0, so map back to absolute time.
      // Only the active element may move the cursor.
      audio.addEventListener('timeupdate', () => {
        if (activeAudioRef.current !== audio) return
        setAudioCurrentTime(segment.start + audio.currentTime)
      })
      audio.addEventListener('ended', () => {
        if (activeAudioRef.current !== audio) return
        setPlayingSegment(null)
        setSegmentMarker(prev => (prev ? { ...prev, playing: false } : prev))
      })
    }

    if (playSeqRef.current !== seq) return // superseded before we could start
    activeAudioRef.current = audio

    audio.play().then(() => {
      // Ownership may have moved elsewhere while play() was pending
      if (activeAudioRef.current !== audio) {
        audio.pause()
        return
      }
      setPlayingSegment(segmentId)
      setSegmentMarker({ start: segment.start, end: segment.end, playing: true })
      setAudioCurrentTime(segment.start + audio.currentTime)
    }).catch(() => setPlayingSegment(null))
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
      activeAudioRef.current = null
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
    <div className="max-w-5xl mx-auto p-6 space-y-6">
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
                  segments={segments}
                  segmentMarkers={[
                    ...(segmentMarker
                      ? [{ start: segmentMarker.start, end: segmentMarker.end, kind: segmentMarker.playing ? ('playing' as const) : ('anchor' as const) }]
                      : []),
                    ...(hoverMarker ? [{ start: hoverMarker.start, end: hoverMarker.end, kind: 'hover' as const }] : []),
                  ]}
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
              {isLive && (
                <span className="inline-flex items-center gap-1.5 ml-2">
                  <span className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
                  <span className="text-xs text-red-600 dark:text-red-400 font-medium">LIVE</span>
                </span>
              )}
              {segments.length > 0 && (
                <span className="text-sm text-gray-500 dark:text-gray-400 ml-2">
                  ({segments.length} segments)
                </span>
              )}
            </h2>

            {totalPendingAnnotations > 0 && (
              <div className="flex items-center justify-between gap-3 mb-4 px-3 py-2 bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-800 rounded-lg">
                <span className="text-sm text-orange-700 dark:text-orange-300">
                  {totalPendingAnnotations} pending correction{totalPendingAnnotations === 1 ? '' : 's'}
                  {' '}({pendingDiarAnnotations.length} speaker, {pendingTextAnnotations.length} text) — not yet applied to the transcript
                </span>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    onClick={handleApplyAnnotations}
                    disabled={applyingAnnotations || clearingAnnotations}
                    className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Create a new transcript version with these corrections and reprocess memory"
                  >
                    {applyingAnnotations ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Check className="h-3.5 w-3.5" />
                    )}
                    Apply
                  </button>
                  <button
                    onClick={handleClearAnnotations}
                    disabled={applyingAnnotations || clearingAnnotations}
                    className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Discard all pending corrections"
                  >
                    {clearingAnnotations ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="h-3.5 w-3.5" />
                    )}
                    Clear
                  </button>
                </div>
              </div>
            )}

            {segments.length > 0 ? (
              <div className="space-y-1">
                {segments.map((segment, idx) => {
                  const speaker = segment.speaker || 'Unknown'
                  const speakerColor = speakerColorMap[speaker] || SPEAKER_COLOR_PALETTE[0]
                  const isEvent = segment.segment_type === 'event'
                  const isNote = segment.segment_type === 'note'
                  const isEditing = editingSegment === idx
                  const diarAnnotation = diarizationAnnotations.find(a => a.segment_index === idx && !a.processed)
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
                        onMouseEnter={isEvent ? () => setHoverMarker({ start: segment.start, end: segment.end }) : undefined}
                        onMouseLeave={isEvent ? () => setHoverMarker(null) : undefined}
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
                      onMouseEnter={() => setHoverMarker({ start: segment.start, end: segment.end })}
                      onMouseLeave={() => setHoverMarker(null)}
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

                      {/* Speaker name — pending annotations show the corrected name */}
                      <div className="flex-shrink-0 w-28 inline-flex items-start gap-1">
                        {diarAnnotation && (
                          <button
                            onClick={() => handleDeleteAnnotation(diarAnnotation.id)}
                            className="flex-shrink-0 mt-1 text-gray-400 hover:text-red-500 transition-colors"
                            title={`Revert to "${diarAnnotation.original_speaker}"`}
                          >
                            <X className="w-3 h-3" />
                          </button>
                        )}
                        <SpeakerNameDropdown
                          currentSpeaker={diarAnnotation ? diarAnnotation.corrected_speaker : speaker}
                          enrolledSpeakers={allSpeakers}
                          onSpeakerChange={(newSpeaker) => handleSpeakerChange(idx, diarAnnotation ? diarAnnotation.original_speaker : speaker, newSpeaker, segment.start)}
                          segmentIndex={idx}
                          conversationId={conversation.conversation_id}
                          annotated={!!diarAnnotation}
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
                <div ref={transcriptEndRef} />
              </div>
            ) : (
              <p className="text-sm text-gray-500 dark:text-gray-400 italic">
                {isLive ? 'Waiting for speech...' : 'No transcript segments available'}
              </p>
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

          {/* Version Info Card */}
          {(conversation.transcript_version_count || 0) > 0 && (
            <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
                Versions
              </h3>
              <dl className="space-y-3 text-sm">
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400">Transcript</dt>
                  <dd className="text-gray-900 dark:text-gray-100">
                    v{conversation.active_transcript_version_number || 1} of {conversation.transcript_version_count}
                  </dd>
                </div>
              </dl>
            </div>
          )}

          {/* Memory change history (audit ledger) */}
          <MemoryAuditCard conversationId={conversation.conversation_id} />
        </div>
      </div>

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
