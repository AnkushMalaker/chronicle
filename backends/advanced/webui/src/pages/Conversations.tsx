import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { MessageSquare, RefreshCw, Calendar, User, Play, Pause, MoreVertical, RotateCcw, Zap, ChevronDown, ChevronUp, Trash2, Save, X, Check } from 'lucide-react'
import { conversationsApi, annotationsApi, speakerApi, BACKEND_URL } from '../services/api'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import { getStorageKey } from '../utils/storage'
import { WaveformDisplay } from '../components/audio/WaveformDisplay'
import SpeakerNameDropdown from '../components/SpeakerNameDropdown'

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
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [debugMode, setDebugMode] = useState(false)

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

  // Unified apply state
  const [applyingAnnotations, setApplyingAnnotations] = useState<Set<string>>(new Set())

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
    console.log(`🎯 handleSeek called: conversationId=${conversationId}, time=${time.toFixed(2)}s`);

    const audioElement = audioRefs.current[conversationId];

    if (!audioElement) {
      console.error(`❌ Audio element not found for conversation ${conversationId}`);
      console.log('Available audio refs:', Object.keys(audioRefs.current));
      return;
    }

    console.log(`📍 Audio element found, readyState=${audioElement.readyState}, paused=${audioElement.paused}`);

    // Check if audio is ready for seeking (readyState >= 1 means HAVE_METADATA)
    if (audioElement.readyState < 1) {
      console.warn(`⚠️ Audio not ready for seeking (readyState=${audioElement.readyState})`);
      // Try again after metadata loads
      audioElement.addEventListener('loadedmetadata', () => {
        console.log('✅ Metadata loaded, retrying seek');
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
        console.log(`✅ Seek complete: requested=${time.toFixed(2)}s, actual=${audioElement.currentTime.toFixed(2)}s`);

        if (Math.abs(audioElement.currentTime - time) > 1.0) {
          console.error(`⚠️ Seek failed! Requested ${time.toFixed(2)}s but got ${audioElement.currentTime.toFixed(2)}s`);
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

  const loadConversations = async () => {
    try {
      setLoading(true)
      // Exclude deleted conversations from main view
      const response = await conversationsApi.getAll(false)
      // API now returns a flat list with client_id as a field
      const conversationsList = response.data.conversations || []
      setConversations(conversationsList)
      setError(null)
    } catch (err: any) {
      setError(err.message || 'Failed to load conversations')
    } finally {
      setLoading(false)
    }
  }

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

  const handleSpeakerChange = async (conversationId: string, segmentIndex: number, originalSpeaker: string, newSpeaker: string, segmentStartTime: number) => {
    try {
      await annotationsApi.createDiarizationAnnotation({
        conversation_id: conversationId,
        segment_index: segmentIndex,
        original_speaker: originalSpeaker,
        corrected_speaker: newSpeaker,
        segment_start_time: segmentStartTime,
      })
      
      // Temporarily add new speaker name to enrolledSpeakers if it doesn't exist
      // This makes it immediately available in all dropdowns without requiring a backend reload
      setEnrolledSpeakers(prev => {
        const speakerExists = prev.some(speaker => speaker.name === newSpeaker)
        if (!speakerExists) {
          // Generate a temporary speaker_id for in-memory use
          const tempSpeakerId = `temp_${Date.now()}_${newSpeaker.replace(/\s+/g, '_')}`
          return [...prev, { speaker_id: tempSpeakerId, name: newSpeaker }]
        }
        return prev
      })
      
      // Reload annotations for this conversation
      await loadDiarizationAnnotations(conversationId)
    } catch (err: any) {
      console.error('Failed to create annotation:', err)
      setError('Failed to create speaker annotation')
    }
  }

  const handleApplyAllAnnotations = async (conversationId: string) => {
    try {
      setApplyingAnnotations(prev => new Set(prev).add(conversationId))
      setOpenDropdown(null)

      const response = await annotationsApi.applyAllAnnotations(conversationId)

      if (response.status === 200) {
        const data = response.data
        console.log(`Applied ${data.diarization_count} diarization and ${data.transcript_count} transcript annotations`)

        // Refresh conversation to show new version
        await loadConversations()

        // Reload annotations (should be empty now)
        await loadDiarizationAnnotations(conversationId)
        await loadTranscriptAnnotations(conversationId)
      } else {
        setError(`Failed to apply annotations: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error applying annotations: ${err.message || 'Unknown error'}`)
    } finally {
      setApplyingAnnotations(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  useEffect(() => {
    loadConversations()
    loadEnrolledSpeakers()
  }, [])

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = () => setOpenDropdown(null)
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

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

  const handleReprocessTranscript = async (conversation: Conversation) => {
    try {
      if (!conversation.conversation_id) {
        setError('Cannot reprocess transcript: Conversation ID is missing. This conversation may be from an older format.')
        return
      }

      setReprocessingTranscript(prev => new Set(prev).add(conversation.conversation_id!))
      setOpenDropdown(null)

      const response = await conversationsApi.reprocessTranscript(conversation.conversation_id)

      if (response.status === 200) {
        // Refresh conversations to show updated data
        await loadConversations()
      } else {
        setError(`Failed to start transcript reprocessing: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error starting transcript reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      if (conversation.conversation_id) {
        setReprocessingTranscript(prev => {
          const newSet = new Set(prev)
          newSet.delete(conversation.conversation_id!)
          return newSet
        })
      }
    }
  }

  const handleReprocessMemory = async (conversation: Conversation, transcriptVersionId?: string) => {
    try {
      if (!conversation.conversation_id) {
        setError('Cannot reprocess memory: Conversation ID is missing. This conversation may be from an older format.')
        return
      }

      setReprocessingMemory(prev => new Set(prev).add(conversation.conversation_id!))
      setOpenDropdown(null)

      // For now, use active transcript version. In future, this could be selected from UI
      const response = await conversationsApi.reprocessMemory(conversation.conversation_id, transcriptVersionId || 'active')

      if (response.status === 200) {
        // Refresh conversations to show updated data
        await loadConversations()
      } else {
        setError(`Failed to start memory reprocessing: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error starting memory reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      if (conversation.conversation_id) {
        setReprocessingMemory(prev => {
          const newSet = new Set(prev)
          newSet.delete(conversation.conversation_id!)
          return newSet
        })
      }
    }
  }

  const handleReprocessSpeakers = async (conversation: Conversation) => {
    try {
      if (!conversation.conversation_id) {
        setError('Cannot reprocess speakers: Conversation ID is missing. This conversation may be from an older format.')
        return
      }

      setReprocessingSpeakers(prev => new Set(prev).add(conversation.conversation_id!))
      setOpenDropdown(null)

      const response = await conversationsApi.reprocessSpeakers(
        conversation.conversation_id,
        'active'  // Use active transcript version as source
      )

      if (response.status === 200) {
        // Refresh conversations to show new version with updated speakers
        await loadConversations()
      } else {
        setError(`Failed to start speaker reprocessing: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error starting speaker reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      if (conversation.conversation_id) {
        setReprocessingSpeakers(prev => {
          const newSet = new Set(prev)
          newSet.delete(conversation.conversation_id!)
          return newSet
        })
      }
    }
  }

  const handleDeleteConversation = async (conversationId: string) => {
    try {
      const confirmed = window.confirm('Are you sure you want to delete this conversation? This action cannot be undone.')
      if (!confirmed) return

      setDeletingConversation(prev => new Set(prev).add(conversationId))
      setOpenDropdown(null)

      const response = await conversationsApi.delete(conversationId)

      if (response.status === 200) {
        // Refresh conversations to show updated data
        await loadConversations()
      } else {
        setError(`Failed to delete conversation: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error deleting conversation: ${err.message || 'Unknown error'}`)
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

      // Create annotation (NOT applied immediately)
      await annotationsApi.createTranscriptAnnotation({
        conversation_id: conversationId,
        segment_index: segmentIndex,
        original_text: originalText,
        corrected_text: editedSegmentText
      })

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
        // Update the conversation in state with detailed_summary
        setConversations(prev => prev.map(c =>
          c.conversation_id === conversationId
            ? { ...c, detailed_summary: response.data.conversation.detailed_summary }
            : c
        ))
        // Expand the detailed summary
        setExpandedDetailedSummaries(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch detailed summary:', err)
      setError(`Failed to load detailed summary: ${err.message || 'Unknown error'}`)
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
        // Update the conversation in state with full data
        setConversations(prev => prev.map(c =>
          c.conversation_id === conversationId
            ? { ...c, ...response.data.conversation }
            : c
        ))
        // Load diarization annotations for this conversation
        await loadDiarizationAnnotations(conversationId)
        // Load transcript annotations for this conversation
        await loadTranscriptAnnotations(conversationId)
        // Expand the transcript
        setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch conversation details:', err)
      setError(`Failed to load transcript: ${err.message || 'Unknown error'}`)
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
      console.log('Creating segment audio element with URL:', audioUrl);
      console.log('Segment range:', segment.start, 'to', segment.end, '(duration:', segment.end - segment.start, 'seconds)');
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
    console.log('Playing segment:', segment.start, 'to', segment.end);
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
          onClick={loadConversations}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          Try Again
        </button>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <MessageSquare className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Latest Conversations
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
            onClick={loadConversations}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            <RefreshCw className="h-4 w-4" />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Conversations List */}
      <div className="space-y-6">
        {conversations.length === 0 ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-12">
            <MessageSquare className="h-12 w-12 mx-auto mb-4 opacity-50" />
            <p>No conversations found</p>
          </div>
        ) : (
          conversations.map((conversation) => (
            <div
              key={conversation.conversation_id}
              className="rounded-lg p-6 border bg-gray-50 dark:bg-gray-700 border-gray-200 dark:border-gray-600"
            >
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
                        setConversations(prev => prev.map(c =>
                          c.conversation_id === conversation.conversation_id
                            ? { ...c, ...response.data.conversation }
                            : c
                        ))
                      }
                    } catch (err: any) {
                      console.error('Failed to refresh conversation:', err)
                      // Fallback to full reload on error
                      loadConversations()
                    }
                  }}
                />

              {/* Conversation Header */}
              <div className="flex justify-between items-start mb-4">
                <div className="flex flex-col space-y-2">
                  {/* Conversation Title */}
                  <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
                    {conversation.title || "Conversation"}
                  </h2>

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

                        const diarPending = diarAnnotations.filter(a => !a.processed).length
                        const transcriptPending = transcriptAnnots.filter(a => !a.processed).length
                        const totalPending = diarPending + transcriptPending

                        if (totalPending === 0) return null

                        return (
                          <button
                            onClick={() => handleApplyAllAnnotations(conversation.conversation_id!)}
                            disabled={!conversation.conversation_id || applyingAnnotations.has(conversation.conversation_id!)}
                            className="w-full text-left px-4 py-2 text-sm text-blue-700 dark:text-blue-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
                            title={`Apply ${diarPending} speaker and ${transcriptPending} text corrections`}
                          >
                            {conversation.conversation_id && applyingAnnotations.has(conversation.conversation_id!) ? (
                              <RefreshCw className="h-4 w-4 animate-spin" />
                            ) : (
                              <Check className="h-4 w-4" />
                            )}
                            <span>
                              Apply Changes ({totalPending})
                              {diarPending > 0 && transcriptPending > 0 && (
                                <span className="text-xs ml-1 text-gray-500">
                                  ({diarPending} speaker, {transcriptPending} text)
                                </span>
                              )}
                            </span>
                          </button>
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

                                  // Render the transcript
                                  return segments.map((segment, index) => {
                          const speaker = segment.speaker || 'Unknown'
                          // Use conversation_id for unique segment IDs
                          const segmentId = `${conversation.conversation_id}-${index}`
                          const isPlaying = playingSegment === segmentId
                          const hasAudio = !!conversation.audio_chunks_count && conversation.audio_chunks_count > 0
                          const isEditing = editingSegment === segmentId

                          return (
                            <div
                              key={index}
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

                                {/* Speaker Name - Clickable Dropdown for Annotation */}
                                {(() => {
                                  const conversationAnnotations = diarizationAnnotations.get(conversation.conversation_id!) || []
                                  const annotation = conversationAnnotations.find(a => a.segment_index === index && !a.processed)
                                  const speakerColor = speakerColorMap[speaker]

                                  // Always show dropdown, but use corrected speaker if annotation exists
                                  // This allows users to edit annotations even after creating them
                                  const currentSpeaker = annotation ? annotation.corrected_speaker : speaker
                                  const originalSpeaker = annotation ? annotation.original_speaker : speaker

                                  return (
                                    <span className="inline-flex items-center space-x-1">
                                      {annotation && (
                                        <span className="text-xs bg-orange-100 dark:bg-orange-900 text-orange-600 dark:text-orange-300 px-2 py-0.5 rounded" title="Pending annotation">
                                          Pending
                                        </span>
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
                                        speakerColor={annotation ? 'text-green-600 dark:text-green-400' : speakerColor}
                                      />
                                      <span>:</span>
                                    </span>
                                  )
                                })()}

                                {/* Segment Text - Show pending edit indicator or editable */}
                                {(() => {
                                  const transcriptAnnots = transcriptAnnotations.get(conversation.conversation_id!) || []
                                  const textAnnotation = transcriptAnnots.find(
                                    a => a.segment_index === index && !a.processed
                                  )

                                  if (textAnnotation && !isEditing) {
                                    // Show pending text edit - corrected text is clickable like normal text
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
                                      </span>
                                    )
                                  } else if (isEditing) {
                                    // Show edit textarea
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
                                    // Show normal text (clickable to edit)
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
                          )
                          })
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
        )}
      </div>
    </div>
  )
}