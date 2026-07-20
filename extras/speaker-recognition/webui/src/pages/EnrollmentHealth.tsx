import { useState, useEffect, useCallback } from 'react'
import {
  ShieldCheck, ShieldAlert, ChevronDown, ChevronRight,
  RefreshCw, Archive, ArrowRightLeft, HelpCircle, Database,
} from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { apiService } from '../services/api'
import { formatDuration } from '../utils/audioUtils'

interface BestOther { speaker_id: string; name: string; score: number }
interface Suggested { speaker_id: string; name: string; score: number }
interface Clip {
  segment_id: number
  filename: string
  duration: number
  self_score: number | null
  best_other: BestOther | null
  flags: string[]
  suggested: Suggested | null
}
interface SpeakerHealth {
  speaker_id: string
  name: string
  n_clips: number
  n_flagged: number
  median_self: number | null
  verdict: 'contaminated' | 'weak' | 'clean' | 'unverifiable'
  clips: Clip[]
}
interface AuditReport {
  speakers: SpeakerHealth[]
  total_clips: number
  speakers_without_segments: { speaker_id: string; name: string }[]
}
interface BackfillJob {
  id: number
  status: 'pending' | 'running' | 'completed' | 'failed'
  progress: number
  result: { added: number; failed: number; already_available: number } | null
  error: string | null
}

const VERDICT_STYLES: Record<string, string> = {
  contaminated: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  weak: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  clean: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  unverifiable: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
}
const FLAG_STYLES: Record<string, string> = {
  mislabel: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  junk: 'bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200',
  weak: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
}

function scoreColor(s: number | null): string {
  if (s === null) return 'text-gray-400'
  if (s < 0.35) return 'text-red-600 dark:text-red-400'
  if (s < 0.50) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-green-600 dark:text-green-400'
}

export default function EnrollmentHealth() {
  const { user } = useUser()
  const [report, setReport] = useState<AuditReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<number | null>(null)
  const [backfill, setBackfill] = useState<BackfillJob | null>(null)
  const [backfillError, setBackfillError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!user) return
    setLoading(true)
    setError(null)
    try {
      const res = await apiService.get('/enrollment/health', { params: { user_id: user.id } })
      if (!res.data || !Array.isArray(res.data.speakers)) {
        throw new Error('Unexpected response from enrollment health endpoint')
      }
      setReport(res.data)
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to load enrollment health')
    } finally {
      setLoading(false)
    }
  }, [user])

  useEffect(() => { load() }, [load])

  const checkBackfill = useCallback(async () => {
    if (!user) return
    try {
      const res = await apiService.get('/enrollment/segments/backfill', {
        params: { user_id: user.id },
      })
      setBackfill(res.data)
      return res.data as BackfillJob | null
    } catch (e: any) {
      setBackfillError(e?.response?.data?.detail || e?.message || 'Could not check import status')
      return null
    }
  }, [user])

  useEffect(() => { checkBackfill() }, [checkBackfill])

  useEffect(() => {
    if (!backfill || !['pending', 'running'].includes(backfill.status)) return
    const interval = setInterval(async () => {
      const job = await checkBackfill()
      if (job?.status === 'completed') await load()
    }, 1500)
    return () => clearInterval(interval)
  }, [backfill?.id, backfill?.status, checkBackfill, load])

  const startBackfill = async () => {
    if (!user) return
    setBackfillError(null)
    try {
      const res = await apiService.post('/enrollment/segments/backfill', undefined, {
        params: { user_id: user.id },
      })
      setBackfill(res.data)
    } catch (e: any) {
      setBackfillError(e?.response?.data?.detail || e?.message || 'Could not start enrollment import')
    }
  }

  const toggle = (id: string) =>
    setExpanded(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })

  // All known speakers (for the relabel dropdown), name-sorted.
  const allSpeakers = report
    ? [...report.speakers.map(s => ({ speaker_id: s.speaker_id, name: s.name })),
       ...report.speakers_without_segments]
        .sort((a, b) => a.name.localeCompare(b.name))
    : []

  const relabel = async (segmentId: number, targetSpeakerId: string) => {
    if (!targetSpeakerId) return
    setBusy(segmentId)
    try {
      const fd = new FormData()
      fd.append('target_speaker_id', targetSpeakerId)
      await apiService.post(`/enrollment/segments/${segmentId}/relabel`, fd)
      await load()
    } catch (e: any) {
      alert(e?.response?.data?.detail || 'Relabel failed')
    } finally {
      setBusy(null)
    }
  }

  const quarantine = async (segmentId: number) => {
    if (!confirm('Move this clip to the junk/quarantine folder? It is removed from the speaker voiceprint but kept on disk (recoverable).')) return
    setBusy(segmentId)
    try {
      // No hard flag => quarantine (move to junk dir), not permanent delete.
      await apiService.post(`/enrollment/segments/${segmentId}/delete`, new FormData())
      await load()
    } catch (e: any) {
      alert(e?.response?.data?.detail || 'Quarantine failed')
    } finally {
      setBusy(null)
    }
  }

  const totalFlagged = report?.speakers.reduce((a, s) => a + s.n_flagged, 0) ?? 0
  const contaminated = report?.speakers.filter(s => s.verdict === 'contaminated').length ?? 0

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="heading-md flex items-center gap-2">
            <ShieldCheck className="h-6 w-6 text-blue-600 dark:text-blue-400" />
            Enrollment Health
          </h2>
          <p className="text-sm text-muted mt-1">
            Finds mislabeled, contaminated, and junk enrolled clips from per-clip embeddings.
            Relabel a clip to who it really sounds like, or delete it — the speaker voiceprint is recomputed.
          </p>
        </div>
        <button onClick={load} disabled={loading}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-gray-200 dark:bg-gray-700 rounded-md hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-50">
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {!user && <div className="text-muted">Select a user to audit enrollment.</div>}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 p-3 rounded-md text-sm">{error}</div>
      )}

      {report && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Stat label="Speakers" value={report.speakers.length} />
            <Stat label="Enrolled clips" value={report.total_clips} />
            <Stat label="Flagged clips" value={totalFlagged} tone={totalFlagged ? 'warn' : 'ok'} />
            <Stat label="Contaminated speakers" value={contaminated} tone={contaminated ? 'bad' : 'ok'} />
          </div>

          {report.total_clips === 0 && (
            <div className="bg-yellow-50 dark:bg-yellow-900/30 text-yellow-900 dark:text-yellow-100 p-4 rounded-md">
              <div className="font-medium">Existing enrollment clips need analysis</div>
              <p className="mt-1 text-sm">
                Build the per-clip embeddings used to find mislabeled and low-quality audio.
                Existing speaker voiceprints are not changed.
              </p>
              <div className="mt-3 flex items-center gap-3">
                <button
                  onClick={startBackfill}
                  disabled={backfill?.status === 'pending' || backfill?.status === 'running'}
                  className="flex items-center gap-2 px-3 py-2 text-sm font-medium bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-60"
                >
                  <Database className={`h-4 w-4 ${backfill?.status === 'running' ? 'animate-pulse' : ''}`} />
                  {backfill?.status === 'pending' || backfill?.status === 'running'
                    ? `Analyzing clips · ${Math.round(backfill.progress)}%`
                    : backfill?.status === 'failed' ? 'Try again' : 'Analyze enrollment clips'}
                </button>
                {backfill?.status === 'completed' && backfill.result && (
                  <span className="text-sm">
                    Added {backfill.result.added} clips
                    {backfill.result.failed > 0 ? ` · ${backfill.result.failed} failed` : ''}
                  </span>
                )}
              </div>
              {(backfillError || backfill?.error) && (
                <div className="mt-3 text-sm text-red-700 dark:text-red-300">
                  Analysis failed: {backfillError || backfill?.error}
                </div>
              )}
            </div>
          )}

          <div className="space-y-3">
            {report.speakers.map(spk => {
              const open = expanded.has(spk.speaker_id)
              return (
                <div key={spk.speaker_id} className="card">
                  <button
                    onClick={() => toggle(spk.speaker_id)}
                    className="w-full flex items-center justify-between p-4 hover-bg rounded-t-md"
                  >
                    <div className="flex items-center gap-3">
                      {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                      {spk.verdict === 'contaminated'
                        ? <ShieldAlert className="h-5 w-5 text-red-500" />
                        : spk.verdict === 'clean'
                        ? <ShieldCheck className="h-5 w-5 text-green-500" />
                        : <HelpCircle className="h-5 w-5 text-gray-400" />}
                      <span className="font-medium text-primary">{spk.name}</span>
                      <span className={`text-xs px-2 py-0.5 rounded ${VERDICT_STYLES[spk.verdict]}`}>
                        {spk.verdict}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 text-sm text-muted">
                      <span>{spk.n_clips} clips</span>
                      {spk.n_flagged > 0 && <span className="text-red-500">{spk.n_flagged} flagged</span>}
                      <span>median <b className={scoreColor(spk.median_self)}>
                        {spk.median_self === null ? '—' : spk.median_self.toFixed(3)}
                      </b></span>
                    </div>
                  </button>

                  {open && (
                    <div className="border-t dark:border-gray-700 divide-y dark:divide-gray-700">
                      {spk.clips.map(clip => (
                        <div key={clip.segment_id} className="p-4 flex flex-col lg:flex-row lg:items-center gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="font-mono text-xs truncate">{clip.filename}</span>
                              <span className="text-xs text-muted">{formatDuration(clip.duration)}</span>
                              {clip.flags.map(f => (
                                <span key={f} className={`text-xs px-1.5 py-0.5 rounded ${FLAG_STYLES[f] || 'bg-gray-100 text-gray-700'}`}>{f}</span>
                              ))}
                            </div>
                            <div className="text-xs text-muted mt-1">
                              self <b className={scoreColor(clip.self_score)}>
                                {clip.self_score === null ? '—' : clip.self_score.toFixed(3)}
                              </b>
                              {clip.best_other && (
                                <> · closest other: {clip.best_other.name} <b className={scoreColor(clip.best_other.score)}>
                                  {clip.best_other.score.toFixed(3)}
                                </b></>
                              )}
                            </div>
                          </div>

                          <audio controls preload="none" className="h-8"
                            src={`/api/enrollment/segments/${clip.segment_id}/audio`} />

                          <div className="flex items-center gap-2">
                            {clip.suggested && (
                              <button
                                disabled={busy === clip.segment_id}
                                onClick={() => relabel(clip.segment_id, clip.suggested!.speaker_id)}
                                className="text-xs flex items-center gap-1 px-2 py-1 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                                title={`Move to ${clip.suggested.name}`}
                              >
                                <ArrowRightLeft className="h-3 w-3" />
                                → {clip.suggested.name}
                              </button>
                            )}
                            <select
                              disabled={busy === clip.segment_id}
                              defaultValue=""
                              onChange={e => relabel(clip.segment_id, e.target.value)}
                              className="text-xs py-1 px-2 border border-gray-300 dark:border-gray-700 rounded dark:bg-gray-800 dark:text-gray-100 disabled:opacity-50"
                              title="Relabel to…"
                            >
                              <option value="" disabled>Relabel to…</option>
                              {allSpeakers
                                .filter(s => s.speaker_id !== spk.speaker_id)
                                .map(s => <option key={s.speaker_id} value={s.speaker_id}>{s.name}</option>)}
                            </select>
                            <button
                              disabled={busy === clip.segment_id}
                              onClick={() => quarantine(clip.segment_id)}
                              className="text-xs flex items-center gap-1 px-2 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50"
                              title="Move to junk (quarantine — recoverable)"
                            >
                              <Archive className="h-3 w-3" /> Junk
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: 'ok' | 'warn' | 'bad' }) {
  const color = tone === 'bad' ? 'text-red-600 dark:text-red-400'
    : tone === 'warn' ? 'text-yellow-600 dark:text-yellow-400'
    : 'text-gray-900 dark:text-gray-100'
  return (
    <div className="card p-4">
      <div className="text-sm text-muted">{label}</div>
      <div className={`text-2xl font-semibold ${color}`}>{value}</div>
    </div>
  )
}
