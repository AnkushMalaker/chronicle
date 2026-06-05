import { useCallback, useEffect, useRef, useState } from 'react'
import { Mic, Radio, Trash2, Check, X, RefreshCw, Target, AlertTriangle, Square } from 'lucide-react'
import { wakewordApi, WakeStream, WakeSample } from '../services/api'

type Bucket = 'pending' | 'positive' | 'negative'

const BUCKET_LABELS: Record<Bucket, string> = {
  pending: 'Pending review',
  positive: 'Positives (wake)',
  negative: 'Negatives (not wake)',
}

export default function WakeWordLab() {
  const [streams, setStreams] = useState<WakeStream[]>([])
  const [stats, setStats] = useState<{ pending: number; positive: number; negative: number; false_negatives: number } | null>(null)
  const [bucket, setBucket] = useState<Bucket>('pending')
  const [samples, setSamples] = useState<WakeSample[]>([])
  const [error, setError] = useState<string | null>(null)
  const [primedMsg, setPrimedMsg] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  // Latest selected bucket + whether any stream was priming on the previous poll,
  // read inside the polling callback without re-creating the interval.
  const bucketRef = useRef<Bucket>(bucket)
  const wasPrimingRef = useRef(false)
  useEffect(() => { bucketRef.current = bucket }, [bucket])

  const refreshSamples = useCallback(async (b: Bucket) => {
    try {
      const { data } = await wakewordApi.getSamples(b)
      setSamples(data.samples)
    } catch {
      setSamples([])
    }
  }, [])

  const refreshMeta = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([wakewordApi.getStreams(), wakewordApi.getStats()])
      setStreams(s.data.streams)
      setStats(st.data)
      setError(null)
      // A prime session that just ended has dropped its clip into pending review —
      // pull the current bucket so the capture shows up without a manual refresh.
      const anyPriming = s.data.streams.some((x) => x.priming)
      if (wasPrimingRef.current && !anyPriming) {
        refreshSamples(bucketRef.current)
        setPrimedMsg(null)
      }
      wasPrimingRef.current = anyPriming
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Wake-word service unreachable')
    }
  }, [refreshSamples])

  const refreshAll = useCallback(async () => {
    setLoading(true)
    await Promise.all([refreshMeta(), refreshSamples(bucket)])
    setLoading(false)
  }, [refreshMeta, refreshSamples, bucket])

  useEffect(() => { refreshAll() }, [refreshAll])
  // Poll streams/stats so a freshly-started recording shows up to prime.
  useEffect(() => {
    const t = setInterval(refreshMeta, 4000)
    return () => clearInterval(t)
  }, [refreshMeta])
  useEffect(() => { refreshSamples(bucket) }, [bucket, refreshSamples])

  const prime = async (clientId?: string) => {
    try {
      const { data } = await wakewordApi.prime(clientId)
      // The capture lands in pending review — show that tab so it's visible.
      setBucket('pending')
      setPrimedMsg(`Primed ${data.client_id} — say "hey hermes" now (auto-stops in 10s)…`)
      setTimeout(() => setPrimedMsg(null), 10000)
      refreshMeta()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not prime stream')
    }
  }

  const stopPrime = async (clientId: string) => {
    try {
      await wakewordApi.unprime(clientId)
      setPrimedMsg(null)
      // The finalize+save happens on the next audio frame (~0.25s); give it a beat
      // then pull streams + the pending list so the clip appears.
      setTimeout(() => Promise.all([refreshMeta(), refreshSamples('pending')]), 500)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not stop priming')
    }
  }

  const label = async (id: string, lbl: 'wake' | 'not_wake') => {
    await wakewordApi.label(id, lbl)
    await Promise.all([refreshSamples(bucket), refreshMeta()])
  }

  const remove = async (id: string) => {
    await wakewordApi.remove(id)
    await Promise.all([refreshSamples(bucket), refreshMeta()])
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-2">
          <Target className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Wake-Word Lab</h1>
        </div>
        <button
          onClick={refreshAll}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
        Close the training loop: review false positives the model fired on, and capture
        clips of yourself saying the wake word (false negatives). Labeled clips roll
        straight into the next retrain.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 px-4 py-2 text-sm text-red-700 dark:text-red-300">
          <AlertTriangle className="h-4 w-4" /> {error}
        </div>
      )}

      {primedMsg && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-green-300 dark:border-green-700 bg-green-50 dark:bg-green-900/20 px-4 py-3 text-sm font-medium text-green-800 dark:text-green-200 animate-pulse">
          <Mic className="h-4 w-4" /> {primedMsg}
        </div>
      )}

      {/* Stats */}
      {stats && (
        <div className="mb-6 grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatCard label="Pending" value={stats.pending} tone="amber" />
          <StatCard label="Positives" value={stats.positive} tone="green" />
          <StatCard label="Negatives" value={stats.negative} tone="red" />
          <StatCard label="False negatives" value={stats.false_negatives} tone="blue" />
        </div>
      )}

      {/* Active streams to prime */}
      <div className="mb-6 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
          <Radio className="h-4 w-4" /> Active streams
        </h2>
        {streams.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            No live streams. Start a recording (Live Record) and it will appear here.
          </p>
        ) : (
          <ul className="space-y-2">
            {streams.map((s) => (
              <li
                key={s.client_id}
                className="flex items-center justify-between rounded-md bg-gray-50 dark:bg-gray-800 px-3 py-2"
              >
                <span className="flex items-center gap-2 text-sm font-mono text-gray-700 dark:text-gray-300">
                  <span className="h-2 w-2 rounded-full bg-green-500" />
                  {s.client_id}
                  {s.priming && (
                    <span className="ml-2 rounded bg-green-100 dark:bg-green-900 px-1.5 py-0.5 text-xs text-green-700 dark:text-green-300">
                      listening…
                    </span>
                  )}
                </span>
                {s.priming ? (
                  <button
                    onClick={() => stopPrime(s.client_id)}
                    className="flex items-center gap-1.5 rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700"
                  >
                    <Square className="h-3.5 w-3.5" /> Stop &amp; save
                  </button>
                ) : (
                  <button
                    onClick={() => prime(s.client_id)}
                    className="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
                  >
                    <Target className="h-3.5 w-3.5" /> I'll say it now
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Bucket tabs */}
      <div className="mb-3 flex gap-2">
        {(Object.keys(BUCKET_LABELS) as Bucket[]).map((b) => (
          <button
            key={b}
            onClick={() => setBucket(b)}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              bucket === b
                ? 'bg-blue-600 text-white'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600'
            }`}
          >
            {BUCKET_LABELS[b]}
          </button>
        ))}
      </div>

      {/* Clip list */}
      {samples.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-500 dark:text-gray-400">
          No clips in “{BUCKET_LABELS[bucket]}”.
        </p>
      ) : (
        <ul className="space-y-2">
          {samples.map((s) => (
            <ClipRow key={s.id} sample={s} onLabel={label} onDelete={remove} />
          ))}
        </ul>
      )}
    </div>
  )
}

function StatCard({ label, value, tone }: { label: string; value: number; tone: string }) {
  const tones: Record<string, string> = {
    amber: 'text-amber-600 dark:text-amber-400',
    green: 'text-green-600 dark:text-green-400',
    red: 'text-red-600 dark:text-red-400',
    blue: 'text-blue-600 dark:text-blue-400',
  }
  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-3 text-center">
      <div className={`text-2xl font-bold ${tones[tone]}`}>{value}</div>
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
    </div>
  )
}

function ClipRow({
  sample,
  onLabel,
  onDelete,
}: {
  sample: WakeSample
  onLabel: (id: string, label: 'wake' | 'not_wake') => void
  onDelete: (id: string) => void
}) {
  const [audioUrl, setAudioUrl] = useState<string | null>(null)
  const urlRef = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    wakewordApi
      .getAudioBlob(sample.id)
      .then((res) => {
        if (cancelled) return
        const url = URL.createObjectURL(res.data)
        urlRef.current = url
        setAudioUrl(url)
      })
      .catch(() => {})
    return () => {
      cancelled = true
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    }
  }, [sample.id])

  const when = new Date(sample.created_at_ms).toLocaleString()

  return (
    <li className="flex flex-wrap items-center gap-3 rounded-md border border-gray-200 dark:border-gray-700 px-3 py-2">
      <span className="font-mono text-sm font-semibold text-gray-800 dark:text-gray-200 w-16">
        {sample.score.toFixed(3)}
      </span>
      {sample.false_negative && (
        <span className="rounded bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 text-xs text-blue-700 dark:text-blue-300">
          missed
        </span>
      )}
      <span className="text-xs text-gray-500 dark:text-gray-400">{when}</span>
      <span className="text-xs text-gray-400 dark:text-gray-500">
        {sample.duration_secs}s · {sample.reason}
      </span>
      {audioUrl ? (
        <audio controls src={audioUrl} className="h-8 max-w-[220px]" />
      ) : (
        <span className="text-xs text-gray-400">loading…</span>
      )}
      <div className="ml-auto flex items-center gap-1.5">
        <button
          onClick={() => onLabel(sample.id, 'wake')}
          title="Mark as a real wake word (positive)"
          className="flex items-center gap-1 rounded-md bg-green-100 dark:bg-green-900/40 px-2 py-1 text-xs font-medium text-green-700 dark:text-green-300 hover:bg-green-200 dark:hover:bg-green-900"
        >
          <Check className="h-3.5 w-3.5" /> Wake
        </button>
        <button
          onClick={() => onLabel(sample.id, 'not_wake')}
          title="Mark as NOT the wake word (hard negative)"
          className="flex items-center gap-1 rounded-md bg-red-100 dark:bg-red-900/40 px-2 py-1 text-xs font-medium text-red-700 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-900"
        >
          <X className="h-3.5 w-3.5" /> Not
        </button>
        <button
          onClick={() => onDelete(sample.id)}
          title="Delete clip"
          className="rounded-md p-1 text-gray-400 hover:text-red-600 hover:bg-gray-100 dark:hover:bg-gray-700"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </li>
  )
}
