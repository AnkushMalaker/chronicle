import { useState, useEffect } from 'react'
import { MessageSquare, RefreshCw, User, Clock, ChevronDown, ChevronUp } from 'lucide-react'
import { VerticalTimeline, VerticalTimelineElement } from 'react-vertical-timeline-component'
import 'react-vertical-timeline-component/style.min.css'
import { conversationsApi } from '../services/api'

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  detailed_summary?: string
  created_at?: string
  client_id: string
  segment_count?: number
  memory_count?: number
  duration_seconds?: number
  has_memory?: boolean
  transcript?: string
  segments?: Array<{
    text: string
    speaker: string
    start: number
    end: number
    confidence?: number
  }>
  active_transcript_version?: string
  active_memory_version?: string
  transcript_version_count?: number
  memory_version_count?: number
  deleted?: boolean
  deletion_reason?: string
  deleted_at?: string
}

interface ConversationCardProps {
  conversation: Conversation
  formatDuration: (seconds: number) => string
}

function ConversationCard({ conversation, formatDuration }: ConversationCardProps) {
  const [isExpanded, setIsExpanded] = useState(false)

  return (
    <div>
      {/* Card Header - Always visible */}
      <div
        className="cursor-pointer hover:opacity-90 transition-opacity"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <div className="flex justify-between items-start">
          <h3 className="text-xl font-semibold text-gray-900 flex-1">
            {conversation.title || 'Conversation'}
          </h3>
          {isExpanded ? (
            <ChevronUp className="h-5 w-5 text-gray-500 flex-shrink-0 ml-2" />
          ) : (
            <ChevronDown className="h-5 w-5 text-gray-500 flex-shrink-0 ml-2" />
          )}
        </div>

        {conversation.summary && (
          <p className="text-sm text-gray-700 mt-2">
            {conversation.summary}
          </p>
        )}

        <div className="flex flex-wrap gap-2 mt-3 text-xs">
          <span className="inline-flex items-center gap-1 px-2 py-1 bg-gray-100 rounded text-gray-700">
            <User className="h-3 w-3" />
            {conversation.client_id}
          </span>
          {conversation.segment_count !== undefined && (
            <span className="px-2 py-1 bg-blue-100 text-blue-700 rounded">
              {conversation.segment_count} segments
            </span>
          )}
          {conversation.memory_count !== undefined && conversation.memory_count > 0 && (
            <span className="px-2 py-1 bg-purple-100 text-purple-700 rounded">
              {conversation.memory_count} memories
            </span>
          )}
          {conversation.duration_seconds && (
            <span className="inline-flex items-center gap-1 px-2 py-1 bg-green-100 text-green-700 rounded">
              <Clock className="h-3 w-3" />
              {formatDuration(conversation.duration_seconds)}
            </span>
          )}
        </div>
      </div>

      {/* Expanded Details */}
      {isExpanded && (
        <div className="mt-4 pt-4 border-t border-gray-200 space-y-3">
          {/* Detailed Summary */}
          {conversation.detailed_summary && (
            <div>
              <h4 className="text-sm font-semibold text-gray-700 mb-1">Detailed Summary</h4>
              <p className="text-sm text-gray-600">{conversation.detailed_summary}</p>
            </div>
          )}

          {/* Transcript */}
          {conversation.transcript && (
            <div>
              <h4 className="text-sm font-semibold text-gray-700 mb-1">Transcript</h4>
              <div className="text-sm text-gray-600 bg-gray-50 rounded p-3 max-h-60 overflow-y-auto">
                {conversation.transcript}
              </div>
            </div>
          )}

          {/* Segments */}
          {conversation.segments && conversation.segments.length > 0 && (
            <div>
              <h4 className="text-sm font-semibold text-gray-700 mb-2">Segments ({conversation.segments.length})</h4>
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {conversation.segments.map((segment, idx) => (
                  <div key={idx} className="text-sm bg-gray-50 rounded p-2">
                    <div className="flex justify-between items-start mb-1">
                      <span className="font-medium text-gray-700">{segment.speaker}</span>
                      <span className="text-xs text-gray-500">
                        {Math.floor(segment.start)}s - {Math.floor(segment.end)}s
                      </span>
                    </div>
                    <p className="text-gray-600">{segment.text}</p>
                    {segment.confidence && (
                      <span className="text-xs text-gray-400">
                        Confidence: {(segment.confidence * 100).toFixed(1)}%
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Metadata */}
          <div className="grid grid-cols-2 gap-2 text-xs">
            {conversation.conversation_id && (
              <div>
                <span className="font-medium text-gray-700">ID:</span>{' '}
                <span className="text-gray-600 font-mono">{conversation.conversation_id.slice(0, 8)}...</span>
              </div>
            )}
            {conversation.active_transcript_version && (
              <div>
                <span className="font-medium text-gray-700">Transcript Version:</span>{' '}
                <span className="text-gray-600">{conversation.active_transcript_version}</span>
              </div>
            )}
            {conversation.transcript_version_count && (
              <div>
                <span className="font-medium text-gray-700">Total Versions:</span>{' '}
                <span className="text-gray-600">{conversation.transcript_version_count}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ConversationsTimeline() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadConversations = async () => {
    try {
      setLoading(true)
      // Exclude deleted conversations from timeline view
      const response = await conversationsApi.getAll(false)
      const conversationsList = response.data.conversations || []
      setConversations(conversationsList)
      setError(null)
    } catch (err: any) {
      setError(err.message || 'Failed to load conversations')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadConversations()
  }, [])

  const formatDate = (timestamp: number | string): Date => {
    if (typeof timestamp === 'string') {
      const isoString = timestamp.endsWith('Z') || timestamp.includes('+') || timestamp.includes('T') && timestamp.split('T')[1].includes('-')
        ? timestamp
        : timestamp + 'Z'
      return new Date(isoString)
    }
    if (timestamp === 0) {
      return new Date()
    }
    return new Date(timestamp * 1000)
  }

  const formatDuration = (seconds: number) => {
    const minutes = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${minutes}:${secs.toString().padStart(2, '0')}`
  }

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
            Conversations Timeline
          </h1>
        </div>
        <button
          onClick={loadConversations}
          className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          <RefreshCw className="h-4 w-4" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Timeline */}
      {conversations.length === 0 ? (
        <div className="text-center text-gray-500 dark:text-gray-400 py-12">
          <MessageSquare className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>No conversations found</p>
        </div>
      ) : (
        <VerticalTimeline layout="2-columns" lineColor="#2563eb">
          {conversations.map((conv) => {
            const date = formatDate(conv.created_at || '')

            return (
              <VerticalTimelineElement
                key={conv.conversation_id}
                date={date.toLocaleString('en-US', {
                  month: 'short',
                  day: 'numeric',
                  year: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit'
                })}
                iconStyle={{ background: '#2563eb', color: '#fff' }}
                icon={<MessageSquare />}
                contentStyle={{
                  background: conv.deleted ? '#fee2e2' : '#fff',
                  color: '#1f2937',
                  boxShadow: '0 3px 0 #ddd'
                }}
                contentArrowStyle={{ borderRight: '7px solid  #fff' }}
              >
                <ConversationCard
                  conversation={conv}
                  formatDuration={formatDuration}
                />
              </VerticalTimelineElement>
            )
          })}
        </VerticalTimeline>
      )}
    </div>
  )
}
