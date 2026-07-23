import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  Download,
  HelpCircle,
  Loader2,
  PackageOpen,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import {
  AuditConversation,
  ExportRecord,
  ScreenConversationReport,
  ScreenResult,
  dataAuditApi,
} from '../../services/api'
import { Alert, Button, Modal, Textarea } from '../../components/ui'
import { useJobPolling } from '../../hooks/useJobPolling'
import { formatDate, formatDuration } from './format'

interface Props {
  selected: AuditConversation[]
  onClose: () => void
}

// Survives closing the modal: a running screen job is re-attached on reopen.
const SCREEN_JOB_KEY = 'dataAudit.screenJob'
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function formatBytes(bytes?: number): string {
  if (!bytes) return '—'
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Hover help: a small ? icon whose native tooltip carries the explanation. */
function Hint({ text }: { text: string }) {
  return (
    <span title={text} className="inline-flex cursor-help" aria-label={text}>
      <HelpCircle className="h-3.5 w-3.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" />
    </span>
  )
}

// A flagged segment is keyed by conversation + its segment index.
const segKey = (cid: string, index: number) => `${cid}:${index}`

// 16 kHz mono 16-bit PCM — what exported WAV clips contain (pre-zip).
const WAV_BYTES_PER_SECOND = 32000

/** Estimated dataset impact of the privacy screen, given the current toggles. */
export interface ScreenImpact {
  baselineSeconds: number // dataset duration without the screen (estimate in clips mode)
  withheldSeconds: number // currently-checked flagged segments
  withheldCount: number
  byCategory: { category: string; count: number; seconds: number }[]
  estimated: boolean // clips mode: baseline derived from speech %, not exact
}

interface Progress {
  done: number
  total: number
  message: string
}

export default function ExportModal({ selected, onClose }: Props) {
  // Export parameters
  const [mode, setMode] = useState<'clips' | 'full'>('clips')
  const [padSeconds, setPadSeconds] = useState(1.0)
  const [speechThreshold, setSpeechThreshold] = useState(0.5)
  const [mergeGap, setMergeGap] = useState(3.0)

  // Privacy screen
  const [screenEnabled, setScreenEnabled] = useState(true)
  const [policy, setPolicy] = useState('')
  const [screening, setScreening] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)
  const [screenResult, setScreenResult] = useState<ScreenResult | null>(null)
  // Flagged segments the user has chosen to withhold (default: all flagged).
  const [excluded, setExcluded] = useState<Set<string>>(new Set())

  // Run state
  const [exporting, setExporting] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [latestExportId, setLatestExportId] = useState<string | null>(null)

  // Previous exports
  const [exports, setExports] = useState<ExportRecord[]>([])
  const [loadingList, setLoadingList] = useState(false)

  const { pollJob } = useJobPolling()
  // Stops in-flight polling loops from updating state after the modal closes.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // Order-independent signature of the current selection.
  const idsSig = selected
    .map((c) => c.conversation_id)
    .sort()
    .join(',')

  const loadExports = useCallback(async () => {
    setLoadingList(true)
    try {
      const res = await dataAuditApi.listExports()
      setExports(res.data.exports)
    } catch {
      // non-fatal; the list section just stays empty
    } finally {
      setLoadingList(false)
    }
  }, [])

  useEffect(() => {
    loadExports()
  }, [loadExports])

  // Prefill the editable policy with the server default.
  useEffect(() => {
    dataAuditApi
      .getSensitivityPolicy()
      .then((res) => setPolicy(res.data.policy))
      .catch(() => setPolicy(''))
  }, [])

  // Poll a screen job to completion, surfacing per-conversation progress and
  // the final result. Shared by a fresh screen and by recovery-on-reopen.
  const attachToScreenJob = useCallback(async (jobId: string) => {
    setScreening(true)
    setError(null)
    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const res = await dataAuditApi.getJobResult<ScreenResult>(jobId)
        if (!mounted.current) return
        const { status: st, meta, result } = res.data
        const bp = meta?.batch_progress
        if (bp) {
          setProgress({
            done: bp.done ?? 0,
            total: bp.total ?? selected.length,
            message: bp.message ?? '',
          })
        }
        if (st === 'finished') {
          localStorage.removeItem(SCREEN_JOB_KEY)
          if (result) {
            setScreenResult(result)
            const next = new Set<string>()
            for (const conv of result.conversations)
              for (const f of conv.flagged ?? []) next.add(segKey(conv.conversation_id, f.index))
            setExcluded(next)
          } else {
            setError('Screen finished but returned no result')
          }
          return
        }
        if (st === 'failed') {
          localStorage.removeItem(SCREEN_JOB_KEY)
          setError('Screen job failed — check the Queue page for details')
          return
        }
        await sleep(1500)
      }
    } catch {
      // job evicted / network error — drop the handle so the user can retry
      localStorage.removeItem(SCREEN_JOB_KEY)
      if (mounted.current) setError('Lost track of the screen job — run it again')
    } finally {
      if (mounted.current) {
        setScreening(false)
        setProgress(null)
      }
    }
  }, [selected.length])

  // Recover a screen still running (or just-finished) from a previous open.
  useEffect(() => {
    const raw = localStorage.getItem(SCREEN_JOB_KEY)
    if (!raw) return
    try {
      const saved = JSON.parse(raw) as { jobId: string; ids: string }
      if (saved.ids !== idsSig) {
        localStorage.removeItem(SCREEN_JOB_KEY) // for a different selection
        return
      }
      attachToScreenJob(saved.jobId)
    } catch {
      localStorage.removeItem(SCREEN_JOB_KEY)
    }
    // mount-only recovery; idsSig is stable for a given open
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const runScreen = async () => {
    setScreenResult(null)
    setError(null)
    setProgress({ done: 0, total: selected.length, message: 'queued…' })
    setScreening(true)
    try {
      const res = await dataAuditApi.screenExport(
        selected.map((c) => c.conversation_id),
        policy
      )
      localStorage.setItem(
        SCREEN_JOB_KEY,
        JSON.stringify({ jobId: res.data.job_id, ids: idsSig })
      )
      await attachToScreenJob(res.data.job_id)
    } catch (e: any) {
      setScreening(false)
      setProgress(null)
      setError(e?.response?.data?.error || 'Failed to run privacy screen')
    }
  }

  const toggleSeg = (cid: string, index: number) => {
    setExcluded((prev) => {
      const next = new Set(prev)
      const k = segKey(cid, index)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }

  // Build excluded_ranges {cid: [[start,end],...]} from the checked segments.
  const buildExcludedRanges = (): Record<string, number[][]> => {
    const ranges: Record<string, number[][]> = {}
    if (!screenResult) return ranges
    for (const conv of screenResult.conversations) {
      const picked = (conv.flagged ?? []).filter((f) =>
        excluded.has(segKey(conv.conversation_id, f.index))
      )
      if (picked.length) {
        ranges[conv.conversation_id] = picked.map((f) => [f.start, f.end])
      }
    }
    return ranges
  }

  const runExport = async () => {
    setExporting(true)
    setError(null)
    setStatus('Queued export…')
    try {
      const excludedRanges = screenEnabled ? buildExcludedRanges() : {}
      const res = await dataAuditApi.startExport(
        selected.map((c) => c.conversation_id),
        {
          mode,
          pad_seconds: padSeconds,
          speech_threshold: speechThreshold,
          merge_gap_seconds: mergeGap,
          excluded_ranges: excludedRanges,
          sensitivity_policy:
            screenEnabled && Object.keys(excludedRanges).length ? policy : null,
        }
      )
      const { job_id, export_id } = res.data
      const term = await pollJob(job_id, (s) => setStatus(`Exporting… (${s})`))
      setExporting(false)
      setStatus(null)
      if (term === 'finished') {
        setLatestExportId(export_id)
        loadExports()
      } else {
        setError('Export job failed — check the Queue page for details')
      }
    } catch (e: any) {
      setExporting(false)
      setStatus(null)
      setError(e?.response?.data?.error || 'Failed to start export')
    }
  }

  const deleteExport = async (exportId: string) => {
    if (!window.confirm(`Delete export ${exportId} from the server?`)) return
    try {
      await dataAuditApi.deleteExport(exportId)
      setExports((prev) => prev.filter((e) => e.export_id !== exportId))
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to delete export')
    }
  }

  const numberInput =
    'w-20 px-2 py-1 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm text-gray-900 dark:text-gray-100'

  // The screen result is only valid for the policy + selection it was run on.
  const resultIdsSig = screenResult
    ? screenResult.conversations
        .map((c) => c.conversation_id)
        .sort()
        .join(',')
    : ''
  const resultValid =
    !!screenResult &&
    screenResult.policy.trim() === policy.trim() &&
    resultIdsSig === idsSig

  const busy = screening || exporting
  const totalFlagged = screenResult?.totals.flagged_segments ?? 0
  const totalExcluded = excluded.size

  // Dataset-impact estimate, live-updated as withhold toggles change.
  // Baseline = what the export would contain without the screen: full mode is
  // the exact summed duration; clips mode estimates speech via the cached
  // speech % (padding/merge make the real number slightly larger).
  const impact: ScreenImpact = (() => {
    let baseline = 0
    for (const c of selected) {
      baseline +=
        mode === 'full' || c.speech_fraction === null
          ? c.duration_seconds
          : c.duration_seconds * c.speech_fraction
    }
    let withheldSeconds = 0
    let withheldCount = 0
    const cats = new Map<string, { count: number; seconds: number }>()
    for (const conv of screenResult?.conversations ?? []) {
      for (const f of conv.flagged ?? []) {
        if (!excluded.has(segKey(conv.conversation_id, f.index))) continue
        const secs = Math.max(0, f.end - f.start)
        withheldSeconds += secs
        withheldCount += 1
        const entry = cats.get(f.category) || { count: 0, seconds: 0 }
        entry.count += 1
        entry.seconds += secs
        cats.set(f.category, entry)
      }
    }
    return {
      baselineSeconds: baseline,
      withheldSeconds,
      withheldCount,
      byCategory: [...cats.entries()]
        .map(([category, v]) => ({ category, ...v }))
        .sort((a, b) => b.seconds - a.seconds),
      estimated: mode === 'clips',
    }
  })()
  // When the screen is on, the user screens first, then exports.
  const needsScreenFirst = screenEnabled && !resultValid && !screening

  return (
    <Modal
      open
      onClose={onClose}
      title="Export for annotation"
      icon={<PackageOpen className="h-5 w-5 text-blue-600" />}
      maxWidthClassName="max-w-2xl"
      className="max-h-[85vh] overflow-y-auto"
      footer={
        <Button variant="secondary" size="md" onClick={onClose}>
          Close
        </Button>
      }
    >
      <div className="space-y-5">
          {error && (
            <Alert tone="danger" icon={<AlertTriangle className="h-4 w-4" />}>
              {error}
            </Alert>
          )}

          {/* New export */}
          <div className="space-y-3">
            <p className="text-sm text-gray-600 dark:text-gray-300">
              Exports the selected conversations' audio paired with their machine transcripts in
              a <code className="text-xs">manifest.jsonl</code> — packaged as a zip for the
              annotator. Timestamps back into the source conversation are preserved.
            </p>

            {/* Mode toggle */}
            <div className="flex rounded-lg border border-gray-300 dark:border-gray-600 overflow-hidden w-fit text-sm">
              {(
                [
                  {
                    key: 'clips',
                    label: 'Speech clips',
                    hint: 'One WAV per speech region: silence is cut out using the VAD scores, each region padded on both sides.',
                  },
                  {
                    key: 'full',
                    label: 'Full audio',
                    hint: 'One untouched WAV per conversation — complete recording, silence included.',
                  },
                ] as const
              ).map((m) => (
                <button
                  key={m.key}
                  onClick={() => setMode(m.key)}
                  title={m.hint}
                  className={`px-4 py-1.5 transition-colors ${
                    mode === m.key
                      ? 'bg-blue-600 text-white'
                      : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>

            {mode === 'clips' ? (
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm text-gray-700 dark:text-gray-200">
                <label className="flex items-center space-x-1.5">
                  <span>Padding (s)</span>
                  <Hint text="Extra audio kept on both sides of every speech region so clips don't start or end mid-word. 1s padding means each clip begins 1s before speech starts and ends 1s after it stops." />
                  <input
                    type="number" min={0} max={10} step={0.5}
                    value={padSeconds}
                    onChange={(e) => setPadSeconds(Number(e.target.value))}
                    className={numberInput}
                  />
                </label>
                <label className="flex items-center space-x-1.5">
                  <span>Speech threshold</span>
                  <Hint text="VAD frame probability (0–1) at or above which a frame counts as speech. Lower = more sensitive (quiet/distant speech included, more noise too); higher = stricter." />
                  <input
                    type="number" min={0} max={1} step={0.05}
                    value={speechThreshold}
                    onChange={(e) => setSpeechThreshold(Number(e.target.value))}
                    className={numberInput}
                  />
                </label>
                <label className="flex items-center space-x-1.5">
                  <span>Merge gap (s)</span>
                  <Hint text="Speech regions separated by less silence than this are joined into one clip, so short pauses (between sentences, turn-taking) don't fragment a conversation into many tiny files. Raise it for fewer, longer clips; lower it for tighter, more granular clips." />
                  <input
                    type="number" min={0} max={60} step={0.5}
                    value={mergeGap}
                    onChange={(e) => setMergeGap(Number(e.target.value))}
                    className={numberInput}
                  />
                </label>
              </div>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Each conversation is exported as a single WAV with its full transcript — no VAD
                cropping. Expect larger files (silence included).
              </p>
            )}

            {/* Privacy screen */}
            <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-3">
              <label className="flex items-start space-x-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={screenEnabled}
                  onChange={(e) => setScreenEnabled(e.target.checked)}
                  className="mt-0.5"
                />
                <span className="text-sm">
                  <span className="inline-flex items-center space-x-1.5 font-medium text-gray-900 dark:text-gray-100">
                    <ShieldCheck className="h-4 w-4 text-emerald-600" />
                    <span>Privacy screen before sharing</span>
                  </span>
                  <span className="block text-xs text-gray-500 dark:text-gray-400">
                    An LLM flags transcript segments that match your policy below; you review them,
                    and the ones you keep checked are withheld (audio + text) from the export.
                    Names and identifiers are kept — annotators need them for speaker labels.
                  </span>
                </span>
              </label>

              {screenEnabled && (
                <div className="space-y-3 pl-6">
                  <div>
                    <div className="flex items-center space-x-1.5 mb-1">
                      <span className="text-xs font-medium text-gray-700 dark:text-gray-200">
                        Shareability policy
                      </span>
                      <Hint text="Describe what you would NOT be comfortable sending to an annotator. This is about personal comfort, not a strict PII definition. Editing it re-runs the screen." />
                    </div>
                    <Textarea
                      value={policy}
                      onChange={(e) => setPolicy(e.target.value)}
                      rows={5}
                      disabled={screening}
                      className="text-xs font-mono"
                    />
                  </div>

                  {/* Live progress while the screen job runs */}
                  {screening && (
                    <div className="space-y-1">
                      <div className="flex items-center justify-between text-xs text-gray-600 dark:text-gray-300">
                        <span className="inline-flex items-center gap-1.5">
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          {progress?.message || 'Screening transcripts…'}
                        </span>
                        {progress && progress.total > 0 && (
                          <span className="tabular-nums">
                            {progress.done}/{progress.total}
                          </span>
                        )}
                      </div>
                      <div className="h-1.5 w-full rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
                        <div
                          className="h-full bg-emerald-500 transition-all"
                          style={{
                            width: progress && progress.total
                              ? `${Math.round((100 * progress.done) / progress.total)}%`
                              : '10%',
                          }}
                        />
                      </div>
                      <p className="text-[11px] text-gray-400">
                        Runs on the server — safe to close this dialog; reopening reattaches.
                      </p>
                    </div>
                  )}

                  {!screening && !resultValid && (
                    <button
                      onClick={runScreen}
                      disabled={busy || selected.length === 0}
                      className="flex items-center space-x-2 px-3 py-1.5 rounded-lg text-sm font-medium border border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-900/30 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      <span>Run privacy screen</span>
                    </button>
                  )}

                  {!screening && resultValid && (
                    <ScreenReview
                      report={screenResult!.conversations}
                      excluded={excluded}
                      onToggle={toggleSeg}
                      totalFlagged={totalFlagged}
                      totalExcluded={totalExcluded}
                      onRescreen={runScreen}
                      impact={impact}
                    />
                  )}
                </div>
              )}
            </div>

            <div className="flex items-center space-x-3">
              <Button
                variant="primary"
                size="md"
                onClick={needsScreenFirst ? runScreen : runExport}
                disabled={busy || selected.length === 0}
                icon={busy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
              >
                {screening
                  ? progress && progress.total
                    ? `Screening ${progress.done}/${progress.total}…`
                    : 'Screening…'
                  : exporting
                  ? status || 'Exporting…'
                  : needsScreenFirst
                  ? `Screen ${selected.length} conversation${selected.length === 1 ? '' : 's'}`
                  : `Export ${selected.length} conversation${selected.length === 1 ? '' : 's'}` +
                    (screenEnabled && totalExcluded > 0
                      ? ` · withholding ${totalExcluded} segment${totalExcluded === 1 ? '' : 's'}`
                      : '')}
              </Button>
              {selected.length === 0 && !busy && (
                <span className="text-xs text-gray-400">
                  Select conversations in the table first
                </span>
              )}
            </div>
          </div>

          {/* Previous exports */}
          <div className="space-y-2">
            <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">
              Exports on server
            </h4>
            {loadingList && exports.length === 0 && (
              <div className="text-sm text-gray-400">Loading…</div>
            )}
            {!loadingList && exports.length === 0 && (
              <div className="text-sm text-gray-400">No exports yet.</div>
            )}
            {exports.map((exp) => {
              const skipped = exp.conversations.filter((c) => c.skipped_reason)
              return (
                <div
                  key={exp.export_id}
                  className={`px-3 py-2 rounded-lg border text-sm ${
                    exp.export_id === latestExportId
                      ? 'border-blue-400 bg-blue-50/50 dark:bg-blue-900/20'
                      : 'border-gray-200 dark:border-gray-700'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="min-w-0">
                      <div className="text-gray-800 dark:text-gray-100 truncate flex items-center gap-1.5">
                        {exp.export_id}
                        {exp.params.screened && (
                          <span
                            title="Privacy screen applied; some segments withheld"
                            className="inline-flex items-center gap-0.5 text-[10px] px-1 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300"
                          >
                            <ShieldCheck className="h-3 w-3" /> screened
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        {formatDate(exp.created_at)} · {exp.params.mode === 'full' ? 'full audio' : 'speech clips'} ·{' '}
                        {exp.totals.exported_conversations}/
                        {exp.totals.conversation_count} conversations · {exp.totals.clip_count}{' '}
                        {exp.params.mode === 'full' ? 'files' : 'clips'} ·{' '}
                        {formatDuration(exp.totals.total_clip_seconds)} ·{' '}
                        {formatBytes(exp.totals.zip_bytes)}
                        {exp.totals.excluded_seconds ? (
                          <span
                            className="text-emerald-600 dark:text-emerald-400"
                            title="Audio + transcript withheld by the privacy screen, as a share of what the dataset would have contained without it"
                          >
                            {' '}· {formatDuration(exp.totals.excluded_seconds)} withheld (
                            {(
                              (100 * exp.totals.excluded_seconds) /
                              (exp.totals.excluded_seconds + exp.totals.total_clip_seconds)
                            ).toFixed(1)}
                            %)
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex items-center space-x-1 ml-3">
                      {exp.zip_ready && (
                        <a
                          href={dataAuditApi.exportDownloadUrl(exp.export_id)}
                          className="p-1.5 rounded text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30"
                          title="Download dataset.zip"
                        >
                          <Download className="h-4 w-4" />
                        </a>
                      )}
                      <button
                        onClick={() => deleteExport(exp.export_id)}
                        className="p-1.5 rounded text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/30"
                        title="Delete export from server"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                  {skipped.length > 0 && (
                    <div className="mt-1 text-xs text-yellow-700 dark:text-yellow-300">
                      {skipped.length} skipped:{' '}
                      {skipped
                        .map((c) => `${c.title || c.conversation_id.slice(0, 8)} (${c.skipped_reason})`)
                        .join(', ')}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>

    </Modal>
  )
}

/** Review panel: flagged segments grouped by conversation, each a withhold toggle. */
function ScreenReview({
  report,
  excluded,
  onToggle,
  totalFlagged,
  totalExcluded,
  onRescreen,
  impact,
}: {
  report: ScreenConversationReport[]
  excluded: Set<string>
  onToggle: (cid: string, index: number) => void
  totalFlagged: number
  totalExcluded: number
  onRescreen: () => void
  impact: ScreenImpact
}) {
  const after = Math.max(0, impact.baselineSeconds - impact.withheldSeconds)
  const pct =
    impact.baselineSeconds > 0
      ? (100 * impact.withheldSeconds) / impact.baselineSeconds
      : 0
  const approx = impact.estimated ? '~' : ''

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-600 dark:text-gray-300">
          {totalFlagged === 0
            ? 'Nothing flagged — safe to share as-is.'
            : `${totalFlagged} segment${totalFlagged === 1 ? '' : 's'} flagged · ${totalExcluded} will be withheld`}
        </span>
        <button
          onClick={onRescreen}
          className="text-xs text-emerald-700 dark:text-emerald-300 hover:underline"
        >
          Re-screen
        </button>
      </div>


      {report
        .filter((c) => (c.flagged?.length ?? 0) > 0 || c.error)
        .map((c) => (
          <div key={c.conversation_id} className="rounded border border-gray-200 dark:border-gray-700">
            <div className="px-2 py-1 text-xs font-medium text-gray-700 dark:text-gray-200 bg-gray-50 dark:bg-gray-700/40 truncate">
              {c.title || c.conversation_id.slice(0, 8)}
              {c.error && <span className="text-red-600 dark:text-red-400"> · screen error: {c.error}</span>}
            </div>
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {(c.flagged ?? []).map((f) => {
                const k = segKey(c.conversation_id, f.index)
                const on = excluded.has(k)
                return (
                  <label
                    key={f.index}
                    className="flex items-start space-x-2 px-2 py-1.5 cursor-pointer text-xs"
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => onToggle(c.conversation_id, f.index)}
                      className="mt-0.5"
                    />
                    <span className={on ? 'opacity-100' : 'opacity-50'}>
                      <span className="inline-flex flex-wrap items-center gap-x-2">
                        <span className="px-1 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300">
                          {f.category}
                        </span>
                        <span className="text-gray-400">
                          {f.start.toFixed(1)}–{f.end.toFixed(1)}s
                          {f.speaker ? ` · ${f.speaker}` : ''}
                        </span>
                      </span>
                      {f.reason && (
                        <span className="block text-gray-600 dark:text-gray-300">{f.reason}</span>
                      )}
                      {f.text && (
                        <span className="block text-gray-500 dark:text-gray-400 italic">
                          “{f.text.length > 160 ? f.text.slice(0, 160) + '…' : f.text}”
                        </span>
                      )}
                    </span>
                  </label>
                )
              })}
            </div>
          </div>
        ))}

      {/* Dataset impact — sits under the toggles it reacts to */}
      {totalFlagged > 0 && (
        <div className="rounded border border-emerald-200 dark:border-emerald-800 bg-emerald-50/50 dark:bg-emerald-900/15 px-3 py-2 space-y-1.5">
          <div className="flex items-center gap-1.5 text-xs font-medium text-emerald-800 dark:text-emerald-300">
            <span>Dataset impact</span>
            <Hint
              text={
                impact.estimated
                  ? 'Audio durations and WAV sizes (16 kHz mono, before zip compression). In speech-clips mode the before/after figures are estimates from each conversation’s speech % — padding and region merging make the real export slightly larger.'
                  : 'Audio durations and WAV sizes (16 kHz mono, before zip compression).'
              }
            />
          </div>
          <div className="grid grid-cols-3 gap-2 text-xs text-gray-700 dark:text-gray-200">
            <div>
              <div className="text-gray-400">Without screen</div>
              <div className="tabular-nums">
                {approx}{formatDuration(impact.baselineSeconds)} ·{' '}
                {formatBytes(impact.baselineSeconds * WAV_BYTES_PER_SECOND)}
              </div>
            </div>
            <div>
              <div className="text-gray-400">Withheld</div>
              <div className="tabular-nums text-emerald-700 dark:text-emerald-300">
                {impact.withheldSeconds > 0
                  ? `${formatDuration(impact.withheldSeconds)} · ${
                      formatBytes(impact.withheldSeconds * WAV_BYTES_PER_SECOND)
                    } (${pct < 0.1 && pct > 0 ? '<0.1' : pct.toFixed(1)}%)`
                  : 'nothing'}
              </div>
            </div>
            <div>
              <div className="text-gray-400">Exported</div>
              <div className="tabular-nums">
                {approx}{formatDuration(after)} ·{' '}
                {formatBytes(after * WAV_BYTES_PER_SECOND)}
              </div>
            </div>
          </div>
          {impact.byCategory.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {impact.byCategory.map((c) => (
                <span
                  key={c.category}
                  className="px-1.5 py-0.5 rounded text-[11px] bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300"
                  title={`${c.count} segment${c.count === 1 ? '' : 's'} · ${formatDuration(c.seconds)}`}
                >
                  {c.category} ×{c.count} · {formatDuration(c.seconds)}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
