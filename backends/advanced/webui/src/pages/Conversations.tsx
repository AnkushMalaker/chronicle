import { useState, useEffect, useRef, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { MessageSquare, RefreshCw, Calendar, User, Play, Pause, MoreVertical, RotateCcw, Zap, ChevronDown, ChevronUp, ChevronLeft, ChevronRight, Trash2, Save, X, AlertTriangle, Pencil, Search, Brain, Star, ArrowUpDown, Clock, UserX, Mic, Regex, ListFilter, Check } from 'lucide-react'
import { conversationsApi, annotationsApi, speakerApi } from '../services/api'
import { useConversations, useDeleteConversation, useReprocessTranscript, useReprocessMemory, useReprocessSpeakers, useReprocessOrphan, useToggleStar } from '../hooks/useConversations'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import { PlayheadTimeLabel } from '../components/audio/PlayheadWaveform'
import { useGaplessPlayer } from '../hooks/useGaplessPlayer'
import TranscriptEditor from '../components/transcript/TranscriptEditor'
import { Button, Checkbox } from '../components/ui'

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  detailed_summary?: string
  created_at?: string
  client_id: string
  segment_count?: number  // From list endpoint
  speakers?: string[]  // Unique speakers of the active version (from list endpoint, at a glance)
  audio_chunks_count?: number  // Number of MongoDB audio chunks
  audio_total_duration?: number  // Total duration in seconds
  duration_seconds?: number
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
  transcript_version_count?: number
  active_transcript_version_number?: number
  deleted?: boolean
  deletion_reason?: string
  deleted_at?: string
  always_persist?: boolean
  processing_status?: string
  failure_stage?: string
  is_orphan?: boolean
  starred?: boolean
  starred_at?: string
}


// Unknown and background labels are metadata, not enrolled people.
const isUnknownLabel = (name?: string): boolean => {
  if (!name || !name.trim()) return true
  const n = name.trim().toLowerCase()
  return ['noise', 'background speech'].includes(n) || /^unknown(?:[ _]speaker)?(?:[ _]*\d+)?$/.test(n)
}

const PAGE_SIZE = 20

const SORT_OPTIONS = [
  { label: 'Date (newest)', sortBy: 'created_at', sortOrder: 'desc' },
  { label: 'Date (oldest)', sortBy: 'created_at', sortOrder: 'asc' },
  { label: 'Duration (longest)', sortBy: 'audio_total_duration', sortOrder: 'desc' },
  { label: 'Title (A-Z)', sortBy: 'title', sortOrder: 'asc' },
] as const

export default function Conversations() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [debugMode, setDebugMode] = useState(false)
  const [starredOnly, setStarredOnly] = useState(false)
  const [hideUnknownSpeakers, setHideUnknownSpeakers] = useState(false)
  const [sortIdx, setSortIdx] = useState(0)
  const [page, setPage] = useState(0)

  const sortOption = SORT_OPTIONS[sortIdx]

  const {
    data: conversationsData,
    isLoading: loading,
    error: queryError,
    refetch,
  } = useConversations({
    includeUnprocessed: debugMode || undefined,
    starredOnly: starredOnly || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    sortBy: sortOption.sortBy,
    sortOrder: sortOption.sortOrder,
  })

  const conversations: Conversation[] = conversationsData?.conversations ?? []
  const totalConversations: number = conversationsData?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalConversations / PAGE_SIZE))

  // Stable query key matching what useConversations uses, for setQueryData calls
  const conversationsQueryKey = useMemo(() => ['conversations', {
    includeUnprocessed: debugMode || undefined,
    starredOnly: starredOnly || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    sortBy: sortOption.sortBy,
    sortOrder: sortOption.sortOrder,
  }], [debugMode, starredOnly, page, sortOption])
  const [actionError, setActionError] = useState<string | null>(null)
  const error = queryError?.message ?? actionError ?? null

  // Transcript expand/collapse state
  const [expandedTranscripts, setExpandedTranscripts] = useState<Set<string>>(new Set())
  // Detailed summary expand/collapse state
  const [expandedDetailedSummaries, setExpandedDetailedSummaries] = useState<Set<string>>(new Set())
  // Audio playback is owned by the app-wide gapless scheduler (Web Audio).
  // Only one conversation plays at a time across the whole list.
  const player = useGaplessPlayer()

  // Reprocessing state
  const [openDropdown, setOpenDropdown] = useState<string | null>(null)
  const [reprocessingTranscript, setReprocessingTranscript] = useState<Set<string>>(new Set())
  const [reprocessingMemory, setReprocessingMemory] = useState<Set<string>>(new Set())
  const [reprocessingSpeakers, setReprocessingSpeakers] = useState<Set<string>>(new Set())
  const [reprocessingOrphan, setReprocessingOrphan] = useState<Set<string>>(new Set())
  const [deletingConversation, setDeletingConversation] = useState<Set<string>>(new Set())

  // Enrolled speakers (passed to the shared transcript editor)
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<Array<{speaker_id: string, name: string}>>([])

  // Title editing state
  const [editingTitle, setEditingTitle] = useState<string | null>(null) // conversationId being edited
  const [editedTitle, setEditedTitle] = useState<string>('')
  const [savingTitle, setSavingTitle] = useState<boolean>(false)
  const [titleEditError, setTitleEditError] = useState<string | null>(null)

  // Search state (regex-only; semantic search was removed for performance reasons)
  const [searchQuery, setSearchQuery] = useState('')
  type SearchField = 'title' | 'summary' | 'speakers'
  const allSearchFields: SearchField[] = ['title', 'summary', 'speakers']
  const [searchFields, setSearchFields] = useState<SearchField[]>(allSearchFields)
  const [searchResults, setSearchResults] = useState<Conversation[] | null>(null)
  const [isSearching, setIsSearching] = useState(false)
  const [searchTotal, setSearchTotal] = useState(0)
  const [searchError, setSearchError] = useState<string | null>(null)
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const loadEnrolledSpeakers = async () => {
    try {
      const response = await speakerApi.getEnrolledSpeakers()
      setEnrolledSpeakers(response.data.speakers || [])
    } catch (err: any) {
      console.error('Failed to load enrolled speakers:', err)
    }
  }


  useEffect(() => {
    loadEnrolledSpeakers()
  }, [])

  // Refetch conversations when debug mode toggles (to include/exclude orphans)
  useEffect(() => {
    refetch()
  }, [debugMode])

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = () => setOpenDropdown(null)
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

  const allFieldsSelected = searchFields.length === allSearchFields.length

  const toggleSearchField = (field: SearchField) => {
    setSearchFields((current) =>
      current.includes(field)
        ? current.filter((selected) => selected !== field)
        : [...current, field]
    )
  }

  const runSearch = async (query: string, fields: SearchField[]) => {
    setIsSearching(true)
    try {
      const response = await conversationsApi.search(query, 50, 0, fields)
      setSearchResults(response.data.conversations ?? [])
      setSearchTotal(response.data.total ?? 0)
      setSearchError(response.data.error ?? null)
    } catch (err: any) {
      console.error('Search failed:', err)
      setSearchResults([])
      setSearchTotal(0)
      setSearchError(err?.response?.data?.error || 'Search failed')
    } finally {
      setIsSearching(false)
    }
  }

  // Regex search runs live, debounced.
  useEffect(() => {
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current)
    }

    const trimmed = searchQuery.trim()
    if (!trimmed) {
      setSearchResults(null)
      setSearchTotal(0)
      setSearchError(null)
      setIsSearching(false)
      return
    }

    if (searchFields.length === 0) {
      setSearchResults([])
      setSearchTotal(0)
      setSearchError(null)
      setIsSearching(false)
      return
    }

    setIsSearching(true)
    searchTimeoutRef.current = setTimeout(() => runSearch(trimmed, searchFields), 300)

    return () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current)
    }
  }, [searchQuery, searchFields])

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
  const toggleStarMutation = useToggleStar()

  const handleToggleStar = async (conversationId: string) => {
    try {
      await toggleStarMutation.mutateAsync(conversationId)
    } catch (err: any) {
      setActionError(err?.response?.data?.error || 'Failed to toggle star')
    }
  }

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
      queryClient.setQueryData(conversationsQueryKey, (old: any) => {
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
    const conversation = (searchResults ?? conversations).find(
      c => c.conversation_id === conversationId,
    )
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
        queryClient.setQueryData(conversationsQueryKey, (old: any) => {
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

  // Re-fetch a single conversation's full detail (segments) into the list cache — used
  // after the shared editor applies corrections (which create a new transcript version).
  const refreshConversationDetail = async (conversationId: string) => {
    try {
      const response = await conversationsApi.getById(conversationId)
      if (response.status === 200 && response.data.conversation) {
        queryClient.setQueryData(conversationsQueryKey, (old: any) => {
          if (!old) return old
          return {
            ...old,
            conversations: old.conversations.map((c: Conversation) =>
              c.conversation_id === conversationId ? { ...c, ...response.data.conversation } : c
            ),
          }
        })
      }
    } catch (err: any) {
      setActionError(`Failed to refresh conversation: ${err.message || 'Unknown error'}`)
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
        queryClient.setQueryData(conversationsQueryKey, (old: any) => {
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
        setSearchResults(prev => prev?.map(c =>
          c.conversation_id === conversationId
            ? { ...c, ...response.data.conversation }
            : c
        ) ?? null)
        // Expand the transcript (the editor loads its own annotations)
        setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch conversation details:', err)
      setActionError(`Failed to load transcript: ${err.message || 'Unknown error'}`)
    }
  }


  // Stop playback when leaving the list.
  useEffect(() => {
    return () => player.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        <Button variant="primary" size="md" onClick={() => { setActionError(null); refetch() }}>
          Try Again
        </Button>
      </div>
    )
  }

  return (
    <div>
      {/* Header with Search */}
      <div className="flex flex-col gap-4 mb-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center">
          <div className="flex items-center space-x-2">
            <MessageSquare className="h-6 w-6 text-blue-600 flex-shrink-0" />
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Conversations
            </h1>
          </div>
          <div className="flex flex-wrap items-center gap-2 sm:gap-4">
            <button
              onClick={() => { setStarredOnly(!starredOnly); setPage(0) }}
              className={`flex items-center space-x-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                starredOnly
                  ? 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300 border border-yellow-300 dark:border-yellow-700'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
              title={starredOnly ? 'Show all conversations' : 'Show only starred'}
            >
              <Star className={`h-4 w-4 ${starredOnly ? 'fill-yellow-500 text-yellow-500' : ''}`} />
              <span>Starred</span>
            </button>
            <button
              onClick={() => setHideUnknownSpeakers(!hideUnknownSpeakers)}
              className={`flex items-center space-x-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                hideUnknownSpeakers
                  ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-300 dark:border-blue-700'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
              title={hideUnknownSpeakers ? 'Show unknown speakers and noise' : 'Hide unknown speakers and noise'}
            >
              <UserX className="h-4 w-4" />
              <span>{hideUnknownSpeakers ? 'Unknown speakers hidden' : 'Hide unknown speakers'}</span>
            </button>
            <Checkbox
              checked={debugMode}
              onChange={(e) => { setDebugMode(e.target.checked); setPage(0) }}
              label="Debug Mode"
            />
            <Button
              variant="primary"
              size="md"
              onClick={() => refetch()}
              icon={<RefreshCw className="h-4 w-4" />}
            >
              Refresh
            </Button>
          </div>
        </div>

        {/* Search Bar */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 min-w-[180px]">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search conversations or people..."
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
          {/* Match mode: compact icons keep the search row scannable. */}
          <div className="inline-flex rounded-lg border border-gray-300 dark:border-gray-600 overflow-hidden">
            <button
              type="button"
              aria-label="Regex search"
              title="Regex search — case-insensitive text matching"
              className="flex h-9 w-9 items-center justify-center bg-blue-600 text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-inset"
            >
              <Regex className="h-4 w-4" />
            </button>
            <button
              type="button"
              disabled
              aria-label="Semantic search unavailable"
              title="Semantic search — unavailable because of performance issues"
              className="flex h-9 w-9 items-center justify-center border-l border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-400 dark:text-gray-500 cursor-not-allowed"
            >
              <Brain className="h-4 w-4" />
            </button>
          </div>
          {/* Search fields: Everything mirrors the three individual checkboxes. */}
          <div className="relative">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                setOpenDropdown(openDropdown === 'search-fields' ? null : 'search-fields')
              }}
              aria-haspopup="menu"
              aria-expanded={openDropdown === 'search-fields'}
              className="flex h-9 items-center gap-1.5 rounded-lg border border-gray-300 bg-white pl-2.5 pr-2 text-sm text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              <ListFilter className="h-4 w-4 text-gray-400" />
              <span>{allFieldsSelected ? 'Everything' : searchFields.length === 0 ? 'No fields' : `${searchFields.length} fields`}</span>
              <ChevronDown className={`h-3.5 w-3.5 text-gray-400 transition-transform ${openDropdown === 'search-fields' ? 'rotate-180' : ''}`} />
            </button>
            {openDropdown === 'search-fields' && (
              <div
                role="menu"
                onClick={(event) => event.stopPropagation()}
                className="absolute left-0 z-30 mt-1 w-52 rounded-lg border border-gray-200 bg-white p-1.5 shadow-lg dark:border-gray-600 dark:bg-gray-800"
              >
                {[
                  { key: 'all', label: 'Everything', selected: allFieldsSelected },
                  { key: 'title', label: 'Titles', selected: searchFields.includes('title') },
                  { key: 'summary', label: 'Summaries', selected: searchFields.includes('summary') },
                  { key: 'speakers', label: 'Speakers', selected: searchFields.includes('speakers') },
                ].map((option, index) => (
                  <button
                    key={option.key}
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={option.selected}
                    onClick={() => option.key === 'all'
                      ? setSearchFields(allFieldsSelected ? [] : allSearchFields)
                      : toggleSearchField(option.key as SearchField)}
                    className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-gray-700 ${index === 0 ? 'mb-1 border-b border-gray-100 pb-2 dark:border-gray-700' : ''}`}
                  >
                    <span className={`flex h-4 w-4 items-center justify-center rounded border ${option.selected ? 'border-blue-500 bg-blue-600 text-white' : 'border-gray-300 dark:border-gray-600'}`}>
                      {option.selected && <Check className="h-3 w-3" />}
                    </span>
                    <span>{option.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          {/* Sort Dropdown */}
          <div className="relative">
            <select
              value={sortIdx}
              onChange={(e) => { setSortIdx(Number(e.target.value)); setPage(0) }}
              className="appearance-none pl-8 pr-8 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent cursor-pointer"
            >
              {SORT_OPTIONS.map((opt, i) => (
                <option key={i} value={i}>{opt.label}</option>
              ))}
            </select>
            <ArrowUpDown className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400 pointer-events-none" />
          </div>
        </div>

        {/* Search status */}
        {searchQuery.trim() && (
          <div className="text-sm text-gray-500 dark:text-gray-400 space-y-1">
            {isSearching ? (
              <span className="flex items-center gap-1">
                <RefreshCw className="h-3 w-3 animate-spin" />
                Searching...
              </span>
            ) : searchError ? (
              <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                <AlertTriangle className="h-3.5 w-3.5" />
                {searchError}
              </span>
            ) : searchFields.length === 0 ? (
              <span>Select at least one field to search.</span>
            ) : searchResults !== null ? (
              <span>
                {searchTotal} result{searchTotal !== 1 ? 's' : ''}
                {` for “${searchQuery.trim()}”`}
              </span>
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
              onClick={(event) => {
                const target = event.target as HTMLElement
                if (target.closest('button, a, input, textarea, select, [role="button"]')) return
                navigate(`/conversations/${conversation.conversation_id}`)
              }}
              className={`rounded-lg p-6 border cursor-pointer ${
                conversation.is_orphan
                  ? 'bg-amber-50 dark:bg-amber-900/10 border-amber-300 dark:border-amber-700'
                  : 'bg-gray-50 dark:bg-gray-700 border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
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
                        {conversation.processing_status === 'failed'
                          ? (conversation.failure_stage === 'summarization' ? 'Summary generation failed' : 'Transcription failed') :
                         conversation.processing_status === 'active' ? 'Processing…' :
                         conversation.deleted ? `Deleted: ${conversation.deletion_reason}` :
                         conversation.processing_status || 'Pending'}
                        {conversation.audio_total_duration ? ` · ${Math.floor(conversation.audio_total_duration / 60)}:${Math.floor(conversation.audio_total_duration % 60).toString().padStart(2, '0')} audio` : ''}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => handleReprocessOrphan(conversation)}
                    disabled={reprocessingOrphan.has(conversation.conversation_id)}
                    className="flex items-center space-x-1 px-3 py-1.5 text-sm font-medium text-amber-700 dark:text-amber-300 bg-white dark:bg-transparent border border-amber-300 dark:border-amber-700 hover:bg-amber-50 dark:hover:bg-amber-900/20 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
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

              {/* Conversation Header */}
              <div className="flex justify-between items-start mb-4 gap-2">
                <div className="flex flex-col space-y-2 min-w-0">
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
                        className="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:underline flex items-center space-x-1"
                      >
                        <span>
                          {expandedDetailedSummaries.has(conversation.conversation_id) ? '▼' : '▶'} Detailed Summary
                        </span>
                      </button>

                      {/* Detailed Summary Content */}
                      {expandedDetailedSummaries.has(conversation.conversation_id) && conversation.detailed_summary && (
                        <div className="mt-2 p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg border border-gray-200 dark:border-gray-700 animate-in slide-in-from-top-2 duration-200">
                          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                            {conversation.detailed_summary}
                          </p>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Metadata */}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                    <ConversationVersionHeader
                      conversationId={conversation.conversation_id}
                      versionInfo={{
                        transcript_count: conversation.transcript_version_count || 0,
                        active_transcript_version: conversation.active_transcript_version,
                        active_transcript_version_number: conversation.active_transcript_version_number,
                      }}
                      onVersionChange={async () => {
                        try {
                          const response = await conversationsApi.getById(conversation.conversation_id!)
                          if (response.status === 200 && response.data.conversation) {
                            queryClient.setQueryData(conversationsQueryKey, (old: any) => {
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
                          refetch()
                        }
                      }}
                    />
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                      <Calendar className="h-4 w-4 flex-shrink-0" />
                      <span>{formatDate(conversation.created_at || '')}</span>
                    </div>
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400 min-w-0">
                      <User className="h-4 w-4 flex-shrink-0" />
                      <span className="truncate">{conversation.client_id}</span>
                    </div>
                    {/* Play pill inline (doubles as the duration readout) when there's audio;
                        otherwise a static duration. */}
                    {(conversation.audio_chunks_count && conversation.audio_chunks_count > 0) ? (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          player.togglePlay(conversation.conversation_id!, conversation.audio_total_duration || 0)
                        }}
                        className="inline-flex items-center gap-1.5 pl-1.5 pr-2.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                        title={player.isActive(conversation.conversation_id) && player.isPlaying ? 'Pause' : 'Play'}
                      >
                        {player.isActive(conversation.conversation_id) && player.isPlaying
                          ? <Pause className="h-3.5 w-3.5 text-blue-600" />
                          : <Play className="h-3.5 w-3.5 text-blue-600" />}
                        {player.isActive(conversation.conversation_id) ? (
                          <PlayheadTimeLabel
                            cid={conversation.conversation_id}
                            total={conversation.audio_total_duration}
                            className="text-xs font-mono tabular-nums text-gray-600 dark:text-gray-300"
                          />
                        ) : (
                          <span className="text-xs font-mono tabular-nums text-gray-600 dark:text-gray-300">
                            {conversation.audio_total_duration
                              ? `${Math.floor(conversation.audio_total_duration / 60)}:${Math.floor(conversation.audio_total_duration % 60).toString().padStart(2, '0')}`
                              : 'Audio'}
                          </span>
                        )}
                      </button>
                    ) : (() => {
                      const dur = conversation.duration_seconds || conversation.audio_total_duration
                      return dur && dur > 0 ? (
                        <div className="flex items-center space-x-1 text-sm text-gray-600 dark:text-gray-400">
                          <Clock className="h-3.5 w-3.5" />
                          <span>{Math.floor(dur / 60)}:{Math.floor(dur % 60).toString().padStart(2, '0')}</span>
                        </div>
                      ) : null
                    })()}
                  </div>

                  {/* Speakers at a glance (active version) */}
                  {conversation.speakers && conversation.speakers.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
                      <Mic className="h-3.5 w-3.5 text-gray-400 flex-shrink-0" />
                      {conversation.speakers.map((sp, i) => (
                        <span
                          key={i}
                          className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                            isUnknownLabel(sp)
                              ? 'bg-gray-100 dark:bg-gray-700/60 text-gray-400 dark:text-gray-500'
                              : 'bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-200'
                          }`}
                        >
                          {sp}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {/* Star + Hamburger Menu */}
                <div className="flex items-center space-x-1">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleToggleStar(conversation.conversation_id)
                    }}
                    className="p-1 rounded-full hover:bg-yellow-100 dark:hover:bg-yellow-900/30 transition-colors"
                    title={conversation.starred ? 'Unstar conversation' : 'Star conversation'}
                  >
                    <Star className={`h-5 w-5 ${conversation.starred ? 'fill-yellow-400 text-yellow-400' : 'text-gray-400 dark:text-gray-500'}`} />
                  </button>
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
              </div>

              {/* Transcript */}
              <div className="space-y-2" onClick={(event) => event.stopPropagation()}>
                {(() => {
                  // Get segments directly from conversation (returned by detail endpoint)
                  const segments = conversation.segments || []

                  return (
                    <>
                      {/* Transcript Header with Expand/Collapse */}
                      <button
                        type="button"
                        className="flex w-full items-center justify-between p-2 rounded-lg text-left hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors"
                        onClick={() => conversation.conversation_id && toggleTranscriptExpansion(conversation.conversation_id)}
                      >
                        <span className="font-medium text-gray-900 dark:text-gray-100">
                          Transcript {(segments.length > 0 || conversation.segment_count) && (
                            <span className="text-sm text-gray-500 dark:text-gray-400 ml-1">
                              ({segments.length || conversation.segment_count || 0} segments)
                            </span>
                          )}
                        </span>
                        <div className="flex items-center space-x-2">
                          {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) ? (
                            <ChevronUp className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          ) : (
                            <ChevronDown className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          )}
                        </div>
                      </button>

                      {/* Transcript Content - Conditionally Rendered */}
                      {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) && (
                        <div className="animate-in slide-in-from-top-2 duration-300 ease-out">
                          <TranscriptEditor
                            conversationId={conversation.conversation_id}
                            segments={segments}
                            duration={conversation.audio_total_duration}
                            hasAudio={!!conversation.audio_chunks_count && conversation.audio_chunks_count > 0}
                            showWaveform
                            enrolledSpeakers={enrolledSpeakers}
                            hideUnknownSpeakers={hideUnknownSpeakers}
                            onChanged={() => conversation.conversation_id && refreshConversationDetail(conversation.conversation_id)}
                          />
                        </div>
                      )}
                    </>
                  )
                })()}
              </div>

              {/* Debug info */}
              {debugMode && (
                <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                  <h4 className="font-medium text-gray-900 dark:text-gray-100 mb-2">🔧 Debug Info:</h4>
                  <div className="text-xs text-gray-600 dark:text-gray-400 space-y-1">
                    <div>Conversation ID: {conversation.conversation_id || 'N/A'}</div>
                    <div>Transcript Version Count: {conversation.transcript_version_count || 0}</div>
                    <div>Segment Count: {conversation.segment_count || 0}</div>
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

      {/* Pagination */}
      {!searchResults && totalPages > 1 && (
        <div className="flex items-center justify-between mt-6 px-2">
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {totalConversations} conversation{totalConversations !== 1 ? 's' : ''} total
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </button>
            <span className="text-sm text-gray-600 dark:text-gray-400 px-2">
              Page {page + 1} of {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
