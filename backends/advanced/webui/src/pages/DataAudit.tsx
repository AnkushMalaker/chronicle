import { useCallback, useEffect, useState } from 'react'
import { Sparkles, Archive as ArchiveIcon, AlertTriangle } from 'lucide-react'
import { dataAuditApi, AuditConversation } from '../services/api'
import { useJobPolling } from '../hooks/useJobPolling'
import AuditFilterBar from '../components/dataAudit/AuditFilterBar'
import {
  AUDIT_FILTERS,
  SpeakerFilterState,
  defaultFilterValues,
} from '../components/dataAudit/filters'
import AuditToolbar from '../components/dataAudit/AuditToolbar'
import AuditTable from '../components/dataAudit/AuditTable'
import SplitConversationModal from '../components/dataAudit/SplitConversationModal'
import MergePreviewModal from '../components/dataAudit/MergePreviewModal'
import ExportModal from '../components/dataAudit/ExportModal'

type ArchiveReason = 'near_silent' | 'bad_speaker' | 'manual_cleanup'

// Persist filter inputs across navigation (e.g. opening a conversation detail
// page and clicking back) so the user doesn't lose their filters.
// v2: generic per-filter values keyed by AUDIT_FILTERS registry keys.
const FILTERS_STORAGE_KEY = 'data_audit_filters_v2'
const ARCHIVED_VIEW_STORAGE_KEY = 'data_audit_archived_view'
const SELECTION_STORAGE_KEY = 'data_audit_selection'

function loadSelection(): Set<string> {
  try {
    const raw = sessionStorage.getItem(SELECTION_STORAGE_KEY)
    if (raw) return new Set(JSON.parse(raw))
  } catch {
    // ignore malformed storage
  }
  return new Set()
}

function loadFilters(): Record<string, unknown> {
  const defaults = defaultFilterValues()
  try {
    const raw = sessionStorage.getItem(FILTERS_STORAGE_KEY)
    if (raw) return { ...defaults, ...JSON.parse(raw) }
  } catch {
    // ignore malformed storage
  }
  return defaults
}

function loadArchivedView(): boolean {
  try {
    return sessionStorage.getItem(ARCHIVED_VIEW_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

const REASON_LABELS: Record<ArchiveReason, string> = {
  near_silent: 'Speech-free',
  bad_speaker: 'Bad speaker',
  manual_cleanup: 'Manual cleanup',
}

export default function DataAudit() {
  // Filter values keyed by AUDIT_FILTERS registry keys — initialized from
  // sessionStorage (lazy) so they survive navigating to a conversation detail
  // page and back.
  const [filters, setFilters] = useState<Record<string, unknown>>(() => loadFilters())
  const [archivedOnly, setArchivedOnly] = useState(() => loadArchivedView())

  // Data
  const [speakers, setSpeakers] = useState<string[]>([])
  const [rows, setRows] = useState<AuditConversation[]>([])
  const [total, setTotal] = useState(0)
  const [scanCapped, setScanCapped] = useState(false)
  // null until the first listing response tells us how many conversations
  // still lack cached VAD analysis (0 disables the Analyze button).
  const [unanalyzedCount, setUnanalyzedCount] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<string>>(() => loadSelection())

  // Status
  const [loading, setLoading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [archiving, setArchiving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Modals
  const [splitTarget, setSplitTarget] = useState<AuditConversation | null>(null)
  const [mergeTargets, setMergeTargets] = useState<AuditConversation[] | null>(null)
  const [exportOpen, setExportOpen] = useState(false)

  const { pollJob } = useJobPolling()

  const loadConversations = useCallback(async (overrideFilters?: Record<string, unknown>) => {
    setLoading(true)
    setError(null)
    try {
      // Each registry filter contributes its own query params.
      const values = overrideFilters ?? filters
      const params: Record<string, unknown> = {
        archived_only: archivedOnly,
        limit: 200,
        offset: 0,
      }
      for (const def of AUDIT_FILTERS) {
        Object.assign(params, def.toParams(values[def.key] ?? def.defaultValue))
      }
      const res = await dataAuditApi.getConversations(params)
      setRows(res.data.conversations)
      setTotal(res.data.total)
      setScanCapped(res.data.scan_capped)
      setUnanalyzedCount(res.data.unanalyzed_count)
      // Speaker filter options come from the same scan, so the list only
      // contains speakers actually present in the current view.
      setSpeakers(res.data.speakers || [])
      // Prune (not clear) the selection: keep selected rows that are still
      // visible so navigation/refresh doesn't lose a curation in progress.
      const visible = new Set(res.data.conversations.map((c) => c.conversation_id))
      setSelected((prev) => new Set([...prev].filter((id) => visible.has(id))))
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load conversations')
    } finally {
      setLoading(false)
    }
  }, [filters, archivedOnly])

  // Persist selection whenever it changes.
  useEffect(() => {
    try {
      sessionStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify([...selected]))
    } catch {
      // ignore storage quota/availability errors
    }
  }, [selected])

  // Persist filter inputs whenever they change.
  useEffect(() => {
    try {
      sessionStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters))
      sessionStorage.setItem(ARCHIVED_VIEW_STORAGE_KEY, String(archivedOnly))
    } catch {
      // ignore storage quota/availability errors
    }
  }, [filters, archivedOnly])

  useEffect(() => {
    loadConversations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archivedOnly])

  const runAnalysis = async () => {
    setAnalyzing(true)
    setError(null)
    setMessage('Queued audio analysis…')
    try {
      const res = await dataAuditApi.analyze(undefined, false)
      const jobId = res.data.job_id
      if (!jobId) {
        setAnalyzing(false)
        setMessage('Analysis started')
        return
      }
      const status = await pollJob(jobId, (s, progress) =>
        setMessage(
          progress?.message
            ? `Analyzing audio… ${progress.message}`
            : progress?.total != null
              ? `Analyzing audio… ${progress.done ?? 0}/${progress.total}`
              : `Analyzing audio… (${s})`
        )
      )
      setAnalyzing(false)
      setMessage(status === 'finished' ? 'Analysis complete' : 'Analysis failed')
      if (status === 'finished') loadConversations()
    } catch (e: any) {
      setAnalyzing(false)
      setError(e?.response?.data?.error || 'Failed to start analysis')
    }
  }

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Bulk set used by shift-click range selection in the table.
  const selectMany = (ids: string[], value: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => (value ? next.add(id) : next.delete(id)))
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selected.size === rows.length) setSelected(new Set())
    else setSelected(new Set(rows.map((r) => r.conversation_id)))
  }

  const archiveSelected = async () => {
    if (selected.size === 0) return
    const speakerRules = (filters.speakers || {}) as Record<string, SpeakerFilterState>
    const speech = (filters.speech || {}) as { max?: number }
    const hasExcludedSpeakers = Object.values(speakerRules).some((v) => v === 'exclude')
    const reason: ArchiveReason = hasExcludedSpeakers
      ? 'bad_speaker'
      : (speech.max ?? 100) < 100
        ? 'near_silent'
        : 'manual_cleanup'

    const ok = window.confirm(
      `Permanently delete the AUDIO for ${selected.size} conversation(s)?\n\n` +
        `Reason: ${REASON_LABELS[reason]}\n\n` +
        `The audio bytes will be deleted to reclaim storage. A metadata stub ` +
        `(date, duration, reason) is kept so you know something was recorded. ` +
        `This cannot be undone.`
    )
    if (!ok) return

    setArchiving(true)
    setError(null)
    try {
      const res = await dataAuditApi.archive(Array.from(selected), reason)
      setMessage(`Archived audio for ${res.data.archived}/${res.data.total} conversation(s)`)
      await loadConversations()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to archive')
    } finally {
      setArchiving(false)
    }
  }

  const selectedRows = rows.filter((r) => selected.has(r.conversation_id))
  // Merge needs 2+ conversations from the same device; the server is
  // authoritative on adjacency (filtered-out conversations may sit between).
  const mergeEligible =
    selectedRows.length >= 2 && new Set(selectedRows.map((r) => r.client_id)).size === 1

  const onOperationDone = (msg: string) => {
    setMessage(msg)
    loadConversations()
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center space-x-3">
        <Sparkles className="h-6 w-6 text-blue-600" />
        <div>
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Data Audit</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Inspect recordings: find speech-free or mis-attributed audio, split long recordings at
            silence gaps, merge adjacent conversations, and archive audio.
          </p>
        </div>
      </div>

      {/* View toggle */}
      <div className="flex items-center space-x-1 border-b border-gray-200 dark:border-gray-700">
        <button
          onClick={() => setArchivedOnly(false)}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            !archivedOnly
              ? 'border-blue-600 text-blue-600 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
          }`}
        >
          Conversations
        </button>
        <button
          onClick={() => setArchivedOnly(true)}
          className={`flex items-center space-x-1 px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            archivedOnly
              ? 'border-blue-600 text-blue-600 dark:text-blue-400'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
          }`}
        >
          <ArchiveIcon className="h-4 w-4" />
          <span>Archived stubs</span>
        </button>
      </div>

      {/* Filters (hidden in archived view) */}
      {!archivedOnly && (
        <AuditFilterBar
          filters={filters}
          onChangeFilter={(key, value) => setFilters((prev) => ({ ...prev, [key]: value }))}
          onResetFilter={(key) => {
            // Compute next state synchronously so the refetch can't see a
            // stale filters snapshot.
            const next = {
              ...filters,
              [key]: AUDIT_FILTERS.find((d) => d.key === key)?.defaultValue,
            }
            setFilters(next)
            loadConversations(next)
          }}
          onApply={() => loadConversations()}
          ctx={{ speakers }}
          loading={loading}
        />
      )}

      {/* Messages */}
      {message && (
        <div className="text-sm px-4 py-2 rounded-lg bg-blue-50 dark:bg-blue-900/30 text-blue-800 dark:text-blue-200">
          {message}
        </div>
      )}
      {error && (
        <div className="flex items-center space-x-2 text-sm px-4 py-2 rounded-lg bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-200">
          <AlertTriangle className="h-4 w-4" />
          <span>{error}</span>
        </div>
      )}
      {scanCapped && !archivedOnly && (
        <div className="flex items-center space-x-2 text-sm px-4 py-2 rounded-lg bg-yellow-50 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-200">
          <AlertTriangle className="h-4 w-4" />
          <span>Showing a capped working set — narrow filters or archive in batches to see the rest.</span>
        </div>
      )}

      {/* Toolbar */}
      {!archivedOnly && (
        <AuditToolbar
          total={total}
          selectedCount={selected.size}
          mergeEligible={mergeEligible}
          unanalyzedCount={unanalyzedCount}
          analyzing={analyzing}
          archiving={archiving}
          onAnalyze={runAnalysis}
          onMerge={() => setMergeTargets(selectedRows)}
          onArchive={archiveSelected}
          onExport={() => setExportOpen(true)}
        />
      )}

      {/* Table */}
      <AuditTable
        rows={rows}
        loading={loading}
        archivedView={archivedOnly}
        selected={selected}
        onToggleSelect={toggleSelect}
        onSelectMany={selectMany}
        onToggleSelectAll={toggleSelectAll}
        onSplit={(row) => setSplitTarget(row)}
      />

      {!archivedOnly && (
        <p className="text-xs text-gray-400">
          Tip: run <strong>Analyze audio</strong> first to populate speech metrics. Conversations
          showing “—” haven’t been analyzed yet and won’t match a speech-fraction filter.
        </p>
      )}

      {/* Modals */}
      {splitTarget && (
        <SplitConversationModal
          conversation={splitTarget}
          onClose={() => setSplitTarget(null)}
          onDone={onOperationDone}
        />
      )}
      {mergeTargets && (
        <MergePreviewModal
          conversations={mergeTargets}
          onClose={() => setMergeTargets(null)}
          onDone={onOperationDone}
        />
      )}
      {exportOpen && (
        <ExportModal selected={selectedRows} onClose={() => setExportOpen(false)} />
      )}
    </div>
  )
}
