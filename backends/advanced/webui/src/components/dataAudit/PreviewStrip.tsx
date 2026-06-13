import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Loader2, Pause, Play, X } from 'lucide-react'
import { BACKEND_URL, SpeechRegion, dataAuditApi } from '../../services/api'
import { getStorageKey } from '../../utils/storage'
import { formatClock, formatDuration } from './format'

const PLAYBACK_RATES = [1, 1.5, 2]

// Full-audio mode plays contiguous windows of this length. The stored opus is
// one independent ogg stream per 10s chunk — browsers cannot seek a chained
// concatenation of those (they see only the first stream), so BOTH modes play
// exact time-clipped WAV windows from the chunks endpoint instead.
const FULL_WINDOW_SECONDS = 60

type PlayMode = 'speech' | 'full'

interface Segment {
  start: number
  end: number
}

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
 * Speech-aware audio preview. One playlist engine drives two modes:
 * "Speech only" plays the VAD speech regions and skips the silence between
 * them; "Full audio" plays contiguous windows covering the whole recording.
 * Every segment is an exact time-clipped WAV, so seeking and the playhead
 * are sample-accurate in both modes.
 *
 * With a speaker selected, speech regions are the overlap of VAD speech and
 * that speaker's transcript segments — playback skips everyone else.
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
  const [playing, setPlaying] = useState(false)
  const [buffering, setBuffering] = useState(false)
  const [absTime, setAbsTime] = useState<number | null>(null)
  const [rate, setRate] = useState(1)
  // '' = all speakers; otherwise regions = VAD speech ∩ this speaker's segments.
  const [speakerFilter, setSpeakerFilter] = useState('')

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const prefetchRef = useRef<{ index: number; audio: HTMLAudioElement } | null>(null)
  const playlistRef = useRef<Segment[]>([])
  const regionsRef = useRef<Segment[]>([])
  const durationRef = useRef(durationSeconds)
  const rateRef = useRef(1)
  const modeRef = useRef<PlayMode>(mode)
  // Resume position carried across a region refetch (speaker filter change).
  const resumeAtRef = useRef<number | null>(null)
  const autoPlayedRef = useRef(false)

  const token = () => localStorage.getItem(getStorageKey('token')) || ''

  const segmentUrl = useCallback(
    (start: number, end: number) =>
      `${BACKEND_URL}/api/audio/chunks/${conversationId}` +
      `?start_time=${start.toFixed(2)}&end_time=${end.toFixed(2)}&format=wav&token=${token()}`,
    [conversationId]
  )

  const buildPlaylist = useCallback((forMode: PlayMode): Segment[] => {
    if (forMode === 'speech') return regionsRef.current
    const total = durationRef.current
    if (total <= 0) return []
    const windows: Segment[] = []
    for (let t = 0; t < total; t += FULL_WINDOW_SECONDS) {
      windows.push({ start: t, end: Math.min(t + FULL_WINDOW_SECONDS, total) })
    }
    return windows
  }, [])

  const stop = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    prefetchRef.current = null
    setPlaying(false)
    setBuffering(false)
  }, [])

  // --- Playlist engine: exact-clipped WAV per segment, chained + prefetched ---
  const playSegment = useCallback(
    (index: number, startWithin: number = 0) => {
      const segment = playlistRef.current[index]
      if (!segment) {
        // Ran off the end of the playlist: reset so play starts over.
        stop()
        setAbsTime(null)
        return
      }
      audioRef.current?.pause()

      const startAbs = segment.start + startWithin
      let audio: HTMLAudioElement
      if (startWithin === 0 && prefetchRef.current?.index === index) {
        audio = prefetchRef.current.audio
      } else {
        audio = new Audio(segmentUrl(startAbs, segment.end))
      }
      prefetchRef.current = null
      audioRef.current = audio
      audio.playbackRate = rateRef.current
      setBuffering(true)
      setAbsTime(startAbs)

      audio.addEventListener('playing', () => {
        setBuffering(false)
        setPlaying(true)
        // Prefetch the next segment for a gapless jump.
        const next = playlistRef.current[index + 1]
        if (next && prefetchRef.current?.index !== index + 1) {
          const pre = new Audio(segmentUrl(next.start, next.end))
          pre.preload = 'auto'
          prefetchRef.current = { index: index + 1, audio: pre }
        }
      })
      audio.addEventListener('timeupdate', () => {
        setAbsTime(startAbs + audio.currentTime)
      })
      audio.addEventListener('ended', () => playSegment(index + 1))
      audio.addEventListener('error', () => {
        setError('Playback failed')
        stop()
      })
      audio.play().catch(() => stop())
    },
    [segmentUrl, stop]
  )

  const startAt = useCallback(
    (time: number) => {
      // Land inside the segment containing the time, or snap forward to the
      // next one (in speech mode, time can fall inside a silence gap).
      const index = playlistRef.current.findIndex((s) => s.end > time)
      if (index === -1) {
        stop()
        return
      }
      const segment = playlistRef.current[index]
      playSegment(index, Math.max(0, time - segment.start))
    },
    [playSegment, stop]
  )

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
        playlistRef.current = buildPlaylist(effectiveMode)
        const resumeAt = resumeAtRef.current
        resumeAtRef.current = null
        if (resumeAt !== null && playlistRef.current.length > 0) {
          startAt(resumeAt)
        } else if (autoPlay && !autoPlayedRef.current && playlistRef.current.length > 0) {
          autoPlayedRef.current = true
          playSegment(0)
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

  // Stop audio on unmount.
  useEffect(() => stop, [stop])

  const canPlaySpeech = (regions?.length || 0) > 0
  const playDisabled =
    regions === null || (mode === 'speech' ? !canPlaySpeech : duration <= 0)

  const togglePlay = () => {
    if (audioRef.current && !audioRef.current.paused) {
      audioRef.current.pause()
      setPlaying(false)
      return
    }
    if (audioRef.current && audioRef.current.paused && !audioRef.current.ended) {
      audioRef.current.play().then(() => setPlaying(true)).catch(() => stop())
      return
    }
    if (playlistRef.current.length > 0) playSegment(0)
  }

  const toggleMode = () => {
    const next: PlayMode = mode === 'speech' ? 'full' : 'speech'
    const position = absTime
    const wasPlaying = playing || buffering
    stop()
    setMode(next)
    modeRef.current = next
    playlistRef.current = buildPlaylist(next)
    try {
      sessionStorage.setItem(MODE_STORAGE_KEY, next)
    } catch {
      // ignore storage quota/availability errors
    }
    // Continue from the same position in the other mode.
    if (wasPlaying && position !== null) startAt(position)
  }

  const changeSpeaker = (value: string) => {
    const position = absTime
    const wasPlaying = playing || buffering
    stop()
    // Picking a speaker implies speech-only playback (the filter has no
    // effect on full-audio windows). Not persisted — it's a side effect,
    // not the user's sticky mode preference.
    if (value && modeRef.current !== 'speech') {
      setMode('speech')
      modeRef.current = 'speech'
    }
    if (wasPlaying && position !== null) resumeAtRef.current = position
    setSpeakerFilter(value)
  }

  const cycleRate = () => {
    const next = PLAYBACK_RATES[(PLAYBACK_RATES.indexOf(rate) + 1) % PLAYBACK_RATES.length]
    setRate(next)
    rateRef.current = next
    if (audioRef.current) audioRef.current.playbackRate = next
  }

  const seekTo = (e: React.MouseEvent<HTMLDivElement>) => {
    if (duration <= 0 || playlistRef.current.length === 0) return
    const rect = e.currentTarget.getBoundingClientRect()
    const time = ((e.clientX - rect.left) / rect.width) * duration
    startAt(time)
  }

  const pct = (t: number) => `${Math.min(100, Math.max(0, (t / duration) * 100))}%`

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

      {/* Timeline: speech blocks, overlays (gaps), markers (split points), playhead */}
      <div
        onClick={seekTo}
        className="relative h-4 rounded bg-gray-200 dark:bg-gray-700 cursor-pointer overflow-hidden"
        title={
          mode === 'speech'
            ? 'Click to play from here (snaps forward to speech)'
            : 'Click to play from here'
        }
      >
        {(overlays || []).map((o, i) => (
          <div
            key={`o${i}`}
            className="absolute inset-y-0 bg-amber-200/70 dark:bg-amber-700/40"
            style={{ left: pct(o.start), width: pct(o.end - o.start) }}
          />
        ))}
        {(regions || []).map((r, i) => (
          <div
            key={`r${i}`}
            className={`absolute inset-y-0 rounded-sm ${
              speakerFilter
                ? 'bg-violet-500/80 dark:bg-violet-400/80'
                : 'bg-blue-500/80 dark:bg-blue-400/80'
            }`}
            style={{ left: pct(r.start), width: `max(2px, ${pct(r.end - r.start)})` }}
          />
        ))}
        {(markers || []).map((m, i) => (
          <div
            key={`m${i}`}
            className="absolute inset-y-0 w-0.5 bg-red-500"
            style={{ left: pct(m) }}
          />
        ))}
        {absTime !== null && (
          <div
            className="absolute inset-y-0 w-0.5 bg-gray-900 dark:bg-white"
            style={{ left: pct(absTime) }}
          />
        )}
      </div>
    </div>
  )
}
