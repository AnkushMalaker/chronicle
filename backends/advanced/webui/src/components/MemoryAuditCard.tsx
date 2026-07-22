import { useEffect, useState } from 'react'
import { ArrowUp, Loader2 } from 'lucide-react'
import { conversationsApi } from '../services/api'
import { Card, MetadataChip, StateBadge } from './ui'

interface MemoryAuditEntry {
  id: string
  operation: string
  note_path?: string | null
  // Backend-classified provenance label (see services/memory/audit.py).
  source_label?: string | null
  agent_mode?: boolean
  summary?: string | null
  created_at?: string | null
}

// Deletions keep a restrained danger tint in this audit list; other operations
// are plain metadata (mirrors MemoryLedger).
const isDestructiveOp = (op: string) => op === 'delete' || op === 'delete_all'

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
    <Card id="memory-history" className="bg-gray-50 dark:bg-gray-800/50 scroll-mt-6">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase">
          Memory History
        </h3>
        <a href="#transcript" className="inline-flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200">
          <ArrowUp className="h-3.5 w-3.5" /> Back to transcript
        </a>
      </div>

      {loading && (
        <div className="flex items-center space-x-2 text-sm text-gray-500 dark:text-gray-400">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span>Loading…</span>
        </div>
      )}

      {error && <div className="text-sm text-red-600 dark:text-red-400">{error}</div>}

      {!loading && !error && (
        <ul className="grid grid-cols-1 xl:grid-cols-2 gap-x-8 gap-y-3 text-sm">
          {entries.map((entry) => (
            <li key={entry.id} className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  {isDestructiveOp(entry.operation) ? (
                    <StateBadge tone="danger">{entry.operation}</StateBadge>
                  ) : (
                    <MetadataChip>{entry.operation}</MetadataChip>
                  )}
                  <span className="text-gray-900 dark:text-gray-100 break-all">
                    {entry.note_path || '(whole vault)'}
                  </span>
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {entry.source_label || 'system'}
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
    </Card>
  )
}
