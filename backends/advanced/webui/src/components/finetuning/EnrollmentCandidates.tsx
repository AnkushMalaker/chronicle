import { useState, useEffect, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, Pause, RefreshCw, ShieldCheck, Check, AlertTriangle } from 'lucide-react'
import { finetuningApi } from '../../services/api'
import { useGaplessPlayer } from '../../hooks/useGaplessPlayer'

interface Clip {
  conversation_id: string
  conversation_title: string
  segment_index: number
  start: number
  end: number
  duration: number
  text: string
  gated_in: boolean
  default_selected: boolean
  reasons: string[]
}
interface SpeakerGroup {
  speaker: string
  clips: Clip[]
  selected_count: number
}
interface CandidatesResponse {
  candidates: SpeakerGroup[]
  min_duration: number
  default_per_speaker: number
  conversation_count: number
}

const clipKey = (c: Clip) => `${c.conversation_id}:${c.segment_index}`

/**
 * Curated speaker enrollment: shows quality-gated candidate clips (per speaker)
 * built from each conversation's ACTIVE transcript version, pre-ticks the clean
 * ones (>= min duration, no cross-talk, deduped), and enrolls ONLY what you
 * confirm. Replaces the old "process every applied annotation" blast that
 * mismatched audio↔label and enrolled overlap/short scraps.
 */
export default function EnrollmentCandidates() {
  const qc = useQueryClient()
  const player = useGaplessPlayer()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [enrolling, setEnrolling] = useState(false)
  const [resultMsg, setResultMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { data, isLoading, refetch, isFetching } = useQuery<CandidatesResponse>({
    queryKey: ['finetuning', 'enrollmentCandidates'],
    queryFn: () => finetuningApi.getEnrollmentCandidates().then((r) => r.data),
  })

  // Seed the selection from the gate's defaults whenever fresh data arrives.
  useEffect(() => {
    if (!data) return
    const s = new Set<string>()
    data.candidates.forEach((g) => g.clips.forEach((c) => c.default_selected && s.add(clipKey(c))))
    setSelected(s)
  }, [data])

  const allClips = useMemo(
    () => (data?.candidates || []).flatMap((g) => g.clips.map((c) => ({ ...c, speaker: g.speaker }))),
    [data]
  )
  const selectedClips = useMemo(
    () => allClips.filter((c) => selected.has(clipKey(c))),
    [allClips, selected]
  )

  const toggle = (c: Clip) => {
    setSelected((prev) => {
      const n = new Set(prev)
      const k = clipKey(c)
      n.has(k) ? n.delete(k) : n.add(k)
      return n
    })
  }

  const playClip = (c: Clip) => {
    const segId = `enroll-${clipKey(c)}`
    if (player.playingSegmentId === segId) player.stop()
    else player.playSegment(c.conversation_id, segId, c.start, c.end)
  }

  const handleEnroll = async () => {
    if (selectedClips.length === 0) return
    setError(null)
    setResultMsg(null)
    setEnrolling(true)
    try {
      const payload = selectedClips.map((c) => ({
        conversation_id: c.conversation_id,
        segment_index: c.segment_index,
        start: c.start,
        end: c.end,
        speaker: (c as any).speaker,
      }))
      const { data: res } = await finetuningApi.enrollSelectedClips(payload)
      const parts = [
        `${res.total_enrolled} enrolled (${res.enrolled_new} new, ${res.appended} appended)`,
      ]
      if (res.failed) parts.push(`${res.failed} failed`)
      if (res.skipped) parts.push(`${res.skipped} skipped`)
      setResultMsg(parts.join(', '))
      qc.invalidateQueries({ queryKey: ['finetuning'] })
      await refetch()
    } catch (e: any) {
      setError(e.response?.data?.detail || e.response?.data?.message || e.message || 'Enrollment failed')
    } finally {
      setEnrolling(false)
    }
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center space-x-2">
          <ShieldCheck className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Curated Speaker Enrollment</h2>
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          <span>Refresh</span>
        </button>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Clips from each conversation's active version, gated for quality (≥ {data?.min_duration ?? 3}s, no cross-talk,
        deduped). Clean clips are pre-selected; greyed clips are excluded with a reason — tick them to override.
        Only the checked clips are enrolled.
      </p>

      {resultMsg && (
        <div className="mb-4 p-3 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-700 rounded-lg flex items-center space-x-2">
          <Check className="h-4 w-4 text-green-600 dark:text-green-400" />
          <span className="text-sm text-green-700 dark:text-green-300">{resultMsg}</span>
        </div>
      )}
      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg flex items-center space-x-2">
          <AlertTriangle className="h-4 w-4 text-red-600 dark:text-red-400" />
          <span className="text-sm text-red-700 dark:text-red-300">{error}</span>
        </div>
      )}

      {isLoading ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">Loading candidates…</p>
      ) : !data || data.candidates.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400 italic">
          No enrollment candidates — annotate and apply speaker labels on a conversation, then they'll appear here.
        </p>
      ) : (
        <>
          <div className="space-y-5">
            {data.candidates.map((group) => (
              <div key={group.speaker}>
                <div className="flex items-center space-x-2 mb-1.5">
                  <h3 className="font-medium text-gray-900 dark:text-gray-100">{group.speaker}</h3>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {group.clips.filter((c) => selected.has(clipKey(c))).length} of {group.clips.length} selected
                  </span>
                </div>
                <div className="space-y-1">
                  {group.clips.map((c) => {
                    const isSel = selected.has(clipKey(c))
                    const segId = `enroll-${clipKey(c)}`
                    const playing = player.playingSegmentId === segId
                    return (
                      <div
                        key={clipKey(c)}
                        className={`flex items-center gap-2 px-2 py-1.5 rounded border ${
                          isSel
                            ? 'border-emerald-300 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-900/15'
                            : c.gated_in
                            ? 'border-gray-200 dark:border-gray-700'
                            : 'border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/40 opacity-70'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={isSel}
                          onChange={() => toggle(c)}
                          className="h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
                        />
                        <button
                          onClick={() => playClip(c)}
                          className="flex-shrink-0 p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
                          title={`Play ${c.duration}s`}
                        >
                          {playing ? <Pause className="h-3.5 w-3.5 text-emerald-600" /> : <Play className="h-3.5 w-3.5 text-gray-500" />}
                        </button>
                        <span className="text-xs tabular-nums text-gray-500 dark:text-gray-400 w-12 flex-shrink-0">{c.duration}s</span>
                        <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1" title={c.text}>
                          {c.text || <em className="text-gray-400">(no text)</em>}
                        </span>
                        {!c.gated_in && (
                          <span className="text-xs text-amber-600 dark:text-amber-400 flex-shrink-0" title={c.reasons.join(', ')}>
                            {c.reasons.join(', ')}
                          </span>
                        )}
                        <span
                          className="text-xs text-gray-400 dark:text-gray-500 truncate max-w-[10rem] flex-shrink-0 hidden sm:inline"
                          title={c.conversation_title}
                        >
                          {c.conversation_title}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>

          <div className="mt-5 flex items-center justify-between">
            <span className="text-sm text-gray-600 dark:text-gray-400">
              {selectedClips.length} clip{selectedClips.length === 1 ? '' : 's'} selected across{' '}
              {new Set(selectedClips.map((c) => (c as any).speaker)).size} speaker(s)
            </span>
            <button
              onClick={handleEnroll}
              disabled={enrolling || selectedClips.length === 0}
              className="flex items-center space-x-2 px-6 py-2.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            >
              {enrolling ? <RefreshCw className="h-5 w-5 animate-spin" /> : <ShieldCheck className="h-5 w-5" />}
              <span>Enroll {selectedClips.length} selected</span>
            </button>
          </div>
        </>
      )}
    </div>
  )
}
