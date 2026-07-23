import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Loader2, Pause, Play, RefreshCw, Sparkles, Volume2, X } from 'lucide-react'
import {
  annotationsApi,
  AuditSegment,
  BACKEND_URL,
  dataAuditApi,
  speakerApi,
} from '../../services/api'
import { getStorageKey } from '../../utils/storage'
import SpeakerNameDropdown from '../SpeakerNameDropdown'
import { confidenceBadgeClass, formatClock } from './format'

// Reserved label for background/noise (kept in sync with the backend
// constants.NOISE_LABEL). Applying it relabels the segment AND reclassifies it
// to non-speech; enrollment skips it.
const NOISE_LABEL = 'Noise'
const BACKGROUND_SPEECH_LABEL = 'Background Speech'

// Placeholder label for a speaker that couldn't be matched (kept in sync with the
// backend constants.UNKNOWN_SPEAKER_PREFIX). It relabels the segment for display
// but is never enrolled as a real voiceprint, so don't promote it into the
// enrolled-speaker list.
const UNKNOWN_SPEAKER = 'Unknown Speaker'

// Cap on concurrent live-identify calls so a long review list doesn't flood the
// speaker service.
const MAX_SUGGEST_INFLIGHT = 3

interface Enrolled {
  speaker_id: string
  name: string
}

interface PendingAnnotation {
  id: string
  segment_index: number
  corrected_speaker: string
}

interface Suggestion {
  loading: boolean
  name: string | null
  confidence: number | null
}

interface Props {
  conversationId: string
  /** Speaker labels already on the row — lets the parent skip refetching. */
  onDecisionsChanged?: () => void
  /** Stored confidence below this (threshold + margin) is a weak match: the
   *  segment carries a name but likely shouldn't, so it joins the review set. */
  marginalThreshold?: number
}

/**
 * Per-segment speaker triage. Surfaces the speech segments that weren't matched
 * to an enrolled speaker (the "needs review" bucket) so you can play each one,
 * see the closest enrolled match (live), and decide: assign it to a known/new
 * speaker, or mark it as background/noise. Each decision is persisted as a
 * diarization annotation (undo = delete); the toolbar's "Apply all" commits
 * them in bulk across every conversation you triaged.
 */
export default function SegmentTriage({
  conversationId,
  onDecisionsChanged,
  marginalThreshold,
}: Props) {
  const [segments, setSegments] = useState<AuditSegment[] | null>(null)
  const [enrolled, setEnrolled] = useState<Enrolled[]>([])
  const [annotations, setAnnotations] = useState<PendingAnnotation[]>([])
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<'review' | 'all'>('review')
  const [recent, setRecent] = useState<string[]>([])

  const [playingIndex, setPlayingIndex] = useState<number | null>(null)
  const [buffering, setBuffering] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const [suggestions, setSuggestions] = useState<Record<number, Suggestion>>({})
  const inflightRef = useRef(0)
  const requestedRef = useRef<Set<number>>(new Set())

  const token = () => localStorage.getItem(getStorageKey('token')) || ''

  const reloadAnnotations = useCallback(() => {
    return annotationsApi
      .getDiarizationAnnotations(conversationId)
      .then((res) => {
        const pending = (res.data as any[])
          .filter((a) => !a.processed)
          .map((a) => ({
            id: a.id,
            segment_index: a.segment_index,
            corrected_speaker: a.corrected_speaker,
          }))
        setAnnotations(pending)
      })
      .catch(() => {
        /* annotations are best-effort; ignore load errors */
      })
  }, [conversationId])

  // Initial load: segments, enrolled speakers, existing pending decisions.
  useEffect(() => {
    let cancelled = false
    setSegments(null)
    setError(null)
    dataAuditApi
      .getSegments(conversationId)
      .then((res) => {
        if (!cancelled) setSegments(res.data.segments)
      })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.error || 'Failed to load segments')
      })
    speakerApi
      .getEnrolledSpeakers()
      .then((res) => {
        if (cancelled) return
        const list = (res.data.speakers || []).map((s: any) => ({
          speaker_id: s.speaker_id || s.id,
          name: s.name,
        }))
        setEnrolled(list)
      })
      .catch(() => {
        /* enrolled list is optional for marking noise / new names */
      })
    reloadAnnotations()
    return () => {
      cancelled = true
    }
  }, [conversationId, reloadAnnotations])

  // Stop audio on unmount / conversation change.
  useEffect(() => {
    return () => {
      audioRef.current?.pause()
      audioRef.current = null
    }
  }, [conversationId])

  const pendingByIndex = useMemo(() => {
    const map = new Map<number, PendingAnnotation>()
    for (const a of annotations) map.set(a.segment_index, a)
    return map
  }, [annotations])

  const isUnidentified = (s: AuditSegment) =>
    s.segment_type === 'speech' && !s.identified_as

  // Identified, but at a confidence below the operating threshold+margin — a
  // weak match likely to be wrong (e.g. noise labeled as the nearest speaker).
  const isMarginal = useCallback(
    (s: AuditSegment) =>
      s.segment_type === 'speech' &&
      !!s.identified_as &&
      s.confidence != null &&
      marginalThreshold != null &&
      s.confidence < marginalThreshold,
    [marginalThreshold]
  )

  const needsReview = useCallback(
    (s: AuditSegment) => isUnidentified(s) || isMarginal(s),
    [isMarginal]
  )

  const visibleSegments = useMemo(() => {
    if (!segments) return []
    return segments.filter((s) => {
      if (s.segment_type !== 'speech') return false
      if (mode === 'all') return true
      // Review mode: unidentified or weak-match, plus anything with a pending
      // decision (so you can see/undo it).
      return needsReview(s) || pendingByIndex.has(s.index)
    })
  }, [segments, mode, pendingByIndex, needsReview])

  const reviewCount = useMemo(
    () => (segments || []).filter(needsReview).length,
    [segments, needsReview]
  )
  const speechCount = useMemo(
    () => (segments || []).filter((s) => s.segment_type === 'speech').length,
    [segments]
  )

  // --- Per-segment playback (one clip at a time) ---
  const stopAudio = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlayingIndex(null)
    setBuffering(false)
  }, [])

  const playSegment = (seg: AuditSegment) => {
    if (playingIndex === seg.index) {
      stopAudio()
      return
    }
    stopAudio()
    const url =
      `${BACKEND_URL}/api/audio/chunks/${conversationId}` +
      `?start_time=${seg.start.toFixed(2)}&end_time=${seg.end.toFixed(2)}&format=wav&token=${token()}`
    const audio = new Audio(url)
    audioRef.current = audio
    setPlayingIndex(seg.index)
    setBuffering(true)
    audio.addEventListener('playing', () => setBuffering(false))
    audio.addEventListener('ended', () => stopAudio())
    audio.addEventListener('error', () => {
      setError('Playback failed')
      stopAudio()
    })
    audio.play().catch(() => stopAudio())
    // Playing a row is a strong "look at this" signal — fetch its suggestion.
    fetchSuggestion(seg)
  }

  // --- Live speaker suggestion (closest enrolled match for the clip) ---
  const fetchSuggestion = useCallback(
    (seg: AuditSegment) => {
      if (requestedRef.current.has(seg.index)) return
      if (inflightRef.current >= MAX_SUGGEST_INFLIGHT) return
      requestedRef.current.add(seg.index)
      inflightRef.current += 1
      setSuggestions((prev) => ({
        ...prev,
        [seg.index]: { loading: true, name: null, confidence: null },
      }))
      dataAuditApi
        .identifySegment(conversationId, seg.start, seg.end)
        .then((res) => {
          setSuggestions((prev) => ({
            ...prev,
            [seg.index]: {
              loading: false,
              name: res.data.speaker_name,
              confidence: res.data.confidence,
            },
          }))
        })
        .catch(() => {
          setSuggestions((prev) => ({
            ...prev,
            [seg.index]: { loading: false, name: null, confidence: null },
          }))
          // Allow a retry on failure.
          requestedRef.current.delete(seg.index)
        })
        .finally(() => {
          inflightRef.current = Math.max(0, inflightRef.current - 1)
        })
    },
    [conversationId]
  )

  // Auto-suggest for the review list (bounded), but not for the (potentially
  // huge) "all" list — there it's play/button triggered only.
  useEffect(() => {
    if (mode !== 'review') return
    for (const seg of visibleSegments) {
      if (pendingByIndex.has(seg.index)) continue
      if (suggestions[seg.index]) continue
      if (inflightRef.current >= MAX_SUGGEST_INFLIGHT) break
      fetchSuggestion(seg)
    }
  }, [mode, visibleSegments, pendingByIndex, suggestions, fetchSuggestion])

  // --- Decisions: create / undo a diarization annotation ---
  const decide = async (seg: AuditSegment, correctedSpeaker: string) => {
    try {
      await annotationsApi.createDiarizationAnnotation({
        conversation_id: conversationId,
        segment_index: seg.index,
        original_speaker: seg.identified_as || seg.speaker || '',
        corrected_speaker: correctedSpeaker,
        segment_start_time: seg.segment_start_time,
      })
      if (![NOISE_LABEL, BACKGROUND_SPEECH_LABEL, UNKNOWN_SPEAKER].includes(correctedSpeaker)) {
        setRecent((prev) => [correctedSpeaker, ...prev.filter((s) => s !== correctedSpeaker)])
        setEnrolled((prev) =>
          prev.some((s) => s.name === correctedSpeaker)
            ? prev
            : [...prev, { speaker_id: `pending_${correctedSpeaker}`, name: correctedSpeaker }]
        )
      }
      await reloadAnnotations()
      onDecisionsChanged?.()
    } catch {
      setError('Failed to save decision')
    }
  }

  const undo = async (annotationId: string) => {
    try {
      await annotationsApi.deleteAnnotation(annotationId)
      await reloadAnnotations()
      onDecisionsChanged?.()
    } catch {
      setError('Failed to undo decision')
    }
  }

  if (error && segments === null) {
    return <div className="text-xs text-red-600 dark:text-red-400 px-1 py-2">{error}</div>
  }

  return (
    <div className="mt-2 space-y-2 rounded-lg bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 p-3">
      {/* Header */}
      <div className="flex items-center gap-3 text-sm text-gray-700 dark:text-gray-200">
        <span className="font-medium">Speaker triage</span>
        <div className="flex rounded border border-gray-300 dark:border-gray-600 overflow-hidden text-xs">
          {(['review', 'all'] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-2 py-0.5 transition-colors ${
                mode === m
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
            >
              {m === 'review' ? 'Needs review' : 'All segments'}
            </button>
          ))}
        </div>
        {segments !== null && (
          <span className="text-gray-400 text-xs">
            {reviewCount} to review · {speechCount} speech
          </span>
        )}
        <div className="flex-1" />
        <button
          onClick={() => {
            setSuggestions({})
            requestedRef.current = new Set()
            reloadAnnotations()
          }}
          className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          title="Refresh"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>

      {error && <div className="text-xs text-red-600 dark:text-red-400">{error}</div>}

      {/* Segment list */}
      {segments === null ? (
        <div className="text-xs text-gray-400 px-1 py-2">Loading segments…</div>
      ) : visibleSegments.length === 0 ? (
        <div className="text-xs text-gray-400 px-1 py-2">
          {mode === 'review'
            ? 'Nothing to review — every speech segment is identified. 🎉'
            : 'No speech segments.'}
        </div>
      ) : (
        <div className="divide-y divide-gray-100 dark:divide-gray-800">
          {visibleSegments.map((seg) => {
            const pending = pendingByIndex.get(seg.index)
            const sug = suggestions[seg.index]
            const isBackgroundPending = [NOISE_LABEL, BACKGROUND_SPEECH_LABEL].includes(
              pending?.corrected_speaker || ''
            )
            return (
              <div
                key={seg.index}
                className={`flex items-center gap-2 py-1.5 ${
                  pending
                    ? `border-l-2 pl-2 ${isBackgroundPending ? 'border-zinc-400' : 'border-blue-400'}`
                    : 'pl-2'
                }`}
              >
                {/* Play */}
                <button
                  onClick={() => playSegment(seg)}
                  className={`flex-shrink-0 p-1 rounded-full transition-colors ${
                    playingIndex === seg.index
                      ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200'
                      : 'text-gray-400 hover:text-blue-600 hover:bg-gray-100 dark:hover:bg-gray-700'
                  }`}
                  title="Play this segment"
                >
                  {playingIndex === seg.index && buffering ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : playingIndex === seg.index ? (
                    <Pause className="h-3.5 w-3.5" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                </button>

                {/* Time */}
                <span className="flex-shrink-0 text-xs font-mono text-gray-400 whitespace-nowrap">
                  {formatClock(seg.start)}–{formatClock(seg.end)}
                </span>

                {/* Label + stored confidence */}
                <span
                  className={`flex-shrink-0 px-1.5 py-0.5 rounded text-xs whitespace-nowrap ${
                    !seg.identified_as
                      ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                      : isMarginal(seg)
                        ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300'
                        : 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                  }`}
                  title={
                    !seg.identified_as
                      ? 'Not matched to an enrolled speaker'
                      : isMarginal(seg)
                        ? 'Weak match (low confidence) — likely wrong, review'
                        : 'Identified'
                  }
                >
                  {seg.identified_as || seg.speaker || 'unknown'}
                  {seg.identified_as != null &&
                    seg.confidence != null &&
                    seg.confidence > 0 &&
                    seg.confidence < 1 && (
                      <span className="ml-1 opacity-60">· {seg.confidence.toFixed(2)}</span>
                    )}
                </span>

                {/* Text — wraps (line-clamped) rather than nowrap-truncate, so a long
                    transcript line can't balloon the table cell and force horizontal scroll. */}
                <span className="flex-1 min-w-0 break-words line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                  {seg.text}
                </span>

                {/* Decision / actions */}
                {pending ? (
                  <span className="flex-shrink-0 flex items-center gap-1">
                    <span
                      className={`px-1.5 py-0.5 rounded text-xs ${
                        isBackgroundPending
                          ? 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200'
                          : 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-200'
                      }`}
                    >
                      → {pending.corrected_speaker}
                    </span>
                    <button
                      onClick={() => undo(pending.id)}
                      className="text-gray-400 hover:text-red-500"
                      title="Undo this decision"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </span>
                ) : (
                  <span className="flex-shrink-0 flex items-center gap-1.5">
                    {/* Live suggestion */}
                    {sug?.loading ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-gray-300" />
                    ) : sug?.name ? (
                      <button
                        onClick={() => decide(seg, sug.name as string)}
                        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs hover:opacity-80 ${
                          sug.confidence != null
                            ? confidenceBadgeClass(sug.confidence)
                            : 'bg-gray-100 text-gray-600'
                        }`}
                        title="Assign the closest enrolled speaker (live match)"
                      >
                        <Sparkles className="h-3 w-3" />
                        {sug.name}
                        {sug.confidence != null && (
                          <span className="opacity-70">· {sug.confidence.toFixed(2)}</span>
                        )}
                      </button>
                    ) : (
                      <button
                        onClick={() => fetchSuggestion(seg)}
                        className="text-gray-300 hover:text-blue-500"
                        title="Suggest closest enrolled speaker"
                      >
                        <Sparkles className="h-3.5 w-3.5" />
                      </button>
                    )}

                    {/* Assign (existing or new) */}
                    <span className="px-1.5 py-0.5 rounded text-xs border border-gray-300 dark:border-gray-600">
                      <SpeakerNameDropdown
                        currentSpeaker={seg.identified_as || 'Assign'}
                        enrolledSpeakers={enrolled}
                        onSpeakerChange={(name) => decide(seg, name)}
                        segmentIndex={seg.index}
                        conversationId={conversationId}
                        recentSpeakers={recent}
                      />
                    </span>

                    {/* Noise */}
                    <button
                      onClick={() => decide(seg, NOISE_LABEL)}
                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs border border-zinc-300 dark:border-zinc-600 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-700"
                      title="Mark as background/noise (not a person)"
                    >
                      <Volume2 className="h-3 w-3" />
                      Noise
                    </button>
                    <button
                      onClick={() => decide(seg, BACKGROUND_SPEECH_LABEL)}
                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs border border-zinc-300 dark:border-zinc-600 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-700"
                      title="Mark as speech from TV, media, or another background source"
                    >
                      <Volume2 className="h-3 w-3" />
                      BG speech
                    </button>
                  </span>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
