import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Loader2, Pause, Play, X } from 'lucide-react'
import { SpeechRegion, dataAuditApi } from '../../services/api'
import { useGaplessPlayer, usePlayheadTime } from '../../hooks/useGaplessPlayer'
import type { Range } from '../../lib/gaplessPlayer'
import { formatClock, formatDuration } from './format'

const PLAYBACK_RATES = [1, 1.5, 2]

// The timeline always fits its container width (never scrolls horizontally), but
// to keep it readable it caps density at this many seconds per row. Longer
// recordings wrap onto additional rows instead of compressing into one. Tune
// this single number to trade rows-of-height against horizontal detail.
const MAX_SECONDS_PER_ROW = 1200

type PlayMode = 'speech' | 'full'

// Mode is a sticky preference shared by every strip in the session.
const MODE_STORAGE_KEY = 'data_audit_preview_mode'

function loadMode(): PlayMode {
  const raw = sessionStorage.getItem(MODE_STORAGE_KEY)
  return raw === 'full' ? 'full' : 'speech'
}

interface Props {
  conversationId: string
  durationSeconds: number
  onClose?: () => void
  /** Ranges to shade on the timeline (e.g. detected silence gaps). */
  overlays?: { start: number; end: number }[]
  /** Vertical marker positions in seconds (e.g. chosen split points). */
  markers?: number[]
  /** Start playing as soon as regions load. */
  autoPlay?: boolean
  /** Speaker labels available for the "only this speaker" filter. */
  speakers?: string[]
}

/**
 * Speech-aware audio preview. Two modes share one program fed to the app-wide
 * gapless scheduler: "Speech only" plays the VAD speech regions and skips the
 * silence between them (seamless gapless concatenation of the regions); "Full
 * audio" plays the whole recording as a single range. The scheduler decodes 10 s
 * windows on the browser audio thread and schedules them back-to-back, so seeking
 * is sample-accurate and boundaries are inaudible in both modes.
 *
 * With a speaker selected, speech regions are the overlap of VAD speech and that
 * speaker's transcript segments — playback skips everyone else.
 */
export default function PreviewStrip({
  conversationId,
  durationSeconds,
  onClose,
  overlays,
  markers,
  autoPlay = false,
  speakers,
}: Props) {
  const [regions, setRegions] = useState<SpeechRegion[] | null>(null)
  const [speechSeconds, setSpeechSeconds] = useState(0)
  const [duration, setDuration] = useState(durationSeconds)
  const [needsAnalysis, setNeedsAnalysis] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [mode, setMode] = useState<PlayMode>(() => loadMode())
  const [rate, setRate] = useState(1)
  // '' = all speakers; otherwise regions = VAD speech ∩ this speaker's segments.
  const [speakerFilter, setSpeakerFilter] = useState('')

  // Playback is owned by the app-wide gapless scheduler.
  const player = useGaplessPlayer()
  const isActive = player.isActive(conversationId)
  const playing = isActive && player.isPlaying
  const buffering = isActive && player.buffering
  const absTime = usePlayheadTime(conversationId) ?? null

  // Latest values for the regions-load effect (which only depends on cid/filter).
  const regionsRef = useRef<Range[]>([])
  const durationRef = useRef(durationSeconds)
  const rateRef = useRef(1)
  const modeRef = useRef<PlayMode>(mode)
  // Resume position carried across a region refetch (speaker filter change).
  const resumeAtRef = useRef<number | null>(null)
  const autoPlayedRef = useRef(false)

  // Build the scheduler program for a mode: the speech regions, or the whole
  // recording as a single range (the scheduler windows it internally).
  const buildProgram = useCallback((forMode: PlayMode): Range[] => {
    if (forMode === 'speech') return regionsRef.current
    const total = durationRef.current
    return total > 0 ? [{ start: 0, end: total }] : []
  }, [])

  // Load regions on mount and whenever the speaker filter changes.
  useEffect(() => {
    let cancelled = false
    setRegions(null)
    dataAuditApi
      .getSpeechRegions(conversationId, speakerFilter ? [speakerFilter] : undefined)
      .then((res) => {
        if (cancelled) return
        setNeedsAnalysis(res.data.needs_analysis)
        setRegions(res.data.regions)
        regionsRef.current = res.data.regions
        setSpeechSeconds(res.data.speech_seconds)
        if (res.data.duration_seconds > 0) {
          setDuration(res.data.duration_seconds)
          durationRef.current = res.data.duration_seconds
        }
        // Unanalyzed conversations have no regions — fall back to full audio.
        const effectiveMode: PlayMode =
          modeRef.current === 'speech' && res.data.regions.length === 0 && !speakerFilter
            ? 'full'
            : modeRef.current
        const program = buildProgram(effectiveMode)
        const resumeAt = resumeAtRef.current
        resumeAtRef.current = null
        if (resumeAt !== null && program.length > 0) {
          player.playProgram(conversationId, program, { rate: rateRef.current, fromAudioTime: resumeAt })
        } else if (autoPlay && !autoPlayedRef.current && program.length > 0) {
          autoPlayedRef.current = true
          player.playProgram(conversationId, program, { rate: rateRef.current })
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.error || 'Failed to load speech regions')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, speakerFilter])

  // Stop playback on unmount (only if this strip owns it).
  useEffect(() => {
    return () => {
      if (player.isActive(conversationId)) player.stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  const canPlaySpeech = (regions?.length || 0) > 0
  const playDisabled =
    regions === null || (mode === 'speech' ? !canPlaySpeech : duration <= 0)

  const togglePlay = () => {
    if (player.isActive(conversationId)) {
      if (player.isPlaying) {
        player.pause()
        return
      }
      if (player.isPaused) {
        player.resume()
        return
      }
    }
    const program = buildProgram(modeRef.current)
    if (program.length > 0) player.playProgram(conversationId, program, { rate: rateRef.current })
  }

  const toggleMode = () => {
    const next: PlayMode = mode === 'speech' ? 'full' : 'speech'
    const position = absTime
    const wasPlaying = playing || buffering
    setMode(next)
    modeRef.current = next
    try {
      sessionStorage.setItem(MODE_STORAGE_KEY, next)
    } catch {
      // ignore storage quota/availability errors
    }
    // Continue from the same position in the other mode.
    const program = buildProgram(next)
    if (wasPlaying && position !== null && program.length > 0) {
      player.playProgram(conversationId, program, { rate: rateRef.current, fromAudioTime: position })
    } else if (player.isActive(conversationId)) {
      player.stop()
    }
  }

  const changeSpeaker = (value: string) => {
    const position = absTime
    const wasPlaying = playing || buffering
    // Picking a speaker implies speech-only playback (the filter has no effect on
    // full-audio). Not persisted — it's a side effect, not the sticky preference.
    if (value && modeRef.current !== 'speech') {
      setMode('speech')
      modeRef.current = 'speech'
    }
    // Carry the position across the refetch; the load effect resumes there.
    resumeAtRef.current = wasPlaying && position !== null ? position : null
    if (player.isActive(conversationId)) player.stop()
    setSpeakerFilter(value)
  }

  const cycleRate = () => {
    const next = PLAYBACK_RATES[(PLAYBACK_RATES.indexOf(rate) + 1) % PLAYBACK_RATES.length]
    setRate(next)
    rateRef.current = next
    player.setRate(next)
  }

  // Split the recording into equal-length rows, each capped at MAX_SECONDS_PER_ROW
  // so density stays readable. Short recordings stay a single fit-to-width row.
  const rowCount = duration > 0 ? Math.max(1, Math.ceil(duration / MAX_SECONDS_PER_ROW)) : 1
  const rowSpan = duration > 0 ? duration / rowCount : 0

  // Seek by clicking a row: map the click x within this row back to absolute time.
  const seekRow = (e: React.MouseEvent<HTMLDivElement>, rowStart: number) => {
    if (duration <= 0) return
    const rect = e.currentTarget.getBoundingClientRect()
    const time = Math.min(duration, Math.max(0, rowStart + ((e.clientX - rect.left) / rect.width) * rowSpan))
    if (player.isActive(conversationId)) {
      player.seek(time)
    } else {
      const program = buildProgram(modeRef.current)
      if (program.length > 0) {
        player.playProgram(conversationId, program, { rate: rateRef.current, fromAudioTime: time })
      }
    }
  }

  // Position a point within a row as a CSS percentage.
  const pointPct = (t: number, rowStart: number) =>
    `${Math.min(100, Math.max(0, ((t - rowStart) / rowSpan) * 100))}%`

  // Clip an absolute [start,end] interval to a row; null if it doesn't intersect.
  const clipToRow = (start: number, end: number, rowStart: number) => {
    const a = Math.max(start, rowStart)
    const b = Math.min(end, rowStart + rowSpan)
    if (b <= a) return null
    return {
      left: `${((a - rowStart) / rowSpan) * 100}%`,
      width: `${((b - a) / rowSpan) * 100}%`,
    }
  }

  return (
    <div className="space-y-2 rounded-lg bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 p-3">
      {/* Header */}
      <div className="flex items-center space-x-3 text-sm text-gray-700 dark:text-gray-200">
        <button
          onClick={togglePlay}
          disabled={playDisabled}
          className="p-1.5 rounded-full bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          title={playing ? 'Pause' : mode === 'speech' ? 'Play speech only' : 'Play full audio'}
        >
          {buffering ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : playing ? (
            <Pause className="h-4 w-4" />
          ) : (
            <Play className="h-4 w-4" />
          )}
        </button>
        {regions === null && !error ? (
          <span className="text-gray-400">Loading speech map…</span>
        ) : needsAnalysis ? (
          <span className="flex items-center space-x-1 text-yellow-700 dark:text-yellow-300">
            <AlertTriangle className="h-4 w-4" />
            <span>Not analyzed — playing full audio; run Analyze audio for speech-only preview</span>
          </span>
        ) : (
          <span>
            {speakerFilter || 'Speech'}: <strong>{formatDuration(speechSeconds)}</strong> of{' '}
            {formatDuration(duration)} · {regions?.length || 0} region
            {regions?.length === 1 ? '' : 's'}
            {absTime !== null && (
              <span className="text-gray-400"> · {formatClock(absTime)}</span>
            )}
          </span>
        )}
        <div className="flex-1" />
        {!needsAnalysis && (speakers?.length || 0) > 0 && (
          <select
            value={speakerFilter}
            onChange={(e) => changeSpeaker(e.target.value)}
            className={`px-1.5 py-0.5 rounded text-xs border bg-transparent transition-colors ${
              speakerFilter
                ? 'border-violet-400 bg-violet-50 text-violet-700 dark:bg-violet-900/40 dark:text-violet-200 dark:border-violet-600'
                : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'
            }`}
            title="Play only where this speaker talks (VAD speech ∩ speaker segments)"
          >
            <option value="">All speakers</option>
            {(speakers || []).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
        {!needsAnalysis && (
          <button
            onClick={toggleMode}
            className={`px-2 py-0.5 rounded text-xs border transition-colors ${
              mode === 'speech'
                ? 'border-blue-400 bg-blue-50 text-blue-700 dark:bg-blue-900/40 dark:text-blue-200 dark:border-blue-600'
                : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
            title={
              mode === 'speech'
                ? 'Skipping silence — click to play the full recording'
                : 'Playing everything — click to skip silence'
            }
          >
            {mode === 'speech' ? 'Speech only' : 'Full audio'}
          </button>
        )}
        <button
          onClick={cycleRate}
          className="px-2 py-0.5 rounded text-xs border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          title="Playback speed"
        >
          {rate}x
        </button>
        {onClose && (
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            title="Close preview"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      {error && <div className="text-xs text-red-600 dark:text-red-400">{error}</div>}

      {/* Timeline: speech blocks, overlays (gaps), markers (split points), playhead.
          Wraps onto multiple fit-to-width rows for long audio — never scrolls
          horizontally. Each row covers an equal slice of the recording. */}
      <div className="space-y-1">
        {Array.from({ length: rowCount }, (_, row) => {
          const rowStart = row * rowSpan
          const rowEnd = rowStart + rowSpan
          return (
            <div
              key={`row${row}`}
              onClick={(e) => seekRow(e, rowStart)}
              className="relative h-4 rounded bg-gray-200 dark:bg-gray-700 cursor-pointer overflow-hidden"
              title={
                mode === 'speech'
                  ? 'Click to play from here (snaps forward to speech)'
                  : 'Click to play from here'
              }
            >
              {(overlays || []).map((o, i) => {
                const c = clipToRow(o.start, o.end, rowStart)
                return c ? (
                  <div
                    key={`o${i}`}
                    className="absolute inset-y-0 bg-amber-200/70 dark:bg-amber-700/40"
                    style={c}
                  />
                ) : null
              })}
              {(regions || []).map((r, i) => {
                const c = clipToRow(r.start, r.end, rowStart)
                return c ? (
                  <div
                    key={`r${i}`}
                    className={`absolute inset-y-0 rounded-sm ${
                      speakerFilter
                        ? 'bg-violet-500/80 dark:bg-violet-400/80'
                        : 'bg-blue-500/80 dark:bg-blue-400/80'
                    }`}
                    style={{ left: c.left, width: `max(2px, ${c.width})` }}
                  />
                ) : null
              })}
              {(markers || []).map((m, i) =>
                m >= rowStart && m < rowEnd ? (
                  <div
                    key={`m${i}`}
                    className="absolute inset-y-0 w-0.5 bg-red-500"
                    style={{ left: pointPct(m, rowStart) }}
                  />
                ) : null
              )}
              {absTime !== null && absTime >= rowStart && absTime < rowEnd && (
                <div
                  className="absolute inset-y-0 w-0.5 bg-gray-900 dark:bg-white"
                  style={{ left: pointPct(absTime, rowStart) }}
                />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
