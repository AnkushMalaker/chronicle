import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { conversationsApi } from '../services/api'

interface MemoryAuditEntry {
  id: string
  operation: string
  note_path?: string | null
  trigger?: string | null
  agent_mode?: boolean
  summary?: string | null
  created_at?: string | null
}

const OPERATION_STYLES: Record<string, string> = {
  create: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  update: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  delete: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  delete_all: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
}

// Human-readable label for what initiated a change.
const TRIGGER_LABELS: Record<string, string> = {
  memory_extraction: 'extraction',
  reprocess_after_speaker: 'speaker reprocess',
  obsidian_sync: 'Obsidian edit',
  delete_all: 'cleared',
}

function formatTime(value?: string | null): string {
  if (!value) return ''
  const d = new Date(value)
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
}

export default function MemoryAuditCard({ conversationId }: { conversationId: string }) {
  const [entries, setEntries] = useState<MemoryAuditEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        setLoading(true)
        const response = await conversationsApi.getMemoryAudit(conversationId)
        if (!cancelled) setEntries(response.data?.entries || [])
      } catch (err: any) {
        if (!cancelled) setError('Failed to load memory history')
        console.error('Failed to load memory audit:', err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [conversationId])

  // Nothing recorded and no error: stay quiet rather than show an empty card.
  if (!loading && !error && entries.length === 0) return null

  return (
    <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
        Memory History
      </h3>

      {loading && (
        <div className="flex items-center space-x-2 text-sm text-gray-500 dark:text-gray-400">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span>Loading…</span>
        </div>
      )}

      {error && <div className="text-sm text-red-600 dark:text-red-400">{error}</div>}

      {!loading && !error && (
        <ul className="space-y-2 text-sm">
          {entries.map((entry) => (
            <li key={entry.id} className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span
                    className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                      OPERATION_STYLES[entry.operation] ||
                      'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                    }`}
                  >
                    {entry.operation}
                  </span>
                  <span className="truncate text-gray-900 dark:text-gray-100">
                    {entry.note_path || '(whole vault)'}
                  </span>
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {entry.trigger ? (TRIGGER_LABELS[entry.trigger] || entry.trigger) : 'system'}
                  {entry.summary ? ` • ${entry.summary}` : ''}
                </div>
              </div>
              <span className="shrink-0 text-xs text-gray-400 dark:text-gray-500">
                {formatTime(entry.created_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
