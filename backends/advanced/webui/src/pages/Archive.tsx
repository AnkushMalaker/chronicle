import { useState, useEffect } from 'react'
import { Archive as ArchiveIcon, RefreshCw, Calendar, User, RotateCcw, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import { conversationsApi } from '../services/api'
import { authApi } from '../services/api'

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  created_at?: string
  client_id: string
  segment_count?: number
  memory_count?: number
  deleted?: boolean
  deletion_reason?: string
  deleted_at?: string
  transcript?: string
  segments?: Array<{
    text: string
    speaker: string
    start: number
    end: number
    confidence?: number
  }>
}

export default function Archive() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedTranscripts, setExpandedTranscripts] = useState<Set<string>>(new Set())
  const [restoringConversation, setRestoringConversation] = useState<Set<string>>(new Set())
  const [deletingConversation, setDeletingConversation] = useState<Set<string>>(new Set())
  const [isAdmin, setIsAdmin] = useState(false)

  const loadArchivedConversations = async () => {
    try {
      setLoading(true)
      // Include deleted conversations and filter for only deleted ones
      const response = await conversationsApi.getAll(true)
      const allConversations = response.data.conversations || []
      // Filter to show only deleted conversations
      const deletedConversations = allConversations.filter((conv: Conversation) => conv.deleted === true)
      setConversations(deletedConversations)
      setError(null)
    } catch (err: any) {
      setError(err.message || 'Failed to load archived conversations')
    } finally {
      setLoading(false)
    }
  }

  const checkAdminStatus = async () => {
    try {
      const response = await authApi.getMe()
      setIsAdmin(response.data.is_superuser || false)
    } catch {
      setIsAdmin(false)
    }
  }

  useEffect(() => {
    loadArchivedConversations()
    checkAdminStatus()
  }, [])

  const formatDate = (timestamp: number | string) => {
    if (typeof timestamp === 'string') {
      const isoString = timestamp.endsWith('Z') || timestamp.includes('+') || timestamp.includes('T') && timestamp.split('T')[1].includes('-')
        ? timestamp
        : timestamp + 'Z'
      return new Date(isoString).toLocaleString()
    }
    if (timestamp === 0) {
      return 'Unknown date'
    }
    return new Date(timestamp * 1000).toLocaleString()
  }

  const handleRestoreConversation = async (conversationId: string) => {
    try {
      setRestoringConversation(prev => new Set(prev).add(conversationId))

      const response = await conversationsApi.restore(conversationId)

      if (response.status === 200) {
        // Refresh archived conversations to show updated data
        await loadArchivedConversations()
      } else {
        setError(`Failed to restore conversation: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error restoring conversation: ${err.message || 'Unknown error'}`)
    } finally {
      setRestoringConversation(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  const handlePermanentDelete = async (conversationId: string) => {
    try {
      const confirmed = window.confirm(
        'Are you sure you want to PERMANENTLY delete this conversation? This action CANNOT be undone and will remove all data including audio.'
      )
      if (!confirmed) return

      setDeletingConversation(prev => new Set(prev).add(conversationId))

      const response = await conversationsApi.permanentDelete(conversationId)

      if (response.status === 200) {
        // Refresh archived conversations to show updated data
        await loadArchivedConversations()
      } else {
        setError(`Failed to permanently delete conversation: ${response.data?.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      setError(`Error permanently deleting conversation: ${err.message || 'Unknown error'}`)
    } finally {
      setDeletingConversation(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  const toggleTranscriptExpansion = async (conversationId: string) => {
    if (expandedTranscripts.has(conversationId)) {
      setExpandedTranscripts(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
      return
    }

    const conversation = conversations.find(c => c.conversation_id === conversationId)
    if (!conversation || !conversation.conversation_id) {
      return
    }

    if (conversation.segments && conversation.segments.length > 0) {
      setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      return
    }

    try {
      const response = await conversationsApi.getById(conversation.conversation_id)
      if (response.status === 200 && response.data.conversation) {
        setConversations(prev => prev.map(c =>
          c.conversation_id === conversationId
            ? { ...c, ...response.data.conversation }
            : c
        ))
        setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch conversation details:', err)
      setError(`Failed to load transcript: ${err.message || 'Unknown error'}`)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600 dark:text-gray-400">Loading archived conversations...</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center">
        <div className="text-red-600 dark:text-red-400 mb-4">{error}</div>
        <button
          onClick={loadArchivedConversations}
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
          <ArchiveIcon className="h-6 w-6 text-orange-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Archived Conversations
          </h1>
        </div>
        <button
          onClick={loadArchivedConversations}
          className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          <RefreshCw className="h-4 w-4" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Archive Info */}
      <div className="mb-4 p-3 bg-orange-50 dark:bg-orange-900/20 rounded-lg border border-orange-300 dark:border-orange-700">
        <p className="text-sm text-orange-800 dark:text-orange-300">
          <strong>Archive:</strong> Deleted conversations are stored here. You can restore them to active view or permanently delete them {isAdmin && '(admin only)'}.
        </p>
      </div>

      {/* Archived Conversations List */}
      <div className="space-y-6">
        {conversations.length === 0 ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-12">
            <ArchiveIcon className="h-12 w-12 mx-auto mb-4 opacity-50" />
            <p>No archived conversations</p>
          </div>
        ) : (
          conversations.map((conversation) => (
            <div
              key={conversation.conversation_id}
              className="rounded-lg p-6 border bg-red-50 dark:bg-red-900/20 border-red-300 dark:border-red-700"
            >
              {/* Deleted Conversation Banner */}
              <div className="mb-4 p-3 bg-red-100 dark:bg-red-900/40 rounded-lg border border-red-300 dark:border-red-700">
                <div className="flex items-start space-x-2">
                  <ArchiveIcon className="h-5 w-5 text-red-600 dark:text-red-400 mt-0.5 flex-shrink-0" />
                  <div className="flex-1">
                    <p className="font-semibold text-red-800 dark:text-red-300 text-sm">Archived Conversation</p>
                    <p className="text-xs text-red-700 dark:text-red-400 mt-1">
                      Reason: {conversation.deletion_reason === 'user_deleted'
                        ? 'User deleted'
                        : conversation.deletion_reason === 'no_meaningful_speech'
                        ? 'No meaningful speech detected'
                        : conversation.deletion_reason === 'audio_file_not_ready'
                        ? 'Audio file not saved (possible Bluetooth disconnect)'
                        : conversation.deletion_reason || 'Unknown'}
                    </p>
                    {conversation.deleted_at && (
                      <p className="text-xs text-red-600 dark:text-red-500 mt-1">
                        Deleted at: {formatDate(conversation.deleted_at)}
                      </p>
                    )}
                  </div>
                </div>
              </div>

              {/* Conversation Header */}
              <div className="flex justify-between items-start mb-4">
                <div className="flex flex-col space-y-2">
                  <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
                    {conversation.title || "Conversation"}
                  </h2>

                  {conversation.summary && (
                    <p className="text-sm text-gray-600 dark:text-gray-400 italic">
                      {conversation.summary}
                    </p>
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
                  </div>
                </div>

                {/* Action Buttons */}
                <div className="flex items-center space-x-2">
                  {conversation.conversation_id && (
                    <>
                      <button
                        onClick={() => handleRestoreConversation(conversation.conversation_id!)}
                        disabled={restoringConversation.has(conversation.conversation_id)}
                        className="flex items-center space-x-2 px-3 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Restore conversation to active view"
                      >
                        {restoringConversation.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="h-4 w-4" />
                        )}
                        <span>Restore</span>
                      </button>

                      {isAdmin && (
                        <button
                          onClick={() => handlePermanentDelete(conversation.conversation_id!)}
                          disabled={deletingConversation.has(conversation.conversation_id)}
                          className="flex items-center space-x-2 px-3 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                          title="Permanently delete (admin only)"
                        >
                          {deletingConversation.has(conversation.conversation_id) ? (
                            <RefreshCw className="h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="h-4 w-4" />
                          )}
                          <span>Permanent Delete</span>
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>

              {/* Transcript */}
              <div className="space-y-2">
                {(() => {
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
                                {segments.map((segment, index) => {
                                  const speaker = segment.speaker || 'Unknown'
                                  return (
                                    <div
                                      key={index}
                                      className="text-sm leading-relaxed flex items-start space-x-2 py-1 px-2 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
                                    >
                                      <div className="flex-1 min-w-0">
                                        <span className="font-medium text-blue-600 dark:text-blue-400">
                                          {speaker}:
                                        </span>
                                        <span className="text-gray-900 dark:text-gray-100 ml-1">
                                          {segment.text}
                                        </span>
                                      </div>
                                    </div>
                                  )
                                })}
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
            </div>
          ))
        )}
      </div>
    </div>
  )
}
