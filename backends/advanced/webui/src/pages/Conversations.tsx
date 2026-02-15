import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { MessageSquare, RefreshCw, Calendar, User, Play, Pause, MoreVertical, RotateCcw, Zap, ChevronDown, ChevronUp, Trash2, Save, X, Check, AlertTriangle, Pencil, Search, Brain } from 'lucide-react'
import { conversationsApi, annotationsApi, speakerApi, BACKEND_URL } from '../services/api'
import { useConversations, useDeleteConversation, useReprocessTranscript, useReprocessMemory, useReprocessSpeakers, useReprocessOrphan } from '../hooks/useConversations'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import { getStorageKey } from '../utils/storage'
import { WaveformDisplay } from '../components/audio/WaveformDisplay'
import SpeakerNameDropdown from '../components/SpeakerNameDropdown'
import SpeakerInlineInput from '../components/SpeakerInlineInput'

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  detailed_summary?: string
  created_at?: string
  client_id: string
  segment_count?: number  // From list endpoint
  memory_count?: number  // From list endpoint
  audio_chunks_count?: number  // Number of MongoDB audio chunks
  audio_total_duration?: number  // Total duration in seconds
  duration_seconds?: number
  has_memory?: boolean
  transcript?: string  // From detail endpoint
  segments?: Array<{
    text: string
    speaker: string
    segment_type?: string  // "speech" | "event" | "note"
    start: number
    end: number
    confidence?: number
  }>  // From detail endpoint (loaded on expand)
  active_transcript_version?: string
  active_memory_version?: string
  transcript_version_count?: number
  memory_version_count?: number
  active_transcript_version_number?: number
  active_memory_version_number?: number
  deleted?: boolean
  deletion_reason?: string
  deleted_at?: string
  always_persist?: boolean
  processing_status?: string
  is_orphan?: boolean
}

// Speaker color palette for consistent colors across conversations
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
];

export default function Conversations() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [debugMode, setDebugMode] = useState(false)

  const {
    data: conversationsData,
    isLoading: loading,
    error: queryError,
    refetch,
  } = useConversations({ includeUnprocessed: debugMode || undefined })

  const conversations: Conversation[] = conversationsData?.conversations ?? []
  const [actionError, setActionError] = useState<string | null>(null)
  const error = queryError?.message ?? actionError ?? null

  // Transcript expand/collapse state
  const [expandedTranscripts, setExpandedTranscripts] = useState<Set<string>>(new Set())
  // Detailed summary expand/collapse state
  const [expandedDetailedSummaries, setExpandedDetailedSummaries] = useState<Set<string>>(new Set())
  // Audio playback state
  const [playingSegment, setPlayingSegment] = useState<string | null>(null) // Format: "audioUuid-segmentIndex"
  const [audioCurrentTime, setAudioCurrentTime] = useState<{ [conversationId: string]: number }>({})
  const audioRefs = useRef<{ [key: string]: HTMLAudioElement }>({})

  // Reprocessing state
  const [openDropdown, setOpenDropdown] = useState<string | null>(null)
  const [reprocessingTranscript, setReprocessingTranscript] = useState<Set<string>>(new Set())
  const [reprocessingMemory, setReprocessingMemory] = useState<Set<string>>(new Set())
  const [reprocessingSpeakers, setReprocessingSpeakers] = useState<Set<string>>(new Set())
  const [reprocessingOrphan, setReprocessingOrphan] = useState<Set<string>>(new Set())
  const [deletingConversation, setDeletingConversation] = useState<Set<string>>(new Set())

  // Transcript segment editing state
  const [editingSegment, setEditingSegment] = useState<string | null>(null) // Format: "conversationId-segmentIndex"
  const [editedSegmentText, setEditedSegmentText] = useState<string>('')
  const [savingSegment, setSavingSegment] = useState<boolean>(false)
  const [segmentEditError, setSegmentEditError] = useState<string | null>(null)

  // Diarization annotation state
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<Array<{speaker_id: string, name: string}>>([])
  const [diarizationAnnotations, setDiarizationAnnotations] = useState<Map<string, any[]>>(new Map()) // conversationId -> annotations[]

  // Transcript annotation state
  const [transcriptAnnotations, setTranscriptAnnotations] = useState<Map<string, any[]>>(new Map()) // conversationId -> annotations[]

  // Insert annotation state
  const [insertAnnotations, setInsertAnnotations] = useState<Map<string, any[]>>(new Map()) // conversationId -> annotations[]
  const [insertFormOpen, setInsertFormOpen] = useState<string | null>(null) // Format: "conversationId-afterIndex"
  const [insertText, setInsertText] = useState('')
  const [insertSegmentType, setInsertSegmentType] = useState<'event' | 'note' | 'speech'>('speech')
  const [insertSpeaker, setInsertSpeaker] = useState('')

  // Track recently selected speakers in this session (most recent first)
  const [recentSpeakers, setRecentSpeakers] = useState<string[]>([])

  // Preview mode state
  const [previewMode, setPreviewMode] = useState<Set<string>>(new Set()) // conversationIds in preview mode

  // Unified apply state
  const [applyingAnnotations, setApplyingAnnotations] = useState<Set<string>>(new Set())

  // Title editing state
  const [editingTitle, setEditingTitle] = useState<string | null>(null) // conversationId being edited
  const [editedTitle, setEditedTitle] = useState<string>('')
  const [savingTitle, setSavingTitle] = useState<boolean>(false)
  const [titleEditError, setTitleEditError] = useState<string | null>(null)

  // Search state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Conversation[] | null>(null)
  const [isSearching, setIsSearching] = useState(false)
  const [searchTotal, setSearchTotal] = useState(0)
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Compute merged speaker list that includes speakers from annotations
  // This ensures newly created speaker names appear in all dropdowns immediately
  const allSpeakers = useMemo(() => {
    const speakers = [...enrolledSpeakers]
    const existingNames = new Set(speakers.map(s => s.name))
    
    // Add speakers from all diarization annotations
    diarizationAnnotations.forEach((annotations) => {
      annotations.forEach(a => {
        if (a.corrected_speaker && !existingNames.has(a.corrected_speaker)) {
          speakers.push({ speaker_id: `annotation_${a.corrected_speaker}`, name: a.corrected_speaker })
          existingNames.add(a.corrected_speaker)
        }
      })
    })
    return speakers
  }, [enrolledSpeakers, diarizationAnnotations])

  // Stable seek handler for waveform click-to-seek
  const handleSeek = useCallback((conversationId: string, time: number) => {
    const audioElement = audioRefs.current[conversationId];

    if (!audioElement) {
      return;
    }

    // Check if audio is ready for seeking (readyState >= 1 means HAVE_METADATA)
    if (audioElement.readyState < 1) {
      console.warn(`⚠️ Audio not ready for seeking (readyState=${audioElement.readyState})`);
      // Try again after metadata loads
      audioElement.addEventListener('loadedmetadata', () => {
        audioElement.currentTime = time;
      }, { once: true });
      return;
    }

    try {
      // Force a small delay to ensure audio is ready
      const wasPlaying = !audioElement.paused;

      // Pause before seeking (helps with seeking reliability)
      if (wasPlaying) {
        audioElement.pause();
      }

      // Set the seek position
      audioElement.currentTime = time;

      // Verify the seek worked
      setTimeout(() => {
        if (Math.abs(audioElement.currentTime - time) > 1.0) {
          console.error(`Seek failed! Requested ${time.toFixed(2)}s but got ${audioElement.currentTime.toFixed(2)}s`);
        }
      }, 100);

      // Resume playback if it was playing
      if (wasPlaying) {
        audioElement.play().catch(err => {
          console.warn('Could not resume playback after seek:', err);
        });
      }
    } catch (err) {
      console.error('❌ Seek failed:', err);
    }
  }, []); // Empty deps - uses ref which is always stable

  const loadEnrolledSpeakers = async () => {
    try {
      const response = await speakerApi.getEnrolledSpeakers()
      setEnrolledSpeakers(response.data.speakers || [])
    } catch (err: any) {
      console.error('Failed to load enrolled speakers:', err)
    }
  }

  const loadDiarizationAnnotations = async (conversationId: string) => {
    try {
      const response = await annotationsApi.getDiarizationAnnotations(conversationId)
      setDiarizationAnnotations(prev => new Map(prev).set(conversationId, response.data))
    } catch (err: any) {
      console.error('Failed to load diarization annotations:', err)
    }
  }

  const loadTranscriptAnnotations = async (conversationId: string) => {
    try {
      const response = await annotationsApi.getTranscriptAnnotations(conversationId)
      setTranscriptAnnotations(prev => new Map(prev).set(conversationId, response.data))
    } catch (err: any) {
      console.error('Failed to load transcript annotations:', err)
    }
  }

  const loadInsertAnnotations = async (conversationId: string) => {
    try {
      const response = await annotationsApi.getInsertAnnotations(conversationId)
      setInsertAnnotations(prev => new Map(prev).set(conversationId, response.data))
    } catch (err: any) {
      console.error('Failed to load insert annotations:', err)
    }
  }

  const handleDeleteAnnotation = async (annotationId: string, conversationId: string) => {
    try {
      await annotationsApi.deleteAnnotation(annotationId)
      // Reload all annotation types for this conversation
      await Promise.all([
        loadDiarizationAnnotations(conversationId),
        loadTranscriptAnnotations(conversationId),
        loadInsertAnnotations(conversationId),
      ])
    } catch (err: any) {
      console.error('Failed to delete annotation:', err)
      setActionError('Failed to delete annotation')
    }
  }

  const handleCreateInsertAnnotation = async (conversationId: string, afterIndex: number) => {
    if (!insertText.trim()) return
    try {
      await annotationsApi.createInsertAnnotation({
        conversation_id: conversationId,
        insert_after_index: afterIndex,
        insert_text: insertText.trim(),
        insert_segment_type: insertSegmentType,
        ...(insertSegmentType === 'speech' && insertSpeaker ? { insert_speaker: insertSpeaker } : {}),
      })
      setInsertFormOpen(null)
      setInsertText('')
      setInsertSegmentType('speech')
      setInsertSpeaker('')
      await loadInsertAnnotations(conversationId)
    } catch (err: any) {
      console.error('Failed to create insert annotation:', err)
      setActionError('Failed to create insert annotation')
    }
  }

  const handleSpeakerChange = async (conversationId: string, segmentIndex: number, originalSpeaker: string, newSpeaker: string, segmentStartTime: number) => {
    try {
      // Check if a pending annotation already exists for this segment
      const existingAnnotations = diarizationAnnotations.get(conversationId) || []
      const existingAnnotation = existingAnnotations.find(
        a => a.segment_index === segmentIndex && !a.processed
      )

      if (existingAnnotation) {
        // Update existing annotation instead of creating duplicate
        await annotationsApi.updateAnnotation(existingAnnotation.id, {
          corrected_speaker: newSpeaker,
        })
      } else {
        // Create new annotation
        await annotationsApi.createDiarizationAnnotation({
          conversation_id: conversationId,
          segment_index: segmentIndex,
          original_speaker: originalSpeaker,
          corrected_speaker: newSpeaker,
          segment_start_time: segmentStartTime,
        })
      }

      // Temporarily add new speaker name to enrolledSpeakers if it doesn't exist
      // This makes it immediately available in all dropdowns without requiring a backend reload
      setEnrolledSpeakers(prev => {
        const speakerExists = prev.some(speaker => speaker.name === newSpeaker)
        if (!speakerExists) {
          const tempSpeakerId = `temp_${Date.now()}_${newSpeaker.replace(/\s+/g, '_')}`
          return [...prev, { speaker_id: tempSpeakerId, name: newSpeaker }]
        }
        return prev
      })

      // Track as recently used speaker (move to front)
      setRecentSpeakers(prev => [newSpeaker, ...prev.filter(s => s !== newSpeaker)])

      // Reload annotations for this conversation
      await loadDiarizationAnnotations(conversationId)
    } catch (err: any) {
      console.error('Failed to create/update annotation:', err)
      setActionError('Failed to create speaker annotation')
    }
  }

  const handleApplyAllAnnotations = async (conversationId: string) => {
    try {
      setApplyingAnnotations(prev => new Set(prev).add(conversationId))
      setOpenDropdown(null)

      const response = await annotationsApi.applyAllAnnotations(conversationId)

      if (response.status === 200) {
        // Applied annotations successfully

        // Refresh conversation to show new version
        await queryClient.invalidateQueries({ queryKey: ['conversations'] })

        // Reload annotations (should be empty now)
        await loadDiarizationAnnotations(conversationId)
        await loadTranscriptAnnotations(conversationId)
        await loadInsertAnnotations(conversationId)
        // Exit preview mode after applying
        setPreviewMode(prev => {
          const newSet = new Set(prev)
          newSet.delete(conversationId)
          return newSet
        })
      } else {
        setActionError(`Failed to apply annotations: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setActionError(`Error applying annotations: ${err.message || 'Unknown error'}`)
    } finally {
      setApplyingAnnotations(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  useEffect(() => {
    loadEnrolledSpeakers()
  }, [])

  // Load conversations on mount and when debug mode toggles (to include/exclude orphans)
  useEffect(() => {
    loadConversations()
  }, [debugMode])

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = () => setOpenDropdown(null)
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

  // Debounced search
  useEffect(() => {
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current)
    }

    const trimmed = searchQuery.trim()
    if (!trimmed) {
      setSearchResults(null)
      setSearchTotal(0)
      setIsSearching(false)
      return
    }

    setIsSearching(true)
    searchTimeoutRef.current = setTimeout(async () => {
      try {
        const response = await conversationsApi.search(trimmed, 50)
        setSearchResults(response.data.conversations ?? [])
        setSearchTotal(response.data.total ?? 0)
      } catch (err: any) {
        console.error('Search failed:', err)
        setSearchResults([])
        setSearchTotal(0)
      } finally {
        setIsSearching(false)
      }
    }, 300)

    return () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current)
    }
  }, [searchQuery])

  const formatDate = (timestamp: number | string) => {
    // Handle both Unix timestamp (number) and ISO string
    if (typeof timestamp === 'string') {
      // If the string doesn't include timezone info, append 'Z' to treat as UTC
      const isoString = timestamp.endsWith('Z') || timestamp.includes('+') || timestamp.includes('T') && timestamp.split('T')[1].includes('-')
        ? timestamp
        : timestamp + 'Z'
      return new Date(isoString).toLocaleString()
    }
    // If timestamp is 0, return placeholder
    if (timestamp === 0) {
      return 'Unknown date'
    }
    return new Date(timestamp * 1000).toLocaleString()
  }

  const formatDuration = (start: number, end: number) => {
    const duration = end - start
    const minutes = Math.floor(duration / 60)
    const seconds = Math.floor(duration % 60)
    return `${minutes}:${seconds.toString().padStart(2, '0')}`
  }

  const reprocessTranscriptMutation = useReprocessTranscript()

  const handleReprocessTranscript = async (conversation: Conversation) => {
    if (!conversation.conversation_id) {
      setActionError('Cannot reprocess transcript: Conversation ID is missing. This conversation may be from an older format.')
      return
    }

    setReprocessingTranscript(prev => new Set(prev).add(conversation.conversation_id!))
    setOpenDropdown(null)

    try {
      await reprocessTranscriptMutation.mutateAsync(conversation.conversation_id)
    } catch (err: any) {
      setActionError(`Error starting transcript reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingTranscript(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const reprocessMemoryMutation = useReprocessMemory()

  const handleReprocessMemory = async (conversation: Conversation, transcriptVersionId?: string) => {
    if (!conversation.conversation_id) {
      setActionError('Cannot reprocess memory: Conversation ID is missing. This conversation may be from an older format.')
      return
    }

    setReprocessingMemory(prev => new Set(prev).add(conversation.conversation_id!))
    setOpenDropdown(null)

    try {
      await reprocessMemoryMutation.mutateAsync({
        conversationId: conversation.conversation_id,
        transcriptVersionId: transcriptVersionId,
      })
    } catch (err: any) {
      setActionError(`Error starting memory reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingMemory(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const reprocessSpeakersMutation = useReprocessSpeakers()

  const handleReprocessSpeakers = async (conversation: Conversation) => {
    if (!conversation.conversation_id) {
      setActionError('Cannot reprocess speakers: Conversation ID is missing. This conversation may be from an older format.')
      return
    }

    setReprocessingSpeakers(prev => new Set(prev).add(conversation.conversation_id!))
    setOpenDropdown(null)

    try {
      await reprocessSpeakersMutation.mutateAsync({
        conversationId: conversation.conversation_id,
        transcriptVersionId: 'active',
      })
    } catch (err: any) {
      setActionError(`Error starting speaker reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingSpeakers(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const reprocessOrphanMutation = useReprocessOrphan()

  const handleReprocessOrphan = async (conversation: Conversation) => {
    if (!conversation.conversation_id) return

    setReprocessingOrphan(prev => new Set(prev).add(conversation.conversation_id!))


    try {
      await reprocessOrphanMutation.mutateAsync(conversation.conversation_id)
    } catch (err: any) {
      setActionError(`Error starting orphan reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingOrphan(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const deleteConversationMutation = useDeleteConversation()

  const handleDeleteConversation = async (conversationId: string) => {
    const confirmed = window.confirm('Are you sure you want to delete this conversation? This action cannot be undone.')
    if (!confirmed) return

    setDeletingConversation(prev => new Set(prev).add(conversationId))
    setOpenDropdown(null)

    try {
      await deleteConversationMutation.mutateAsync(conversationId)
    } catch (err: any) {
      setActionError(`Error deleting conversation: ${err.message || 'Unknown error'}`)
    } finally {
      setDeletingConversation(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  // Transcript segment editing handlers
  const handleStartSegmentEdit = (conversationId: string, segmentIndex: number, originalText: string) => {
    const segmentKey = `${conversationId}-${segmentIndex}`
    setEditingSegment(segmentKey)
    setEditedSegmentText(originalText)
    setSegmentEditError(null)
  }

  const handleSaveSegmentEdit = async (conversationId: string, segmentIndex: number, originalText: string) => {
    if (!editedSegmentText.trim()) {
      setSegmentEditError('Segment text cannot be empty')
      return
    }

    if (editedSegmentText === originalText) {
      // No changes, just cancel
      handleCancelSegmentEdit()
      return
    }

    try {
      setSavingSegment(true)
      setSegmentEditError(null)

      // Check if a pending annotation already exists for this segment
      const existingAnnotations = transcriptAnnotations.get(conversationId) || []
      const existingAnnotation = existingAnnotations.find(
        a => a.segment_index === segmentIndex && !a.processed
      )

      if (existingAnnotation) {
        // Update existing annotation instead of creating duplicate
        await annotationsApi.updateAnnotation(existingAnnotation.id, {
          corrected_text: editedSegmentText,
        })
      } else {
        // Create annotation (NOT applied immediately)
        await annotationsApi.createTranscriptAnnotation({
          conversation_id: conversationId,
          segment_index: segmentIndex,
          original_text: originalText,
          corrected_text: editedSegmentText
        })
      }

      // Exit edit mode
      setEditingSegment(null)
      setEditedSegmentText('')

      // Reload transcript annotations to show pending badge
      await loadTranscriptAnnotations(conversationId)

    } catch (err: any) {
      console.error('Error saving segment edit:', err)
      setSegmentEditError(err.response?.data?.detail || err.message || 'Failed to save segment edit')
    } finally {
      setSavingSegment(false)
    }
  }

  const handleCancelSegmentEdit = () => {
    setEditingSegment(null)
    setEditedSegmentText('')
    setSegmentEditError(null)
  }

  const handleSegmentKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>, conversationId: string, segmentIndex: number, originalText: string) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSaveSegmentEdit(conversationId, segmentIndex, originalText)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      handleCancelSegmentEdit()
    }
  }

  // Title editing handlers
  const handleStartTitleEdit = (conversationId: string, currentTitle: string) => {
    setEditingTitle(conversationId)
    setEditedTitle(currentTitle)
    setTitleEditError(null)
  }

  const handleSaveTitleEdit = async (conversationId: string, originalTitle: string) => {
    if (!editedTitle.trim()) {
      setTitleEditError('Title cannot be empty')
      return
    }

    if (editedTitle === originalTitle) {
      handleCancelTitleEdit()
      return
    }

    try {
      setSavingTitle(true)
      setTitleEditError(null)

      await annotationsApi.createTitleAnnotation({
        conversation_id: conversationId,
        original_text: originalTitle,
        corrected_text: editedTitle.trim(),
      })

      // Optimistically update the title in local state
      queryClient.setQueryData(['conversations', { includeUnprocessed: debugMode || undefined }], (old: any) => {
        if (!old) return old
        return {
          ...old,
          conversations: old.conversations.map((c: Conversation) =>
            c.conversation_id === conversationId
              ? { ...c, title: editedTitle.trim() }
              : c
          ),
        }
      })

      setEditingTitle(null)
      setEditedTitle('')
    } catch (err: any) {
      console.error('Error saving title edit:', err)
      setTitleEditError(err.response?.data?.detail || err.message || 'Failed to save title')
    } finally {
      setSavingTitle(false)
    }
  }

  const handleCancelTitleEdit = () => {
    setEditingTitle(null)
    setEditedTitle('')
    setTitleEditError(null)
  }

  const handleTitleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>, conversationId: string, originalTitle: string) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleSaveTitleEdit(conversationId, originalTitle)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      handleCancelTitleEdit()
    }
  }

  const toggleDetailedSummary = async (conversationId: string) => {
    // If already expanded, just collapse
    if (expandedDetailedSummaries.has(conversationId)) {
      setExpandedDetailedSummaries(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
      return
    }

    // Find the conversation by conversation_id
    const conversation = conversations.find(c => c.conversation_id === conversationId)
    if (!conversation || !conversation.conversation_id) {
      console.error('Cannot expand detailed summary: conversation_id missing')
      return
    }

    // Check if detailed_summary is already loaded
    if (conversation.detailed_summary) {
      setExpandedDetailedSummaries(prev => new Set(prev).add(conversationId))
      return
    }

    // Fetch full conversation details to get detailed_summary
    try {
      const response = await conversationsApi.getById(conversation.conversation_id)
      if (response.status === 200 && response.data.conversation) {
        // Update the conversation in query cache with detailed_summary
        queryClient.setQueryData(['conversations', { includeUnprocessed: debugMode || undefined }], (old: any) => {
          if (!old) return old
          return {
            ...old,
            conversations: old.conversations.map((c: Conversation) =>
              c.conversation_id === conversationId
                ? { ...c, detailed_summary: response.data.conversation.detailed_summary }
                : c
            ),
          }
        })
        // Expand the detailed summary
        setExpandedDetailedSummaries(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch detailed summary:', err)
      setActionError(`Failed to load detailed summary: ${err.message || 'Unknown error'}`)
    }
  }

  const toggleTranscriptExpansion = async (conversationId: string) => {
    // If already expanded, just collapse
    if (expandedTranscripts.has(conversationId)) {
      setExpandedTranscripts(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
      return
    }

    // Find the conversation by conversation_id
    const conversation = conversations.find(c => c.conversation_id === conversationId)
    if (!conversation || !conversation.conversation_id) {
      console.error('Cannot expand transcript: conversation_id missing')
      return
    }

    // If segments are already loaded, just expand
    if (conversation.segments && conversation.segments.length > 0) {
      setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      return
    }

    // Fetch full conversation details including segments
    try {
      const response = await conversationsApi.getById(conversation.conversation_id)
      if (response.status === 200 && response.data.conversation) {
        // Update the conversation in query cache with full data
        queryClient.setQueryData(['conversations', { includeUnprocessed: debugMode || undefined }], (old: any) => {
          if (!old) return old
          return {
            ...old,
            conversations: old.conversations.map((c: Conversation) =>
              c.conversation_id === conversationId
                ? { ...c, ...response.data.conversation }
                : c
            ),
          }
        })
        // Load all annotation types for this conversation
        await loadDiarizationAnnotations(conversationId)
        await loadTranscriptAnnotations(conversationId)
        await loadInsertAnnotations(conversationId)
        // Expand the transcript
        setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch conversation details:', err)
      setActionError(`Failed to load transcript: ${err.message || 'Unknown error'}`)
    }
  }

  const handleSegmentPlayPause = (conversationId: string, segmentIndex: number, segment: any) => {
    const segmentId = `${conversationId}-${segmentIndex}`;

    // If this segment is already playing, pause it
    if (playingSegment === segmentId) {
      const audio = audioRefs.current[segmentId];
      if (audio) {
        audio.pause();
      }
      setPlayingSegment(null);
      return;
    }

    // Stop any currently playing segment
    if (playingSegment) {
      const currentAudio = audioRefs.current[playingSegment];
      if (currentAudio) {
        currentAudio.pause();
      }
    }

    // Get or create audio element for this specific segment
    let audio = audioRefs.current[segmentId];

    // Create new audio element with segment-specific URL
    if (!audio || audio.error) {
      const token = localStorage.getItem(getStorageKey('token')) || '';
      // Use chunks endpoint with time range for instant loading (only fetches needed chunks)
      const audioUrl = `${BACKEND_URL}/api/audio/chunks/${conversationId}?start_time=${segment.start}&end_time=${segment.end}&token=${token}`;
      audio = new Audio(audioUrl);
      audioRefs.current[segmentId] = audio;

      // Add error listener for debugging
      audio.addEventListener('error', () => {
        console.error('Audio segment error:', audio.error?.code, audio.error?.message);
        console.error('Audio src:', audio.src);
      });

      // Add event listener to handle when audio ends naturally
      audio.addEventListener('ended', () => {
        setPlayingSegment(null);
      });
    }

    // Play the segment (no need to seek since audio is already trimmed to exact range)
    audio.play().then(() => {
      setPlayingSegment(segmentId);
    }).catch(err => {
      console.error('Error playing audio segment:', err);
      setPlayingSegment(null);
    });
  }

  // Cleanup audio on unmount
  useEffect(() => {
    return () => {
      // Stop all audio elements
      Object.values(audioRefs.current).forEach(audio => {
        audio.pause();
      });
    };
  }, [])


  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600 dark:text-gray-400">Loading conversations...</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center">
        <div className="text-red-600 dark:text-red-400 mb-4">{error}</div>
        <button
          onClick={() => { setActionError(null); refetch() }}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          Try Again
        </button>
      </div>
    )
  }

  return (
    <div>
      {/* Header with Search */}
      <div className="flex flex-col gap-4 mb-6">
        <div className="flex justify-between items-center">
          <div className="flex items-center space-x-2">
            <MessageSquare className="h-6 w-6 text-blue-600" />
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Conversations
            </h1>
          </div>
          <div className="flex items-center space-x-4">
            <label className="flex items-center space-x-2 text-sm">
              <input
                type="checkbox"
                checked={debugMode}
                onChange={(e) => setDebugMode(e.target.checked)}
                className="rounded border-gray-300"
              />
              <span className="text-gray-700 dark:text-gray-300">Debug Mode</span>
            </label>
            <button
              onClick={() => refetch()}
              className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
            >
              <RefreshCw className="h-4 w-4" />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        {/* Search Bar */}
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search conversations..."
              className="w-full pl-9 pr-9 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent text-sm"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery('')}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
          <button
            disabled
            title="Semantic search coming soon"
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500 cursor-not-allowed text-sm"
          >
            <Brain className="h-4 w-4" />
            <span>Semantic</span>
          </button>
        </div>

        {/* Search status */}
        {searchQuery.trim() && (
          <div className="text-sm text-gray-500 dark:text-gray-400">
            {isSearching ? (
              <span className="flex items-center gap-1">
                <RefreshCw className="h-3 w-3 animate-spin" />
                Searching...
              </span>
            ) : searchResults !== null ? (
              <span>{searchTotal} result{searchTotal !== 1 ? 's' : ''} for "{searchQuery.trim()}"</span>
            ) : null}
          </div>
        )}
      </div>

      {/* Conversations List */}
      <div className="space-y-6">
        {(() => {
          const displayConversations = searchResults ?? conversations
          return displayConversations.length === 0 ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-12">
            <MessageSquare className="h-12 w-12 mx-auto mb-4 opacity-50" />
            <p>{searchResults !== null ? 'No matching conversations' : 'No conversations found'}</p>
          </div>
        ) : (
          displayConversations.map((conversation) => (
            <div
              key={conversation.conversation_id}
              className={`rounded-lg p-6 border ${
                conversation.is_orphan
                  ? 'bg-amber-50 dark:bg-amber-900/10 border-amber-300 dark:border-amber-700'
                  : 'bg-gray-50 dark:bg-gray-700 border-gray-200 dark:border-gray-600'
              }`}
            >
              {/* Orphan Audio Session Banner */}
              {conversation.is_orphan && (
                <div className="mb-4 p-3 bg-amber-100 dark:bg-amber-900/30 rounded-lg border border-amber-200 dark:border-amber-800 flex items-center justify-between">
                  <div className="flex items-center space-x-2">
                    <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400 flex-shrink-0" />
                    <div>
                      <span className="text-sm font-medium text-amber-800 dark:text-amber-200">
                        Unprocessed Audio Session
                      </span>
                      <span className="text-xs text-amber-600 dark:text-amber-400 ml-2">
                        {conversation.processing_status === 'transcription_failed' ? 'Transcription failed' :
                         conversation.processing_status === 'reprocessing' ? 'Reprocessing...' :
                         conversation.deleted ? `Deleted: ${conversation.deletion_reason}` :
                         conversation.processing_status || 'Pending'}
                        {conversation.audio_total_duration ? ` · ${Math.floor(conversation.audio_total_duration / 60)}:${Math.floor(conversation.audio_total_duration % 60).toString().padStart(2, '0')} audio` : ''}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => handleReprocessOrphan(conversation)}
                    disabled={reprocessingOrphan.has(conversation.conversation_id)}
                    className="flex items-center space-x-1 px-3 py-1.5 text-sm font-medium text-white bg-amber-600 hover:bg-amber-700 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {reprocessingOrphan.has(conversation.conversation_id) ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RotateCcw className="h-3.5 w-3.5" />
                    )}
                    <span>{reprocessingOrphan.has(conversation.conversation_id) ? 'Reprocessing...' : 'Reprocess'}</span>
                  </button>
                </div>
              )}

              {/* Version Selector Header */}
              <ConversationVersionHeader
                conversationId={conversation.conversation_id}
                  versionInfo={{
                    transcript_count: conversation.transcript_version_count || 0,
                    memory_count: conversation.memory_version_count || 0,
                    active_transcript_version: conversation.active_transcript_version,
                    active_memory_version: conversation.active_memory_version,
                    active_transcript_version_number: conversation.active_transcript_version_number,
                    active_memory_version_number: conversation.active_memory_version_number
                  }}
                  onVersionChange={async () => {
                    // Update only this specific conversation without reloading all conversations
                    // This prevents page scroll jump
                    try {
                      const response = await conversationsApi.getById(conversation.conversation_id!)
                      if (response.status === 200 && response.data.conversation) {
                        queryClient.setQueryData(['conversations', { includeUnprocessed: debugMode || undefined }], (old: any) => {
                          if (!old) return old
                          return {
                            ...old,
                            conversations: old.conversations.map((c: Conversation) =>
                              c.conversation_id === conversation.conversation_id
                                ? { ...c, ...response.data.conversation }
                                : c
                            ),
                          }
                        })
                      }
                    } catch (err: any) {
                      console.error('Failed to refresh conversation:', err)
                      // Fallback to full reload on error
                      refetch()
                    }
                  }}
                />

              {/* Conversation Header */}
              <div className="flex justify-between items-start mb-4">
                <div className="flex flex-col space-y-2">
                  {/* Conversation Title - Editable */}
                  {editingTitle === conversation.conversation_id ? (
                    <div className="flex items-center space-x-2">
                      <input
                        type="text"
                        value={editedTitle}
                        onChange={(e) => setEditedTitle(e.target.value)}
                        onKeyDown={(e) => handleTitleKeyDown(e, conversation.conversation_id, conversation.title || 'Conversation')}
                        className="text-xl font-semibold px-2 py-1 border-2 border-blue-500 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 min-w-[200px]"
                        autoFocus
                        disabled={savingTitle}
                      />
                      <button
                        onClick={() => handleSaveTitleEdit(conversation.conversation_id, conversation.title || 'Conversation')}
                        disabled={savingTitle || editedTitle === (conversation.title || 'Conversation')}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        <Save className="w-3 h-3" />
                        {savingTitle ? 'Saving...' : 'Save'}
                      </button>
                      <button
                        onClick={handleCancelTitleEdit}
                        disabled={savingTitle}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-200 dark:bg-gray-600 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        <X className="w-3 h-3" />
                        Cancel
                      </button>
                      {titleEditError && (
                        <span className="text-xs text-red-600 dark:text-red-400">{titleEditError}</span>
                      )}
                    </div>
                  ) : (
                    <h2
                      className="text-xl font-semibold text-gray-900 dark:text-gray-100 group cursor-pointer hover:bg-yellow-100 dark:hover:bg-yellow-900/30 px-1 rounded transition-colors inline-flex items-center gap-2"
                      onClick={() => handleStartTitleEdit(conversation.conversation_id, conversation.title || 'Conversation')}
                      title="Click to edit title"
                    >
                      {conversation.title || "Conversation"}
                      <Pencil className="w-3.5 h-3.5 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity" />
                    </h2>
                  )}

                  {/* Short Summary - Always visible */}
                  {conversation.summary && (
                    <p className="text-sm text-gray-600 dark:text-gray-400 italic">
                      {conversation.summary}
                    </p>
                  )}

                  {/* Detailed Summary Expand Button */}
                  {conversation.conversation_id && (
                    <div className="mt-2">
                      <button
                        onClick={() => toggleDetailedSummary(conversation.conversation_id!)}
                        className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center space-x-1"
                      >
                        <span>
                          {expandedDetailedSummaries.has(conversation.conversation_id) ? '▼' : '▶'} Detailed Summary
                        </span>
                      </button>

                      {/* Detailed Summary Content */}
                      {expandedDetailedSummaries.has(conversation.conversation_id) && conversation.detailed_summary && (
                        <div className="mt-2 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800 animate-in slide-in-from-top-2 duration-200">
                          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                            {conversation.detailed_summary}
                          </p>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Metadata */}
                  <div className="flex items-center space-x-4">
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                      <Calendar className="h-4 w-4" />
                      <span>{formatDate(conversation.created_at || '')}</span>
                    </div>
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                      <User className="h-4 w-4" />
                      <span>{conversation.client_id}</span>
                    </div>
                    {conversation.duration_seconds && conversation.duration_seconds > 0 && (
                      <div className="text-sm text-gray-600 dark:text-gray-400">
                        Duration: {Math.floor(conversation.duration_seconds / 60)}:{(conversation.duration_seconds % 60).toFixed(0).padStart(2, '0')}
                      </div>
                    )}
                    {(conversation.memory_count ?? 0) > 0 && (
                      <div className="flex items-center space-x-1 text-sm text-purple-600 dark:text-purple-400">
                        <Brain className="h-4 w-4" />
                        <span>{conversation.memory_count}</span>
                      </div>
                    )}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        navigate(`/conversations/${conversation.conversation_id}`)
                      }}
                      className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                    >
                      View Details
                    </button>
                  </div>
                </div>

                {/* Hamburger Menu */}
                <div className="relative">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      setOpenDropdown(openDropdown === conversation.conversation_id ? null : conversation.conversation_id)
                    }}
                    className="p-1 rounded-full hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                    title="Conversation options"
                  >
                    <MoreVertical className="h-5 w-5 text-gray-500 dark:text-gray-400" />
                  </button>

                  {/* Dropdown Menu */}
                  {openDropdown === conversation.conversation_id && (
                    <div className="absolute right-0 top-8 w-48 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-600 py-2 z-10">
                      <button
                        onClick={() => handleReprocessTranscript(conversation)}
                        disabled={!conversation.conversation_id || reprocessingTranscript.has(conversation.conversation_id)}
                        className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {conversation.conversation_id && reprocessingTranscript.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="h-4 w-4" />
                        )}
                        <span>Reprocess Transcript</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                      <button
                        onClick={() => handleReprocessMemory(conversation)}
                        disabled={!conversation.conversation_id || reprocessingMemory.has(conversation.conversation_id)}
                        className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {conversation.conversation_id && reprocessingMemory.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <Zap className="h-4 w-4" />
                        )}
                        <span>Reprocess Memory</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                      <button
                        onClick={() => handleReprocessSpeakers(conversation)}
                        disabled={!conversation.conversation_id || reprocessingSpeakers.has(conversation.conversation_id)}
                        className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Create new transcript version with re-identified speakers (automatically updates memories)"
                      >
                        {conversation.conversation_id && reprocessingSpeakers.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <User className="h-4 w-4" />
                        )}
                        <span>Reprocess Who Spoke</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                      <div className="border-t border-gray-200 dark:border-gray-600 my-1"></div>

                      {/* Apply All Annotations Button */}
                      {(() => {
                        const diarAnnotations = diarizationAnnotations.get(conversation.conversation_id!) || []
                        const transcriptAnnots = transcriptAnnotations.get(conversation.conversation_id!) || []
                        const insertAnnots = insertAnnotations.get(conversation.conversation_id!) || []

                        const diarPending = diarAnnotations.filter(a => !a.processed).length
                        const transcriptPending = transcriptAnnots.filter(a => !a.processed).length
                        const insertPending = insertAnnots.filter(a => !a.processed).length
                        const totalPending = diarPending + transcriptPending + insertPending

                        if (totalPending === 0) return null

                        return (
                          <>
                            <button
                              onClick={() => {
                                const convId = conversation.conversation_id!
                                setPreviewMode(prev => {
                                  const newSet = new Set(prev)
                                  if (newSet.has(convId)) newSet.delete(convId)
                                  else newSet.add(convId)
                                  return newSet
                                })
                                setOpenDropdown(null)
                              }}
                              className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2"
                            >
                              <Search className="h-4 w-4" />
                              <span>
                                {previewMode.has(conversation.conversation_id!) ? 'Exit Preview' : 'Preview Changes'}
                              </span>
                            </button>
                            <button
                              onClick={() => handleApplyAllAnnotations(conversation.conversation_id!)}
                              disabled={!conversation.conversation_id || applyingAnnotations.has(conversation.conversation_id!)}
                              className="w-full text-left px-4 py-2 text-sm text-blue-700 dark:text-blue-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
                              title={`Apply ${diarPending} speaker, ${transcriptPending} text, ${insertPending} insert corrections`}
                            >
                              {conversation.conversation_id && applyingAnnotations.has(conversation.conversation_id!) ? (
                                <RefreshCw className="h-4 w-4 animate-spin" />
                              ) : (
                                <Check className="h-4 w-4" />
                              )}
                              <span>
                                Apply Changes ({totalPending})
                              </span>
                            </button>
                          </>
                        )
                      })()}

                      <div className="border-t border-gray-200 dark:border-gray-600 my-1"></div>
                      <button
                        onClick={() => conversation.conversation_id && handleDeleteConversation(conversation.conversation_id)}
                        disabled={!conversation.conversation_id || (!!conversation.conversation_id && deletingConversation.has(conversation.conversation_id))}
                        className="w-full text-left px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {conversation.conversation_id && deletingConversation.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <Trash2 className="h-4 w-4" />
                        )}
                        <span>Delete Conversation</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                    </div>
                  )}
                </div>
              </div>

              {/* Audio Player with Waveform */}
              <div className="mb-4">
                <div className="space-y-2">
                  {(conversation.audio_chunks_count && conversation.audio_chunks_count > 0) && (
                    <>
                      <div className="flex items-center space-x-2 text-sm text-gray-700 dark:text-gray-300">
                        <span className="font-medium">
                          🎵 Audio
                        </span>
                      </div>

                      {/* Waveform Visualization */}
                      {conversation.conversation_id && conversation.audio_total_duration && (
                        <WaveformDisplay
                          conversationId={conversation.conversation_id}
                          duration={conversation.audio_total_duration}
                          currentTime={conversation.conversation_id ? audioCurrentTime[conversation.conversation_id] : undefined}
                          onSeek={(time) => handleSeek(conversation.conversation_id!, time)}
                          height={80}
                        />
                      )}

                      {/* Audio Player */}
                      <audio
                        ref={(el) => {
                          if (el && conversation.conversation_id) {
                            audioRefs.current[conversation.conversation_id] = el;
                          }
                        }}
                        controls
                        className="w-full h-10"
                        preload="metadata"
                        style={{ minWidth: '300px' }}
                        src={`${BACKEND_URL}/api/audio/get_audio/${conversation.conversation_id}?token=${localStorage.getItem(getStorageKey('token')) || ''}`}
                        onTimeUpdate={(e) => {
                          // Extract currentTime IMMEDIATELY before any async operations
                          const currentTime = e.currentTarget?.currentTime;
                          const conversationId = conversation.conversation_id;

                          if (conversationId && currentTime !== undefined) {
                            setAudioCurrentTime(prev => ({
                              ...prev,
                              [conversationId]: currentTime
                            }));
                          }
                        }}
                      >
                        Your browser does not support the audio element.
                      </audio>
                    </>
                  )}
                </div>
              </div>

              {/* Transcript */}
              <div className="space-y-2">
                {(() => {
                  // Get segments directly from conversation (returned by detail endpoint)
                  const segments = conversation.segments || []

                  return (
                    <>
                      {/* Transcript Header with Expand/Collapse */}
                      <div
                        className="flex items-center justify-between cursor-pointer p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors"
                        onClick={() => conversation.conversation_id && toggleTranscriptExpansion(conversation.conversation_id)}
                      >
                        <h3 className="font-medium text-gray-900 dark:text-gray-100">
                          Transcript {(segments.length > 0 || conversation.segment_count) && (
                            <span className="text-sm text-gray-500 dark:text-gray-400 ml-1">
                              ({segments.length || conversation.segment_count || 0} segments)
                            </span>
                          )}
                        </h3>
                        <div className="flex items-center space-x-2">
                          {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) ? (
                            <ChevronUp className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          ) : (
                            <ChevronDown className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          )}
                        </div>
                      </div>

                      {/* Transcript Content - Conditionally Rendered */}
                      {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) && (
                        <div className="animate-in slide-in-from-top-2 duration-300 ease-out space-y-4">
                          {segments.length > 0 ? (
                            <div className="p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-600">
                              <div className="space-y-1">
                                {(() => {
                                  // Build a speaker-to-color map for this conversation
                                  const speakerColorMap: { [key: string]: string } = {}
                                  let colorIndex = 0

                                  // First pass: assign colors to unique speakers
                                  segments.forEach(segment => {
                                    const speaker = segment.speaker || 'Unknown'
                                    if (!speakerColorMap[speaker]) {
                                      speakerColorMap[speaker] = SPEAKER_COLOR_PALETTE[colorIndex % SPEAKER_COLOR_PALETTE.length]
                                      colorIndex++
                                    }
                                  })

                                  const convId = conversation.conversation_id!
                                  const isPreview = previewMode.has(convId)
                                  const convDiarAnnotations = diarizationAnnotations.get(convId) || []
                                  const convTranscriptAnnotations = transcriptAnnotations.get(convId) || []
                                  const convInsertAnnotations = (insertAnnotations.get(convId) || []).filter(a => !a.processed)

                                  // Build preview segments if in preview mode
                                  const previewSegments = isPreview ? segments.map((seg, idx) => {
                                    const diarAnnot = convDiarAnnotations.find(a => a.segment_index === idx && !a.processed)
                                    const textAnnot = convTranscriptAnnotations.find(a => a.segment_index === idx && !a.processed)
                                    return {
                                      ...seg,
                                      speaker: diarAnnot ? diarAnnot.corrected_speaker : seg.speaker,
                                      text: textAnnot ? textAnnot.corrected_text : seg.text,
                                    }
                                  }) : segments

                                  // Insert divider helper
                                  const renderInsertDivider = (afterIndex: number) => {
                                    const insertKey = `${convId}-${afterIndex}`
                                    const isOpen = insertFormOpen === insertKey
                                    // Show pending inserts at this position
                                    const pendingInserts = convInsertAnnotations.filter(a => a.insert_after_index === afterIndex)

                                    return (
                                      <div key={`insert-${afterIndex}`}>
                                        {/* Pending insert annotations at this position */}
                                        {pendingInserts.map(ins => (
                                          <div
                                            key={`pending-insert-${ins.id}`}
                                            className={`text-sm border-l-2 border-purple-400 dark:border-purple-600 pl-3 py-0.5 px-2 flex items-center justify-between bg-purple-50 dark:bg-purple-900/20 rounded-r ${
                                              ins.insert_segment_type === 'speech' ? 'text-gray-800 dark:text-gray-200' : 'italic text-gray-500 dark:text-gray-400'
                                            }`}
                                          >
                                            <span>
                                              {ins.insert_segment_type === 'speech'
                                                ? <><span className="font-medium text-blue-600 dark:text-blue-400">{ins.insert_speaker || 'Speaker'}</span>: {ins.insert_text}</>
                                                : ins.insert_segment_type === 'note' ? `[Note: ${ins.insert_text}]` : ins.insert_text}
                                              <span className="text-xs bg-purple-100 dark:bg-purple-900 text-purple-600 dark:text-purple-300 px-2 py-0.5 rounded ml-2">Pending Insert</span>
                                            </span>
                                            <button
                                              onClick={() => handleDeleteAnnotation(ins.id, convId)}
                                              className="ml-2 text-gray-400 hover:text-red-500 transition-colors"
                                              title="Remove insert"
                                            >
                                              <X className="w-3 h-3" />
                                            </button>
                                          </div>
                                        ))}

                                        {/* Insert form (when open) */}
                                        {!isPreview && isOpen && (
                                          <div className="w-full border border-purple-200 dark:border-purple-700 rounded-lg p-2 bg-purple-50 dark:bg-purple-900/20 space-y-2" onClick={e => e.stopPropagation()}>
                                            {insertSegmentType !== 'speech' && (
                                              <div className="flex flex-wrap gap-1">
                                                {['[laughter]', '[music]', '[applause]', '[silence]', '[unintelligible]', '[crosstalk]'].map(tag => (
                                                  <button
                                                    key={tag}
                                                    onClick={() => setInsertText(tag)}
                                                    className={`px-2 py-0.5 text-xs rounded border transition-colors ${
                                                      insertText === tag
                                                        ? 'bg-purple-200 dark:bg-purple-700 border-purple-400 dark:border-purple-500'
                                                        : 'bg-white dark:bg-gray-700 border-gray-300 dark:border-gray-600 hover:border-purple-300'
                                                    }`}
                                                  >
                                                    {tag}
                                                  </button>
                                                ))}
                                              </div>
                                            )}
                                            {insertSegmentType === 'speech' && (
                                              <div className="flex items-center gap-2">
                                                <label className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">Speaker:</label>
                                                <SpeakerInlineInput
                                                  value={insertSpeaker}
                                                  onChange={setInsertSpeaker}
                                                  onSelect={(speaker) => {
                                                    setInsertSpeaker(speaker)
                                                    setRecentSpeakers(prev => [speaker, ...prev.filter(s => s !== speaker)])
                                                  }}
                                                  enrolledSpeakers={allSpeakers}
                                                  recentSpeakers={recentSpeakers}
                                                  placeholder="Type or select speaker..."
                                                />
                                              </div>
                                            )}
                                            <div className="flex items-center gap-2">
                                              <input
                                                type="text"
                                                value={insertText}
                                                onChange={e => setInsertText(e.target.value)}
                                                placeholder={insertSegmentType === 'speech' ? "What was said..." : "Custom text..."}
                                                className="flex-1 px-2 py-1 text-xs border rounded bg-white dark:bg-gray-700 dark:border-gray-600 focus:outline-none focus:ring-1 focus:ring-purple-500"
                                                onKeyDown={e => { if (e.key === 'Enter') handleCreateInsertAnnotation(convId, afterIndex); if (e.key === 'Escape') setInsertFormOpen(null); }}
                                                autoFocus
                                              />
                                              <select
                                                value={insertSegmentType}
                                                onChange={e => setInsertSegmentType(e.target.value as 'event' | 'note' | 'speech')}
                                                className="px-2 py-1 text-xs border rounded bg-white dark:bg-gray-700 dark:border-gray-600"
                                              >
                                                <option value="speech">Speech</option>
                                                <option value="event">Event Tag</option>
                                                <option value="note">Note</option>
                                              </select>
                                              <button
                                                onClick={() => handleCreateInsertAnnotation(convId, afterIndex)}
                                                disabled={!insertText.trim()}
                                                className="px-2 py-1 text-xs text-white bg-purple-600 rounded hover:bg-purple-700 disabled:opacity-50"
                                              >
                                                Insert
                                              </button>
                                              <button
                                                onClick={() => setInsertFormOpen(null)}
                                                className="px-2 py-1 text-xs text-gray-600 dark:text-gray-300 bg-gray-200 dark:bg-gray-600 rounded hover:bg-gray-300"
                                              >
                                                Cancel
                                              </button>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    )
                                  }

                                  // Insert button helper — appears at top/bottom of hovered segment
                                  const insertBtnClass = (position: 'top' | 'bottom') =>
                                    `absolute ${position === 'top' ? 'top-0 -translate-y-1/2' : 'bottom-0 translate-y-1/2'} left-1/2 -translate-x-1/2 z-10 opacity-0 group-hover/seg:opacity-30 hover:!opacity-100 transition-opacity px-1.5 py-0 text-xs leading-tight text-gray-400 dark:text-gray-500 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 rounded-full hover:text-purple-500 hover:border-purple-400 dark:hover:text-purple-400 dark:hover:border-purple-500`
                                  const openInsertForm = (afterIndex: number, e: React.MouseEvent) => {
                                    e.stopPropagation()
                                    setInsertFormOpen(`${convId}-${afterIndex}`)
                                    setInsertText('')
                                    setInsertSegmentType('speech')
                                    setInsertSpeaker('')
                                  }

                                  // Render the transcript
                                  const renderedSegments: JSX.Element[] = []

                                  // Insert divider before first segment
                                  renderedSegments.push(renderInsertDivider(-1))

                                  previewSegments.forEach((segment, index) => {
                          const speaker = segment.speaker || 'Unknown'
                          const segType = (segment as any).segment_type || 'speech'
                          const isNonSpeech = segType === 'event' || segType === 'note'
                          // Use conversation_id for unique segment IDs
                          const segmentId = `${conversation.conversation_id}-${index}`
                          const isPlaying = playingSegment === segmentId
                          const hasAudio = !!conversation.audio_chunks_count && conversation.audio_chunks_count > 0
                          const isEditing = editingSegment === segmentId

                          // Non-speech segment rendering (event/note)
                          if (isNonSpeech) {
                            renderedSegments.push(
                              <div key={index} className="group/seg relative">
                                {!isPreview && <button onClick={(e) => openInsertForm(index === 0 ? -1 : index - 1, e)} className={insertBtnClass('top')}>+</button>}
                                <div className="text-sm italic text-gray-500 dark:text-gray-400 border-l-2 border-gray-300 dark:border-gray-600 pl-3 py-0.5 px-2 rounded-r">
                                  <span>{segType === 'note' ? `[Note: ${segment.text}]` : segment.text}</span>
                                </div>
                                {!isPreview && <button onClick={(e) => openInsertForm(index, e)} className={insertBtnClass('bottom')}>+</button>}
                              </div>
                            )
                            renderedSegments.push(renderInsertDivider(index))
                            return
                          }

                          renderedSegments.push(
                            <div key={index} className="group/seg relative">
                            {!isPreview && <button onClick={(e) => openInsertForm(index === 0 ? -1 : index - 1, e)} className={insertBtnClass('top')}>+</button>}
                            <div
                              className={`text-sm leading-relaxed flex items-start space-x-2 py-1 px-2 rounded transition-colors ${
                                isPlaying ? 'bg-blue-50 dark:bg-blue-900/20' : isEditing ? 'bg-yellow-50 dark:bg-yellow-900/20' : 'hover:bg-gray-50 dark:hover:bg-gray-700'
                              }`}
                            >
                              {/* Play/Pause Button */}
                              {hasAudio && !isEditing && (
                                <button
                                  onClick={() => handleSegmentPlayPause(conversation.conversation_id, index, segment)}
                                  className={`flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center transition-colors mt-0.5 ${
                                    isPlaying
                                      ? 'bg-blue-600 text-white hover:bg-blue-700'
                                      : 'bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-500'
                                  }`}
                                  title={isPlaying ? 'Pause segment' : 'Play segment'}
                                >
                                  {isPlaying ? (
                                    <Pause className="w-2.5 h-2.5" />
                                  ) : (
                                    <Play className="w-2.5 h-2.5 ml-0.5" />
                                  )}
                                </button>
                              )}

                              <div className="flex-1 min-w-0">
                                {debugMode && (
                                  <span className="text-xs text-gray-400 mr-2">
                                    [start: {segment.start.toFixed(1)}s, end: {segment.end.toFixed(1)}s, duration: {formatDuration(segment.start, segment.end)}]
                                  </span>
                                )}

                                {/* Speaker Name - Preview mode shows final result, normal mode shows annotation UI */}
                                {isPreview ? (
                                  <span className="inline-flex items-center space-x-1">
                                    <span className={`font-medium ${speakerColorMap[speaker] || 'text-gray-600'}`}>{speaker}</span>
                                    <span>:</span>
                                  </span>
                                ) : (() => {
                                  const annotation = convDiarAnnotations.find(a => a.segment_index === index && !a.processed)
                                  const speakerColor = speakerColorMap[speaker]
                                  const currentSpeaker = annotation ? annotation.corrected_speaker : speaker
                                  const originalSpeaker = annotation ? annotation.original_speaker : speaker

                                  return (
                                    <span className="inline-flex items-center space-x-1">
                                      {annotation && (
                                        <>
                                          <span className="text-xs bg-orange-100 dark:bg-orange-900 text-orange-600 dark:text-orange-300 px-2 py-0.5 rounded" title="Pending annotation">
                                            Pending
                                          </span>
                                          <button
                                            onClick={() => handleDeleteAnnotation(annotation.id, convId)}
                                            className="text-gray-400 hover:text-red-500 transition-colors"
                                            title="Revert speaker change"
                                          >
                                            <X className="w-3 h-3" />
                                          </button>
                                        </>
                                      )}
                                      <SpeakerNameDropdown
                                        currentSpeaker={currentSpeaker}
                                        enrolledSpeakers={allSpeakers}
                                        onSpeakerChange={(newSpeaker) =>
                                          handleSpeakerChange(conversation.conversation_id!, index, originalSpeaker, newSpeaker, segment.start)
                                        }
                                        segmentIndex={index}
                                        conversationId={conversation.conversation_id!}
                                        annotated={!!annotation}
                                        recentSpeakers={recentSpeakers}
                                        speakerColor={annotation ? 'text-green-600 dark:text-green-400' : speakerColor}
                                      />
                                      <span>:</span>
                                    </span>
                                  )
                                })()}

                                {/* Segment Text - Preview mode shows final, normal mode shows annotation UI */}
                                {isPreview ? (
                                  <span className="text-gray-900 dark:text-gray-100 ml-1">{segment.text}</span>
                                ) : (() => {
                                  const textAnnotation = convTranscriptAnnotations.find(
                                    a => a.segment_index === index && !a.processed
                                  )

                                  if (textAnnotation && !isEditing) {
                                    return (
                                      <span className="inline-flex items-start space-x-2 ml-1">
                                        <span className="line-through text-gray-400">{textAnnotation.original_text}</span>
                                        <span>→</span>
                                        <span
                                          onClick={() => conversation.conversation_id && handleStartSegmentEdit(conversation.conversation_id, index, textAnnotation.corrected_text)}
                                          className="text-blue-600 dark:text-blue-400 cursor-pointer hover:bg-yellow-100 dark:hover:bg-yellow-900/30 px-1 rounded transition-colors"
                                          title="Click to edit segment"
                                        >
                                          {textAnnotation.corrected_text}
                                        </span>
                                        <span className="text-xs bg-blue-100 dark:bg-blue-900 text-blue-600 dark:text-blue-300 px-2 py-0.5 rounded">Pending</span>
                                        <button
                                          onClick={() => handleDeleteAnnotation(textAnnotation.id, convId)}
                                          className="text-gray-400 hover:text-red-500 transition-colors"
                                          title="Revert text change"
                                        >
                                          <X className="w-3 h-3" />
                                        </button>
                                      </span>
                                    )
                                  } else if (isEditing) {
                                    return (
                                      <div className="ml-1 space-y-2">
                                        <textarea
                                          value={editedSegmentText}
                                          onChange={(e) => setEditedSegmentText(e.target.value)}
                                          onKeyDown={(e) => handleSegmentKeyDown(e, conversation.conversation_id, index, segment.text)}
                                          className="w-full min-h-[60px] px-3 py-2 text-sm border-2 border-blue-500 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                                          autoFocus
                                          disabled={savingSegment}
                                        />
                                        <div className="flex items-center gap-2">
                                          <button
                                            onClick={() => handleSaveSegmentEdit(conversation.conversation_id, index, segment.text)}
                                            disabled={savingSegment || editedSegmentText === segment.text}
                                            className="inline-flex items-center gap-1 px-3 py-1 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                          >
                                            <Save className="w-3 h-3" />
                                            {savingSegment ? 'Saving...' : 'Save'}
                                          </button>
                                          <button
                                            onClick={handleCancelSegmentEdit}
                                            disabled={savingSegment}
                                            className="inline-flex items-center gap-1 px-3 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-200 dark:bg-gray-600 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                          >
                                            <X className="w-3 h-3" />
                                            Cancel
                                          </button>
                                          {segmentEditError && (
                                            <span className="text-xs text-red-600 dark:text-red-400">{segmentEditError}</span>
                                          )}
                                        </div>
                                      </div>
                                    )
                                  } else {
                                    return (
                                      <span
                                        onClick={() => conversation.conversation_id && handleStartSegmentEdit(conversation.conversation_id, index, segment.text)}
                                        className="text-gray-900 dark:text-gray-100 ml-1 cursor-pointer hover:bg-yellow-100 dark:hover:bg-yellow-900/30 px-1 rounded transition-colors"
                                        title="Click to edit segment"
                                      >
                                        {segment.text}
                                      </span>
                                    )
                                  }
                                })()}
                              </div>
                            </div>
                            {!isPreview && <button onClick={(e) => openInsertForm(index, e)} className={insertBtnClass('bottom')}>+</button>}
                            </div>
                          )

                          // Insert divider after each segment
                          renderedSegments.push(renderInsertDivider(index))
                          })

                                  return renderedSegments
                                })()}
                              </div>
                            </div>
                          ) : (
                            <div className="text-sm text-gray-500 dark:text-gray-400 italic p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-600">
                              No transcript available
                            </div>
                          )}
                        </div>
                      )}
                    </>
                  )
                })()}
              </div>

              {/* Speaker Information - derived from segments */}
              {(() => {
                // Get unique speakers from segments
                const segments = conversation.segments || []
                const uniqueSpeakers = [...new Set(segments.map(s => s.speaker).filter(Boolean))]

                return uniqueSpeakers.length > 0 ? (
                  <div className="mt-4">
                    <h4 className="font-medium text-gray-900 dark:text-gray-100 mb-2">🎤 Identified Speakers:</h4>
                    <div className="flex flex-wrap gap-2">
                      {uniqueSpeakers.map((speaker: string, index: number) => (
                        <span
                          key={index}
                          className="px-2 py-1 bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 rounded-md text-sm"
                        >
                          {speaker}
                        </span>
                      ))}
                    </div>
                  </div>
                ) : null
              })()}


              {/* Debug info */}
              {debugMode && (
                <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                  <h4 className="font-medium text-gray-900 dark:text-gray-100 mb-2">🔧 Debug Info:</h4>
                  <div className="text-xs text-gray-600 dark:text-gray-400 space-y-1">
                    <div>Conversation ID: {conversation.conversation_id || 'N/A'}</div>
                    <div>Transcript Version Count: {conversation.transcript_version_count || 0}</div>
                    <div>Memory Version Count: {conversation.memory_version_count || 0}</div>
                    <div>Segment Count: {conversation.segment_count || 0}</div>
                    <div>Memory Count: {conversation.memory_count || 0}</div>
                    <div>Client ID: {conversation.client_id}</div>
                  </div>

                  {/* Raw Segments JSON */}
                  {conversation.segments && conversation.segments.length > 0 && (
                    <details className="mt-3 p-2 bg-gray-100 dark:bg-gray-800 rounded text-xs">
                      <summary className="cursor-pointer font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100">
                        Raw Segments ({conversation.segments.length})
                      </summary>
                      <pre className="mt-2 overflow-auto max-h-96 whitespace-pre-wrap text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 p-2 rounded border border-gray-200 dark:border-gray-700">
                        {JSON.stringify(conversation.segments, null, 2)}
                      </pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          ))
        )
        })()}
      </div>
    </div>
  )
}