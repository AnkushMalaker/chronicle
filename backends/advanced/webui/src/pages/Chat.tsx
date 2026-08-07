import { useState, useEffect, useRef } from 'react'
import { MessageCircle, Send, Plus, Trash2, Brain, Clock, User, Bot, BookOpen, Loader2, Wrench } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { chatApi } from '../services/api'
import { useChatSessions, useChatMessages, useCreateChatSession, useDeleteChatSession, useExtractChatMemories } from '../hooks/useChat'
import { IconButton, MetadataChip } from '../components/ui'

interface ChatMessage {
  message_id: string
  session_id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  memories_used: string[]
}

interface MemoryContext {
  memory_ids: string[]
  memory_count: number
}

// Progress emitted by the backend while a turn runs. A turn on a local model can
// take minutes across several tool rounds, so this is what the user sees until
// the reply itself starts arriving.
interface TurnStatus {
  stage: 'thinking' | 'searching' | 'searched' | 'writing'
  round?: number
  max_rounds?: number
  query?: string
  note_count?: number
  found?: boolean
  failed?: boolean
}

function describeStatus(status: TurnStatus): string {
  switch (status.stage) {
    case 'thinking':
      return status.round && status.round > 1
        ? `Thinking (step ${status.round} of ${status.max_rounds})…`
        : 'Thinking…'
    case 'searching':
      return 'Searching your vault…'
    case 'searched':
      // "failed" and "found nothing" are different facts and must read differently.
      if (status.failed) return 'Vault search failed — results unknown'
      if (!status.found) return 'Vault search came back empty'
      return `Read ${status.note_count} ${status.note_count === 1 ? 'note' : 'notes'} from your vault`
    case 'writing':
      return 'Writing response…'
  }
}

export default function Chat() {
  const queryClient = useQueryClient()

  // TanStack Query hooks
  const { data: sessions = [], isLoading } = useChatSessions()
  const createSession = useCreateChatSession()
  const deleteSessionMutation = useDeleteChatSession()
  const extractMemories = useExtractChatMemories()

  // Local state
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [localMessages, setLocalMessages] = useState<ChatMessage[] | null>(null)
  const [inputMessage, setInputMessage] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [streamingMessage, setStreamingMessage] = useState('')
  const [turnStatus, setTurnStatus] = useState<TurnStatus | null>(null)
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null)
  const [turnElapsed, setTurnElapsed] = useState(0)
  const [memoryContext, setMemoryContext] = useState<MemoryContext | null>(null)
  const [showMemoryPanel, setShowMemoryPanel] = useState(false)
  const [extractionMessage, setExtractionMessage] = useState('')
  const [memoryLimit, setMemoryLimit] = useState(5)
  // Sessions sidebar is a slide-over on mobile (static from md up)
  const [showSessions, setShowSessions] = useState(false)

  // Query for messages of current session
  const { data: queryMessages } = useChatMessages(currentSessionId)

  // Sync query messages into local state (local state allows optimistic updates during streaming)
  useEffect(() => {
    if (queryMessages && !isSending) {
      setLocalMessages(queryMessages)
    }
  }, [queryMessages, isSending])

  const messages = localMessages ?? queryMessages ?? []

  // Derived: find current session object from sessions list
  const currentSession = sessions.find((s: any) => s.session_id === currentSessionId) ?? null

  // Auto-select first session when sessions load
  useEffect(() => {
    if (sessions.length > 0 && !currentSessionId) {
      setCurrentSessionId(sessions[0].session_id)
    }
  }, [sessions, currentSessionId])

  // Refs
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Auto-scroll to bottom (only when actively sending messages)
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    if (streamingMessage || isSending) {
      scrollToBottom()
    }
  }, [messages, streamingMessage, isSending])

  // Tick the elapsed counter while a turn is in flight. Local-model turns run for
  // minutes, so a moving number is what distinguishes "working" from "hung".
  useEffect(() => {
    if (turnStartedAt === null) {
      setTurnElapsed(0)
      return
    }
    setTurnElapsed(0)
    const id = setInterval(() => {
      setTurnElapsed(Math.floor((Date.now() - turnStartedAt) / 1000))
    }, 1000)
    return () => clearInterval(id)
  }, [turnStartedAt])

  const createNewSession = async () => {
    try {
      const newSession = await createSession.mutateAsync(undefined)
      setCurrentSessionId(newSession.session_id)
      setLocalMessages([])
      setMemoryContext(null)
    } catch (err: any) {
      console.error('Failed to create session:', err)
      setError('Failed to create new chat session')
    }
  }

  const deleteSession = async (sessionId: string) => {
    if (!confirm('Are you sure you want to delete this chat session?')) return

    try {
      await deleteSessionMutation.mutateAsync(sessionId)

      if (currentSessionId === sessionId) {
        const remaining = sessions.filter((s: any) => s.session_id !== sessionId)
        setCurrentSessionId(remaining[0]?.session_id ?? null)
        setLocalMessages([])
        setMemoryContext(null)
      }
    } catch (err: any) {
      console.error('Failed to delete session:', err)
      setError('Failed to delete chat session')
    }
  }

  const extractMemoriesFromChat = async () => {
    if (!currentSessionId) return

    setExtractionMessage('')

    try {
      const result = await extractMemories.mutateAsync(currentSessionId)

      if (result.success) {
        setExtractionMessage(`Successfully extracted ${result.count} memories from this chat`)
        setTimeout(() => setExtractionMessage(''), 5000)
      } else {
        setExtractionMessage(`${result.message || 'Failed to extract memories'}`)
        setTimeout(() => setExtractionMessage(''), 5000)
      }
    } catch (err: any) {
      console.error('Failed to extract memories:', err)
      setExtractionMessage('Failed to extract memories from chat')
      setTimeout(() => setExtractionMessage(''), 5000)
    }
  }

  const sendMessage = async () => {
    if (!inputMessage.trim() || isSending) return

    const messageText = inputMessage.trim()
    setInputMessage('')
    setIsSending(true)
    setStreamingMessage('')
    setMemoryContext(null)
    setTurnStatus(null)
    setTurnStartedAt(Date.now())

    try {
      // Create session if none exists
      let sessionId = currentSessionId
      if (!sessionId) {
        const newSession = await createSession.mutateAsync(undefined)
        setCurrentSessionId(newSession.session_id)
        sessionId = newSession.session_id
      }

      // Add user message to UI immediately
      const userMessage: ChatMessage = {
        message_id: `temp-${Date.now()}`,
        session_id: sessionId!,
        role: 'user',
        content: messageText,
        timestamp: new Date().toISOString(),
        memories_used: []
      }
      setLocalMessages(prev => [...(prev ?? []), userMessage])

      // Send message and handle streaming response
      const response = await chatApi.sendMessage(messageText, sessionId!, memoryLimit)

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }

      // Handle Server-Sent Events
      const reader = response.body?.getReader()
      if (!reader) {
        throw new Error('No response body')
      }

      const decoder = new TextDecoder()
      let buffer = ''
      let accumulatedContent = ''

      while (true) {
        const { done, value } = await reader.read()

        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // Keep incomplete line in buffer

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6) // Remove 'data: ' prefix

            if (data === '[DONE]') {
              break
            }

            try {
              const chunk = JSON.parse(data)

              // Handle OpenAI-style error object
              if (chunk.error) {
                throw new Error(chunk.error.message || 'Unknown error')
              }

              // Chronicle metadata (memory context, progress, session info)
              if (chunk.chronicle_metadata) {
                const meta = chunk.chronicle_metadata
                if (meta.memory_count !== undefined) {
                  setMemoryContext({ memory_ids: meta.memory_ids || [], memory_count: meta.memory_count })
                }
                if (meta.status) {
                  setTurnStatus(meta.status as TurnStatus)
                }
                if (meta.reset_content) {
                  // The text so far was a tool round narrating itself, and the
                  // backend has retracted it. Drop it rather than let it read as
                  // the answer while the next round runs.
                  accumulatedContent = ''
                  setStreamingMessage('')
                }
              }

              // Content delta
              const delta = chunk.choices?.[0]?.delta
              if (delta?.content) {
                accumulatedContent += delta.content
                setStreamingMessage(accumulatedContent)
              }

              // Finish reason
              if (chunk.choices?.[0]?.finish_reason === 'stop') {
                setStreamingMessage('')
                setTurnStatus(null)
              }
            } catch (parseError) {
              console.error('Failed to parse streaming event:', parseError)
            }
          }
        }
      }

      // Refresh sessions and messages via TanStack Query
      queryClient.invalidateQueries({ queryKey: ['chat', 'sessions'] })
      queryClient.invalidateQueries({ queryKey: ['chat', 'messages', sessionId] })

    } catch (err: any) {
      console.error('Failed to send message:', err)
      setError('Failed to send message: ' + err.message)
      setStreamingMessage('')
    } finally {
      setIsSending(false)
      setTurnStatus(null)
      setTurnStartedAt(null)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  const formatTime = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  return (
    <div className="flex h-[calc(100vh-12rem)] bg-gray-50 dark:bg-gray-900 relative">
      {/* Backdrop for the mobile sessions slide-over */}
      {showSessions && (
        <div
          className="md:hidden fixed inset-0 bg-black/50 z-30"
          onClick={() => setShowSessions(false)}
          aria-hidden="true"
        />
      )}
      {/* Sidebar — slide-over on mobile, static from md up */}
      <div className={`${
        showSessions ? 'flex' : 'hidden'
      } md:flex fixed md:static inset-y-0 left-0 z-40 w-80 max-w-[85%] bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 flex-col max-h-screen`}>
        {/* Header */}
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center space-x-2">
              <MessageCircle className="h-6 w-6 text-blue-600" />
              <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Chat</h1>
            </div>
            <IconButton label="New Chat" onClick={createNewSession}>
              <Plus className="h-5 w-5" />
            </IconButton>
          </div>
        </div>

        {/* Sessions List */}
        <div className="flex-1 overflow-y-auto">
          {isLoading ? (
            <div className="p-4 text-center text-gray-500">Loading sessions...</div>
          ) : sessions.length === 0 ? (
            <div className="p-4 text-center text-gray-500">
              No chat sessions yet.
              <br />
              <button
                onClick={createNewSession}
                className="mt-2 text-blue-600 hover:text-blue-700"
              >
                Start your first chat!
              </button>
            </div>
          ) : (
            <div className="p-2 space-y-1">
              {sessions.map((session) => (
                <div
                  key={session.session_id}
                  className={`group p-3 rounded-lg cursor-pointer transition-colors ${
                    currentSession?.session_id === session.session_id
                      ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-900 dark:text-blue-100'
                      : 'hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300'
                  }`}
                  onClick={() => { setCurrentSessionId(session.session_id); setShowSessions(false) }}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate">{session.title}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        {formatTime(session.updated_at)}
                      </div>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        deleteSession(session.session_id)
                      }}
                      className="opacity-0 group-hover:opacity-100 p-1 text-red-500 hover:text-red-700 transition-all"
                      title="Delete Session"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col">
        {currentSession ? (
          <>
            {/* Chat Header */}
            <div className="p-4 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  {/* Open sessions list on mobile */}
                  <IconButton
                    label="Show chat sessions"
                    onClick={() => setShowSessions(true)}
                    className="md:hidden -ml-2 flex-shrink-0"
                  >
                    <MessageCircle className="h-5 w-5" />
                  </IconButton>
                  <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
                    {currentSession.title}
                  </h2>
                </div>
                <div className="flex items-center space-x-2">
                  {/* Remember from Chat Button */}
                  <button
                    onClick={extractMemoriesFromChat}
                    disabled={extractMemories.isPending}
                    className="flex items-center space-x-2 px-3 py-1 rounded-full text-sm transition-colors border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                    title="Extract memories from this chat session"
                  >
                    {extractMemories.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <BookOpen className="h-4 w-4" />
                    )}
                    <span>{extractMemories.isPending ? 'Extracting...' : 'Remember from Chat'}</span>
                  </button>

                  {memoryContext && memoryContext.memory_count > 0 && (
                    <button
                      onClick={() => setShowMemoryPanel(!showMemoryPanel)}
                      className={`flex items-center space-x-2 px-3 py-1 rounded-full text-sm transition-colors ${
                        showMemoryPanel
                          ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300'
                      }`}
                      title="Toggle Memory Context"
                    >
                      <Brain className="h-4 w-4" />
                      <span>{memoryContext.memory_count} memories</span>
                    </button>
                  )}
                </div>
              </div>
            </div>

            {/* Memory Extraction Notification */}
            {extractionMessage && (
              <div className={`p-3 border-b border-gray-200 dark:border-gray-700 text-sm ${
                extractionMessage.startsWith('Successfully')
                  ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
                  : 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'
              }`}>
                {extractionMessage}
              </div>
            )}

            {/* Messages Area */}
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {messages.map((message) => (
                <div
                  key={message.message_id}
                  className={`flex items-start space-x-3 ${
                    message.role === 'user' ? 'flex-row-reverse space-x-reverse' : ''
                  }`}
                >
                  <div
                    className={`w-8 h-8 rounded-full flex items-center justify-center ${
                      message.role === 'user'
                        ? 'bg-blue-600 text-white'
                        : 'bg-gray-300 dark:bg-gray-600 text-gray-700 dark:text-gray-300'
                    }`}
                  >
                    {message.role === 'user' ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
                  </div>
                  <div
                    className={`max-w-2xl p-3 rounded-lg ${
                      message.role === 'user'
                        ? 'bg-blue-600 text-white'
                        : 'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700'
                    }`}
                  >
                    <div className="whitespace-pre-wrap">{message.content}</div>
                    <div
                      className={`text-xs mt-2 flex items-center space-x-2 ${
                        message.role === 'user'
                          ? 'text-blue-100'
                          : 'text-gray-500 dark:text-gray-400'
                      }`}
                    >
                      <Clock className="h-3 w-3" />
                      <span>{formatTime(message.timestamp)}</span>
                      {message.memories_used.length > 0 && (
                        <>
                          <Brain className="h-3 w-3" />
                          <span>{message.memories_used.length} memories used</span>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {/* In-flight turn: partial reply and/or what the backend is doing.
                  Shown from the moment the request is sent, because the first
                  token can be minutes away on a local model. */}
              {(isSending || streamingMessage) && (
                <div className="flex items-start space-x-3">
                  <div className="w-8 h-8 rounded-full bg-gray-300 dark:bg-gray-600 text-gray-700 dark:text-gray-300 flex items-center justify-center">
                    <Bot className="h-4 w-4" />
                  </div>
                  <div className="max-w-2xl p-3 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700">
                    {streamingMessage && (
                      <div className="whitespace-pre-wrap">{streamingMessage}</div>
                    )}
                    <div
                      className={`flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 ${streamingMessage ? 'mt-2' : ''}`}
                      aria-live="polite"
                    >
                      {turnStatus?.stage === 'searching' ? (
                        <BookOpen className="h-3 w-3 shrink-0 animate-pulse" />
                      ) : (
                        <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
                      )}
                      <span>{turnStatus ? describeStatus(turnStatus) : 'Sending…'}</span>
                      {turnElapsed > 0 && (
                        <span className="tabular-nums text-gray-400 dark:text-gray-500">
                          {turnElapsed}s
                        </span>
                      )}
                    </div>
                    {turnStatus?.stage === 'searching' && turnStatus.query && (
                      <div className="mt-1 text-xs italic text-gray-400 dark:text-gray-500 break-words">
                        “{turnStatus.query}”
                      </div>
                    )}
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* Input Area */}
            <div className="p-4 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
              {error && (
                <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-300 text-sm">
                  {error}
                  <button
                    onClick={() => setError(null)}
                    aria-label="Dismiss error"
                    title="Dismiss"
                    className="ml-2 rounded text-red-500 hover:text-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400"
                  >
                    ✕
                  </button>
                </div>
              )}

              <div className="flex items-end space-x-3">
                <div className="flex-1">
                  <textarea
                    ref={inputRef}
                    value={inputMessage}
                    onChange={(e) => setInputMessage(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Type your message..."
                    className="w-full p-3 border border-gray-300 dark:border-gray-600 rounded-lg resize-none bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    rows={1}
                    style={{ minHeight: '44px', maxHeight: '120px' }}
                    disabled={isSending}
                  />
                </div>
                <button
                  onClick={sendMessage}
                  disabled={!inputMessage.trim() || isSending}
                  className="p-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  title="Send Message (Enter)"
                >
                  <Send className="h-5 w-5" />
                </button>
              </div>

              <div className="mt-2 flex items-center justify-between">
                <div className="flex items-center space-x-4">
                  <MetadataChip
                    className="space-x-1.5"
                    title="The assistant searches your memory vault automatically when a question needs personal context."
                  >
                    <Wrench className="h-3.5 w-3.5" />
                    <span>Agentic memory</span>
                  </MetadataChip>
                </div>

                <div className="flex items-center space-x-2">
                  <Brain className="h-4 w-4 text-gray-500 dark:text-gray-400" />
                  <input
                    type="range"
                    min={0}
                    max={20}
                    value={memoryLimit}
                    onChange={(e) => setMemoryLimit(parseInt(e.target.value))}
                    className="w-24 h-1.5 accent-blue-600"
                  />
                  <span className="text-sm text-gray-600 dark:text-gray-400 w-20 text-right">
                    {memoryLimit} {memoryLimit === 1 ? 'memory' : 'memories'}
                  </span>
                </div>
              </div>
            </div>
          </>
        ) : (
          /* No Session Selected */
          <div className="flex-1 flex items-center justify-center bg-gray-50 dark:bg-gray-900">
            <div className="text-center">
              <MessageCircle className="h-16 w-16 text-gray-400 mx-auto mb-4" />
              <h3 className="text-xl font-semibold text-gray-700 dark:text-gray-300 mb-2">
                Welcome to Chat
              </h3>
              <p className="text-gray-500 dark:text-gray-400 mb-6">
                Start a new conversation or select an existing chat session
              </p>
              <button
                onClick={createNewSession}
                className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                Start New Chat
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Memory Panel (if enabled and has context) */}
      {showMemoryPanel && memoryContext && memoryContext.memory_count > 0 && (
        <div className="w-full max-w-[85%] sm:max-w-none sm:w-80 bg-white dark:bg-gray-800 border-l border-gray-200 dark:border-gray-700 p-4 absolute sm:static inset-y-0 right-0 z-40 sm:z-auto overflow-y-auto">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center space-x-2">
              <Brain className="h-5 w-5 text-blue-600" />
              <span>Memory Context</span>
            </h3>
            <button
              onClick={() => setShowMemoryPanel(false)}
              aria-label="Close memory context"
              title="Close"
              className="rounded text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
            >
              ✕
            </button>
          </div>
          <div className="text-sm text-gray-600 dark:text-gray-400">
            <p>Using {memoryContext.memory_count} relevant memories to enhance this conversation.</p>
            <div className="mt-4 space-y-2">
              {memoryContext.memory_ids.slice(0, 3).map((id, i) => (
                <div key={id} className="p-2 bg-gray-50 dark:bg-gray-700 rounded text-xs text-gray-600 dark:text-gray-300">
                  Memory {i + 1}
                </div>
              ))}
              {memoryContext.memory_ids.length > 3 && (
                <div className="text-xs text-gray-500">
                  +{memoryContext.memory_ids.length - 3} more memories
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
