import { useEffect, useRef, useCallback, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import { BACKEND_URL } from '../services/api'
import { emitWakeEvent } from './useWakeFeedback'

export type SSEStatus = 'connecting' | 'connected' | 'reconnecting' | 'error'

/**
 * Global SSE hook — connects once per authenticated session and invalidates
 * React Query caches when the backend pushes events.
 *
 * Returns the current connection status for UI indicators.
 */
export function useSSE(): SSEStatus {
  const { token } = useAuth()
  const queryClient = useQueryClient()
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const retriesRef = useRef(0)
  const [status, setStatus] = useState<SSEStatus>('connecting')

  const BASE_DELAY = 1000 // 1s, doubles each retry up to 30s

  const handleEvent = useCallback((eventType: string, data: unknown) => {
    switch (eventType) {
      case 'conversation.created':
      case 'conversation.updated':
      case 'conversation.completed':
        queryClient.invalidateQueries({ queryKey: ['conversations'] })
        queryClient.invalidateQueries({ queryKey: ['conversation'] })
        queryClient.invalidateQueries({ queryKey: ['queue'] })
        break

      case 'memory.processed':
        queryClient.invalidateQueries({ queryKey: ['conversations'] })
        queryClient.invalidateQueries({ queryKey: ['conversation'] })
        queryClient.invalidateQueries({ queryKey: ['memories'] })
        queryClient.invalidateQueries({ queryKey: ['conversationMemories'] })
        queryClient.invalidateQueries({ queryKey: ['queue'] })
        break

      case 'transcript.live': {
        const d = data as { conversation_id?: string; segments?: unknown[]; transcript?: string }
        if (d.conversation_id) {
          const patch = {
            segments: d.segments ?? [],
            transcript: d.transcript ?? '',
            segment_count: d.segments?.length ?? 0,
          }
          // Patch the conversation detail cache (['conversation', id])
          queryClient.setQueryData(
            ['conversation', d.conversation_id],
            (old: Record<string, unknown> | undefined) => (old ? { ...old, ...patch } : old)
          )
          // Patch the matching row in every cached conversations list (['conversations', opts])
          queryClient.setQueriesData(
            { queryKey: ['conversations'] },
            (old: { conversations?: Array<Record<string, unknown>> } | undefined) => {
              if (!old?.conversations) return old
              let changed = false
              const conversations = old.conversations.map((c) => {
                if (c.conversation_id !== d.conversation_id) return c
                changed = true
                return { ...c, ...patch }
              })
              return changed ? { ...old, conversations } : old
            }
          )
        }
        break
      }

      case 'wake.armed': {
        const d = data as { score?: number }
        emitWakeEvent({ type: 'armed', score: d.score })
        break
      }

      case 'wake.end_of_turn': {
        const d = data as { reason?: string; duration?: number }
        emitWakeEvent({ type: 'end_of_turn', reason: d.reason, duration: d.duration })
        break
      }

      case 'wake.command': {
        const d = data as { command?: string; reply?: string }
        emitWakeEvent({ type: 'command', command: d.command, reply: d.reply })
        break
      }

      case 'plugin.event':
      case 'job.progress':
      case 'jobs.queued':
      case 'session.started':
      case 'session.ended':
      case 'conversation.closed':
        queryClient.invalidateQueries({ queryKey: ['queue'] })
        break

      case 'connected':
        retriesRef.current = 0
        setStatus('connected')
        break
    }
  }, [queryClient])

  const connect = useCallback(async () => {
    if (!token) return

    setStatus(retriesRef.current === 0 ? 'connecting' : 'reconnecting')

    // Clean up previous connection
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    const url = `${BACKEND_URL}/api/events/stream?token=${encodeURIComponent(token)}`

    try {
      const response = await fetch(url, {
        signal: controller.signal,
        headers: { 'Accept': 'text/event-stream' },
      })

      if (!response.ok) {
        if (response.status === 401) {
          setStatus('error')
          return
        }
        throw new Error(`SSE connection failed: ${response.status}`)
      }

      const reader = response.body?.getReader()
      if (!reader) return
      readerRef.current = reader

      const decoder = new TextDecoder()
      let buffer = ''
      let currentEvent = 'message'

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))
              handleEvent(currentEvent, data)
            } catch {
              // Ignore malformed JSON
            }
            currentEvent = 'message'
          }
          // Lines starting with ':' are SSE comments (heartbeats) — ignore
        }
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') return
    }

    // Connection dropped — reconnect with backoff
    setStatus('reconnecting')
    const delay = Math.min(BASE_DELAY * Math.pow(2, retriesRef.current), 30000)
    retriesRef.current++
    reconnectTimeoutRef.current = setTimeout(connect, delay)
  }, [token, handleEvent])

  useEffect(() => {
    connect()

    return () => {
      abortRef.current?.abort()
      readerRef.current?.cancel()
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
    }
  }, [connect])

  return status
}
