import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Sparkles, RefreshCw, Trash2, VolumeX, Archive as ArchiveIcon, Loader2, AlertTriangle } from 'lucide-react'
import { dataCleaningApi, CleaningConversation } from '../services/api'

type SpeakerFilterState = 'include' | 'exclude'
type ArchiveReason = 'near_silent' | 'bad_speaker' | 'manual_cleanup'

// Persist filter inputs across navigation (e.g. opening a conversation detail
// page and clicking back) so the user doesn't lose their filters.
const FILTERS_STORAGE_KEY = 'data_cleaning_filters'

interface PersistedFilters {
  silenceThreshold: number
  minSilentPct: number
  minDuration: number
  speakerFilters: Record<string, SpeakerFilterState>
  archivedOnly: boolean
}

const DEFAULT_FILTERS: PersistedFilters = {
  silenceThreshold: -45,
  minSilentPct: 85,
  minDuration: 0,
  speakerFilters: {},
  archivedOnly: false,
}

function loadFilters(): PersistedFilters {
  try {
    const raw = sessionStorage.getItem(FILTERS_STORAGE_KEY)
    if (raw) return { ...DEFAULT_FILTERS, ...JSON.parse(raw) }
  } catch {
    // ignore malformed storage
  }
  return DEFAULT_FILTERS
}

const REASON_LABELS: Record<ArchiveReason, string> = {
  near_silent: 'Near-silent',
  bad_speaker: 'Bad speaker',
  manual_cleanup: 'Manual cleanup',
}

function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return '0s'
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

export default function DataCleaning() {
  // Filter controls — initialized from sessionStorage (lazy) so they survive
  // navigating to a conversation detail page and back.
  const [silenceThreshold, setSilenceThreshold] = useState(() => loadFilters().silenceThreshold)
  const [minSilentPct, setMinSilentPct] = useState(() => loadFilters().minSilentPct)
  const [minDuration, setMinDuration] = useState(() => loadFilters().minDuration)
  // Per-speaker tri-state filter: neutral (absent) → include → exclude → neutral
  const [speakerFilters, setSpeakerFilters] = useState<Record<string, SpeakerFilterState>>(() => loadFilters().speakerFilters)
  const [archivedOnly, setArchivedOnly] = useState(() => loadFilters().archivedOnly)

  // Data
  const [speakers, setSpeakers] = useState<string[]>([])
  const [rows, setRows] = useState<CleaningConversation[]>([])
  const [total, setTotal] = useState(0)
  const [scanCapped, setScanCapped] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // Status
  const [loading, setLoading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [archiving, setArchiving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadSpeakers = useCallback(async () => {
    try {
      const res = await dataCleaningApi.getSpeakers()
      setSpeakers(res.data.speakers || [])
    } catch {
      // non-fatal
    }
  }, [])

  const loadConversations = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const includeSpeakers = Object.entries(speakerFilters).filter(([, v]) => v === 'include').map(([k]) => k)
      const excludeSpeakers = Object.entries(speakerFilters).filter(([, v]) => v === 'exclude').map(([k]) => k)
      const res = await dataCleaningApi.getConversations({
        silence_threshold_dbfs: silenceThreshold,
        min_silent_fraction: archivedOnly ? 0 : minSilentPct / 100,
        min_duration: archivedOnly ? 0 : minDuration,
        include_speakers: includeSpeakers,
        exclude_speakers: excludeSpeakers,
        archived_only: archivedOnly,
        limit: 200,
        offset: 0,
      })
      setRows(res.data.conversations)
      setTotal(res.data.total)
      setScanCapped(res.data.scan_capped)
      setSelected(new Set())
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load conversations')
    } finally {
      setLoading(false)
    }
  }, [silenceThreshold, minSilentPct, minDuration, speakerFilters, archivedOnly])

  // Persist filter inputs whenever they change.
  useEffect(() => {
    try {
      sessionStorage.setItem(
        FILTERS_STORAGE_KEY,
        JSON.stringify({ silenceThreshold, minSilentPct, minDuration, speakerFilters, archivedOnly })
      )
    } catch {
      // ignore storage quota/availability errors
    }
  }, [silenceThreshold, minSilentPct, minDuration, speakerFilters, archivedOnly])

  useEffect(() => {
    loadSpeakers()
  }, [loadSpeakers])

  useEffect(() => {
    loadConversations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [archivedOnly])

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const runAnalysis = async () => {
    setAnalyzing(true)
    setError(null)
    setMessage('Queued audio analysis…')
    try {
      const res = await dataCleaningApi.analyze(undefined, false)
      const jobId = res.data.job_id
      if (!jobId) {
        setAnalyzing(false)
        setMessage('Analysis started')
        return
      }
      pollRef.current = setInterval(async () => {
        try {
          const s = await dataCleaningApi.getJobStatus(jobId)
          const status = s.data.status
          if (status === 'finished' || status === 'failed') {
            if (pollRef.current) clearInterval(pollRef.current)
            setAnalyzing(false)
            setMessage(status === 'finished' ? 'Analysis complete' : 'Analysis failed')
            if (status === 'finished') loadConversations()
          } else {
            setMessage(`Analyzing audio… (${status})`)
          }
        } catch {
          // keep polling; transient errors are tolerated
        }
      }, 2000)
    } catch (e: any) {
      setAnalyzing(false)
      setError(e?.response?.data?.error || 'Failed to start analysis')
    }
  }

  // Cycle a speaker through neutral → include → exclude → neutral
  const cycleSpeakerFilter = (s: string) => {
    setSpeakerFilters((prev) => {
      const next = { ...prev }
      const current = prev[s]
      if (!current) next[s] = 'include'
      else if (current === 'include') next[s] = 'exclude'
      else delete next[s]
      return next
    })
  }

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selected.size === rows.length) setSelected(new Set())
    else setSelected(new Set(rows.map((r) => r.conversation_id)))
  }

  const archiveSelected = async () => {
    if (selected.size === 0) return
    const hasExcludedSpeakers = Object.values(speakerFilters).some((v) => v === 'exclude')
    const reason: ArchiveReason = hasExcludedSpeakers
      ? 'bad_speaker'
      : minSilentPct > 0
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
      const res = await dataCleaningApi.archive(Array.from(selected), reason)
      setMessage(`Archived audio for ${res.data.archived}/${res.data.total} conversation(s)`)
      await loadConversations()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to archive')
    } finally {
      setArchiving(false)
    }
  }

  const allSelected = rows.length > 0 && selected.size === rows.length

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <Sparkles className="h-6 w-6 text-blue-600" />
          <div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Data Cleaning</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Find background / near-silent or mis-attributed recordings and archive their audio.
            </p>
          </div>
        </div>
        <div className="flex items-center space-x-2">
          <button
            onClick={runAnalysis}
            disabled={analyzing || archivedOnly}
            className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {analyzing ? <Loader2 className="h-4 w-4 animate-spin" /> : <VolumeX className="h-4 w-4" />}
            <span>{analyzing ? 'Analyzing…' : 'Analyze audio'}</span>
          </button>
          <button
            onClick={loadConversations}
            disabled={loading}
            className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 transition-colors"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Apply filters</span>
          </button>
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
          Cleanup candidates
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
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 bg-gray-50 dark:bg-gray-900/40 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
          {/* Amplitude controls */}
          <div className="space-y-4">
            <div>
              <label className="flex justify-between text-sm font-medium text-gray-700 dark:text-gray-200">
                <span>Silence threshold</span>
                <span className="text-gray-500 dark:text-gray-400">{silenceThreshold} dBFS</span>
              </label>
              <input
                type="range" min={-90} max={-10} step={1}
                value={silenceThreshold}
                onChange={(e) => setSilenceThreshold(Number(e.target.value))}
                className="w-full mt-1"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400">Windows quieter than this count as silent.</p>
            </div>
            <div>
              <label className="flex justify-between text-sm font-medium text-gray-700 dark:text-gray-200">
                <span>Min. silent fraction</span>
                <span className="text-gray-500 dark:text-gray-400">{minSilentPct}%</span>
              </label>
              <input
                type="range" min={0} max={100} step={1}
                value={minSilentPct}
                onChange={(e) => setMinSilentPct(Number(e.target.value))}
                className="w-full mt-1"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400">Only show conversations at least this silent.</p>
            </div>
            <div>
              <label className="flex justify-between text-sm font-medium text-gray-700 dark:text-gray-200">
                <span>Min. duration</span>
                <span className="text-gray-500 dark:text-gray-400">{minDuration}s</span>
              </label>
              <input
                type="range" min={0} max={600} step={5}
                value={minDuration}
                onChange={(e) => setMinDuration(Number(e.target.value))}
                className="w-full mt-1"
              />
            </div>
          </div>

          {/* Speaker controls — per-speaker tri-state filter */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-700 dark:text-gray-200">Speakers</span>
              {Object.keys(speakerFilters).length > 0 && (
                <button
                  onClick={() => setSpeakerFilters({})}
                  className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
                >
                  Clear
                </button>
              )}
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Click a speaker to cycle: <span className="text-blue-600 dark:text-blue-400">include</span> →{' '}
              <span className="text-red-600 dark:text-red-400">exclude</span> → off. Kept conversations contain at
              least one included speaker (if any) and none of the excluded.
            </p>
            <div className="flex flex-wrap gap-2 max-h-40 overflow-y-auto">
              {speakers.length === 0 && (
                <span className="text-xs text-gray-400">No speaker labels found.</span>
              )}
              {speakers.map((s) => {
                const state = speakerFilters[s]
                return (
                  <button
                    key={s}
                    onClick={() => cycleSpeakerFilter(s)}
                    title={
                      state === 'include'
                        ? 'Including — click to exclude'
                        : state === 'exclude'
                          ? 'Excluding — click to clear'
                          : 'Click to include'
                    }
                    className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                      state === 'include'
                        ? 'bg-blue-100 border-blue-400 text-blue-700 dark:bg-blue-900 dark:text-blue-100 dark:border-blue-600'
                        : state === 'exclude'
                          ? 'bg-red-100 border-red-400 text-red-700 line-through dark:bg-red-900/40 dark:text-red-200 dark:border-red-600'
                          : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
                    }`}
                  >
                    {s}
                  </button>
                )
              })}
            </div>
          </div>
        </div>
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

      {/* Bulk action bar */}
      {!archivedOnly && (
        <div className="flex items-center justify-between">
          <div className="text-sm text-gray-500 dark:text-gray-400">
            {total} match{total === 1 ? '' : 'es'} · {selected.size} selected
          </div>
          <button
            onClick={archiveSelected}
            disabled={selected.size === 0 || archiving}
            className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {archiving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            <span>Archive selected (delete audio)</span>
          </button>
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
        <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
          <thead className="bg-gray-50 dark:bg-gray-900/40">
            <tr className="text-left text-gray-500 dark:text-gray-400">
              {!archivedOnly && (
                <th className="px-3 py-2">
                  <input type="checkbox" checked={allSelected} onChange={toggleSelectAll} />
                </th>
              )}
              <th className="px-3 py-2">Conversation</th>
              <th className="px-3 py-2">Date</th>
              <th className="px-3 py-2">Duration</th>
              {!archivedOnly ? (
                <>
                  <th className="px-3 py-2">Silent %</th>
                  <th className="px-3 py-2">Mean dBFS</th>
                  <th className="px-3 py-2">Peak dBFS</th>
                  <th className="px-3 py-2">Speakers</th>
                </>
              ) : (
                <th className="px-3 py-2">Reason</th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
            {rows.length === 0 && (
              <tr>
                <td colSpan={archivedOnly ? 4 : 9} className="px-3 py-8 text-center text-gray-400">
                  {loading ? 'Loading…' : 'No conversations match the current filters.'}
                </td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.conversation_id} className="text-gray-700 dark:text-gray-200">
                {!archivedOnly && (
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(r.conversation_id)}
                      onChange={() => toggleSelect(r.conversation_id)}
                    />
                  </td>
                )}
                <td className="px-3 py-2 max-w-xs truncate">
                  <Link to={`/conversations/${r.conversation_id}`} className="text-blue-600 dark:text-blue-400 hover:underline">
                    {r.title || r.conversation_id.slice(0, 8)}
                  </Link>
                  <div className="text-xs text-gray-400">{r.client_id}</div>
                </td>
                <td className="px-3 py-2 whitespace-nowrap">{formatDate(r.created_at)}</td>
                <td className="px-3 py-2 whitespace-nowrap">{formatDuration(r.duration_seconds)}</td>
                {!archivedOnly ? (
                  <>
                    <td className="px-3 py-2">
                      {r.silent_fraction !== null ? `${Math.round(r.silent_fraction * 100)}%` : <span className="text-gray-400">—</span>}
                    </td>
                    <td className="px-3 py-2">{r.mean_dbfs !== null ? r.mean_dbfs.toFixed(1) : <span className="text-gray-400">—</span>}</td>
                    <td className="px-3 py-2">{r.peak_dbfs !== null ? r.peak_dbfs.toFixed(1) : <span className="text-gray-400">—</span>}</td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        {r.speakers.length === 0 && <span className="text-gray-400 text-xs">none</span>}
                        {r.speakers.map((s) => (
                          <span key={s} className="px-1.5 py-0.5 rounded text-xs bg-gray-100 dark:bg-gray-700">{s}</span>
                        ))}
                      </div>
                    </td>
                  </>
                ) : (
                  <td className="px-3 py-2">
                    <span className="px-2 py-0.5 rounded text-xs bg-gray-100 dark:bg-gray-700">
                      {r.archive_reason || 'archived'}
                    </span>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!archivedOnly && (
        <p className="text-xs text-gray-400">
          Tip: run <strong>Analyze audio</strong> first to populate amplitude metrics. Conversations
          showing “—” haven’t been analyzed yet and won’t match a silent-fraction filter.
        </p>
      )}
    </div>
  )
}
