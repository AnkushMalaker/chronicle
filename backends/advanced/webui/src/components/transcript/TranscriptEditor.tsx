import { useCallback, useEffect, useMemo, useState } from 'react'
import { Play, Pause, X, Check, RefreshCw, Trash2, Eye, EyeOff, Plus } from 'lucide-react'
import { annotationsApi } from '../../services/api'
import { useGaplessPlayer } from '../../hooks/useGaplessPlayer'
import SpeakerNameDropdown from '../SpeakerNameDropdown'
import { PlayheadWaveform, PlayheadTimeLabel } from '../audio/PlayheadWaveform'
import { WaveformRegionEditor, Region } from '../audio/WaveformRegionEditor'
import InsertSegmentForm from './InsertSegmentForm'
import { useWaveformZoomDisabled } from './useWaveformZoom'

export interface Segment {
  start: number
  end: number
  text: string
  speaker?: string
  segment_type?: string
  identified_as?: string | null
  confidence?: number | null
}

interface TranscriptEditorProps {
  conversationId: string
  segments: Segment[]
  duration?: number
  hasAudio: boolean
  /** Show the audio waveform + enable timing edits (detail page). List can omit it. */
  showWaveform?: boolean
  isLive?: boolean
  enrolledSpeakers: { speaker_id: string; name: string }[]
  hideUnknownSpeakers?: boolean
  /** Called after annotations are applied (parent should refetch the conversation). */
  onChanged?: () => void
}

const SPEAKER_COLOR_PALETTE = [
  'text-blue-700 dark:text-blue-300',
  'text-emerald-700 dark:text-emerald-300',
  'text-purple-700 dark:text-purple-300',
  'text-orange-700 dark:text-orange-300',
  'text-pink-700 dark:text-pink-300',
  'text-cyan-700 dark:text-cyan-300',
  'text-amber-700 dark:text-amber-300',
  'text-indigo-700 dark:text-indigo-300',
]

const formatDuration = (seconds: number) => {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

const isUnknownSpeakerLabel = (name?: string): boolean => {
  if (!name) return true
  const n = name.trim().toLowerCase()
  return n === '' || n === 'unknown' || n.startsWith('unknown speaker') || n === 'background' || n === 'noise'
}

/**
 * The single transcript+segment editor used by BOTH the conversation list (expanded card)
 * and the detail page. Owns its own annotation/edit state for one conversation:
 * speaker dropdown, click-to-edit text, the shared insert menu, and — when `showWaveform`
 * — the waveform with in-place timing (move/resize) editing that auto-opens when you edit
 * a segment's text (unless disabled via the waveform-zoom setting).
 */
export default function TranscriptEditor({
  conversationId,
  segments,
  duration,
  hasAudio,
  showWaveform = true,
  isLive = false,
  enrolledSpeakers,
  hideUnknownSpeakers = false,
  onChanged,
}: TranscriptEditorProps) {
  const player = useGaplessPlayer()
  const [zoomDisabled] = useWaveformZoomDisabled()

  const [diar, setDiar] = useState<any[]>([])
  const [text, setText] = useState<any[]>([])
  const [inserts, setInserts] = useState<any[]>([])
  const [timing, setTiming] = useState<any[]>([])
  const [deletions, setDeletions] = useState<any[]>([])

  const [editingSegment, setEditingSegment] = useState<number | null>(null)
  const [editedText, setEditedText] = useState('')
  const [savingSegment, setSavingSegment] = useState(false)
  const [segmentEditError, setSegmentEditError] = useState<string | null>(null)

  const [timingEditSegment, setTimingEditSegment] = useState<number | null>(null)
  // Live region of the linked timing editor while editing a segment's text — null
  // until the user actually drags (so an untouched waveform saves no timing change).
  const [timingRegion, setTimingRegion] = useState<Region | null>(null)
  const [regionError, setRegionError] = useState<string | null>(null)
  const [insertOpen, setInsertOpen] = useState<number | null>(null) // afterIndex

  const [recentSpeakers, setRecentSpeakers] = useState<string[]>([])
  const [hoverMarker, setHoverMarker] = useState<{ start: number; end: number } | null>(null)
  const [preview, setPreview] = useState(false)
  const [applying, setApplying] = useState(false)
  const [clearing, setClearing] = useState(false)
  // While inserting with the waveform open, the region drawn on it for the new segment.
  const [insertRegion, setInsertRegion] = useState<Region | null>(null)
  // Whether the insert menu drives the top waveform (draw the new segment's span).
  const insertOnWaveform = showWaveform && hasAudio && !!duration

  const reload = useCallback(async () => {
    const [d, t, i, tm, dl] = await Promise.all([
      annotationsApi.getDiarizationAnnotations(conversationId),
      annotationsApi.getTranscriptAnnotations(conversationId),
      annotationsApi.getInsertAnnotations(conversationId),
      annotationsApi.getTimingAnnotations(conversationId),
      annotationsApi.getDeletionAnnotations(conversationId),
    ])
    setDiar(d.data)
    setText(t.data)
    setInserts(i.data)
    setTiming(tm.data)
    setDeletions(dl.data)
  }, [conversationId])

  useEffect(() => {
    reload().catch(() => {})
  }, [reload])

  const pendingDiar = useMemo(() => diar.filter((a) => !a.processed), [diar])
  const pendingText = useMemo(() => text.filter((a) => !a.processed), [text])
  const pendingInsert = useMemo(() => inserts.filter((a) => !a.processed), [inserts])
  const pendingTiming = useMemo(() => timing.filter((a) => !a.processed), [timing])
  const pendingDeletion = useMemo(() => deletions.filter((a) => !a.processed), [deletions])
  const totalPending = pendingDiar.length + pendingText.length + pendingInsert.length + pendingTiming.length + pendingDeletion.length

  const allSpeakers = useMemo(() => {
    const list = [...enrolledSpeakers]
    const names = new Set(list.map((s) => s.name))
    diar.forEach((a) => {
      if (a.corrected_speaker && !names.has(a.corrected_speaker)) {
        list.push({ speaker_id: `annotation_${a.corrected_speaker}`, name: a.corrected_speaker })
        names.add(a.corrected_speaker)
      }
    })
    return list
  }, [enrolledSpeakers, diar])

  const speakerColorMap = useMemo(() => {
    const map: Record<string, string> = {}
    let i = 0
    segments.forEach((seg) => {
      const sp = seg.speaker || 'Unknown'
      if (!map[sp]) {
        map[sp] = SPEAKER_COLOR_PALETTE[i % SPEAKER_COLOR_PALETTE.length]
        i++
      }
    })
    return map
  }, [segments])

  // Auto-open the timing editor on the main waveform when editing a segment's text
  // (unless the user disabled waveform zoom). Closing the text edit closes it too.
  useEffect(() => {
    if (!showWaveform || zoomDisabled || !hasAudio) return
    if (editingSegment !== null) setTimingEditSegment(editingSegment)
  }, [editingSegment, showWaveform, zoomDisabled, hasAudio])

  useEffect(() => {
    if (editingSegment === null) setTimingEditSegment(null)
    setTimingRegion(null) // fresh segment / closed = no pending drag
  }, [editingSegment])

  const noteRecent = (s: string) => setRecentSpeakers((p) => [s, ...p.filter((x) => x !== s)])

  const regionForSegment = (idx: number): Region => {
    const ta = pendingTiming.find((a) => a.segment_index === idx)
    if (ta) return { start: ta.new_start, end: ta.new_end }
    const s = segments[idx]
    return { start: s.start, end: s.end }
  }

  // ---- handlers ----
  const handleSpeakerChange = async (
    segmentIndex: number,
    originalSpeaker: string,
    newSpeaker: string,
    startTime: number
  ) => {
    const existing = pendingDiar.find((a) => a.segment_index === segmentIndex)
    if (existing) await annotationsApi.updateAnnotation(existing.id, { corrected_speaker: newSpeaker })
    else
      await annotationsApi.createDiarizationAnnotation({
        conversation_id: conversationId,
        segment_index: segmentIndex,
        original_speaker: originalSpeaker,
        corrected_speaker: newSpeaker,
        segment_start_time: startTime,
      })
    noteRecent(newSpeaker)
    await reload()
  }

  const handleStartEdit = (idx: number, original: string) => {
    setInsertOpen(null) // text edit and insert are mutually exclusive on the waveform
    setInsertRegion(null)
    setEditingSegment(idx)
    setEditedText(original)
    setSegmentEditError(null)
  }

  const handleSaveEdit = async (idx: number, original: string) => {
    if (!editedText.trim()) {
      setSegmentEditError('Segment text cannot be empty')
      return
    }
    // One Save commits BOTH the text edit and any timing drag from the linked
    // waveform editor — so adjusting the span while editing text isn't silently lost.
    const seg = segments[idx]
    const textChanged = editedText !== original
    const timingChanged =
      !!timingRegion &&
      (Math.abs(timingRegion.start - seg.start) > 0.02 || Math.abs(timingRegion.end - seg.end) > 0.02)

    if (!textChanged && !timingChanged) {
      setEditingSegment(null)
      return
    }
    try {
      setSavingSegment(true)
      setSegmentEditError(null)
      if (textChanged) {
        const existing = pendingText.find((a) => a.segment_index === idx)
        if (existing) await annotationsApi.updateAnnotation(existing.id, { corrected_text: editedText })
        else
          await annotationsApi.createTranscriptAnnotation({
            conversation_id: conversationId,
            segment_index: idx,
            original_text: original,
            corrected_text: editedText,
          })
      }
      if (timingChanged && timingRegion) {
        await handleSaveTiming(idx, timingRegion)
      }
      setEditingSegment(null)
      setEditedText('')
      await reload()
    } catch (err: any) {
      setSegmentEditError(err.response?.data?.detail || err.message || 'Failed to save')
    } finally {
      setSavingSegment(false)
    }
  }

  const handleSaveTiming = async (idx: number, region: Region) => {
    // Upsert: one pending timing per segment (drop a prior pending one for this
    // segment so re-dragging doesn't pile up duplicate annotations).
    setRegionError(null)
    const prior = pendingTiming.find((a) => a.segment_index === idx)
    if (prior) await annotationsApi.deleteAnnotation(prior.id)
    await annotationsApi.createTimingAnnotation({
      conversation_id: conversationId,
      segment_index: idx,
      new_start: region.start,
      new_end: region.end,
    })
    await reload()
  }

  const handlePlayRegion = (region: Region) =>
    player.playSegment(conversationId, `${conversationId}-region`, region.start, region.end)

  const handleDeleteAnnotation = async (annotationId: string) => {
    await annotationsApi.deleteAnnotation(annotationId)
    await reload()
  }

  // Toggle a pending "delete this segment" mark. Clicking again undoes it.
  const handleToggleDeleteSegment = async (idx: number) => {
    const existing = pendingDeletion.find((a) => a.segment_index === idx)
    if (existing) await annotationsApi.deleteAnnotation(existing.id)
    else
      await annotationsApi.createDeletionAnnotation({
        conversation_id: conversationId,
        segment_index: idx,
      })
    await reload()
  }

  const handleSegmentPlayPause = (idx: number, segment: Segment) => {
    const segId = `${conversationId}-${idx}`
    if (player.playingSegmentId === segId) player.stop()
    else player.playSegment(conversationId, segId, segment.start, segment.end)
  }

  const handleApply = async () => {
    try {
      setApplying(true)
      await annotationsApi.applyAllAnnotations(conversationId)
      setPreview(false)
      onChanged?.()
      await reload()
    } finally {
      setApplying(false)
    }
  }

  const handleClear = async () => {
    if (!confirm(`Discard ${totalPending} pending correction(s)?`)) return
    try {
      setClearing(true)
      await Promise.all(
        [...pendingDiar, ...pendingText, ...pendingInsert, ...pendingTiming, ...pendingDeletion].map((a) =>
          annotationsApi.deleteAnnotation(a.id)
        )
      )
      await reload()
    } finally {
      setClearing(false)
    }
  }

  // ---- insert divider (hover "+" + pending inserts + form) ----
  // Time at a gap (after segment `afterIndex`, -1 = before first) — where the waveform
  // zooms to when inserting there.
  const gapTime = (afterIndex: number) => {
    if (afterIndex < 0) return segments[0]?.start ?? 0
    return segments[afterIndex]?.end ?? (duration ?? 0)
  }
  const openInsert = (afterIndex: number) => {
    setEditingSegment(null) // close any timing edit
    setInsertRegion(null)
    setInsertOpen(afterIndex)
  }
  const closeInsert = () => {
    setInsertOpen(null)
    setInsertRegion(null)
  }

  const InsertDivider = ({ afterIndex }: { afterIndex: number }) => {
    const here = pendingInsert.filter((a) => a.insert_after_index === afterIndex)
    const open = insertOpen === afterIndex
    return (
      <div className="group/ins relative py-0.5">
        {here.map((ins) => (
          <div
            key={ins.id}
            className="text-sm border-l-2 border-purple-400 dark:border-purple-600 pl-3 py-0.5 px-2 flex items-center justify-between bg-purple-50 dark:bg-purple-900/20 rounded-r"
          >
            <span className={ins.insert_segment_type === 'speech' ? '' : 'italic text-gray-500'}>
              {ins.insert_segment_type === 'speech' ? (
                <>
                  <span className="font-medium text-blue-600 dark:text-blue-400">{ins.insert_speaker || 'Speaker'}</span>: {ins.insert_text || <em className="text-gray-400">(empty)</em>}
                </>
              ) : ins.insert_segment_type === 'note' ? (
                `[Note: ${ins.insert_text}]`
              ) : (
                ins.insert_text
              )}
              <span className="text-xs bg-purple-100 dark:bg-purple-900 text-purple-600 dark:text-purple-300 px-2 py-0.5 rounded ml-2">
                Pending Insert
              </span>
            </span>
            <button
              onClick={() => handleDeleteAnnotation(ins.id)}
              className="ml-2 text-gray-400 hover:text-red-500"
              title="Remove insert"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        ))}
        {/* When the waveform is available, the insert form moves up next to it (so you can
            draw the new segment's span). Otherwise it appears inline here. */}
        {open && !insertOnWaveform && (
          <InsertSegmentForm
            conversationId={conversationId}
            afterIndex={afterIndex}
            allSpeakers={allSpeakers}
            recentSpeakers={recentSpeakers}
            onSpeakerUsed={noteRecent}
            onDone={async () => {
              closeInsert()
              await reload()
            }}
            onCancel={closeInsert}
          />
        )}
        {!open && !preview && (
          <button
            onClick={() => openInsert(afterIndex)}
            className="absolute left-1/2 -translate-x-1/2 -translate-y-1/2 top-1/2 z-10 opacity-0 group-hover/ins:opacity-60 hover:!opacity-100 transition-opacity px-1.5 leading-tight text-xs text-gray-400 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 rounded-full hover:text-purple-500 hover:border-purple-400"
            title="Insert a segment here"
          >
            +
          </button>
        )}
      </div>
    )
  }

  // ---- render ----
  const showAudio = showWaveform && hasAudio && conversationId && !!duration
  // While actively editing timing / inserting, pin the waveform to the top of the
  // viewport so it stays reachable when the segment you're editing is far down the
  // transcript (no scrolling back up to the player).
  const editorActive = timingEditSegment !== null || insertOpen !== null

  return (
    <div className="space-y-3">
      {/* Waveform — doubles as the timing editor while editing a segment */}
      {showAudio && (
        <div
          className={
            editorActive
              ? 'sticky top-0 z-20 bg-white dark:bg-gray-800 pt-2 pb-3 -mt-2 shadow-[0_6px_8px_-6px_rgba(0,0,0,0.25)] dark:shadow-[0_6px_8px_-6px_rgba(0,0,0,0.6)]'
              : undefined
          }
        >
          {timingEditSegment !== null ? (
            <>
              <WaveformRegionEditor
                key={`timing-${timingEditSegment}`}
                conversationId={conversationId}
                duration={duration!}
                initialRegion={regionForSegment(timingEditSegment)}
                commitMode="linked"
                onChange={setTimingRegion}
                onCancel={() => setEditingSegment(null)}
                onPlay={handlePlayRegion}
                height={96}
              />
              {regionError && <p className="text-xs text-red-500 mt-1">{regionError}</p>}
            </>
          ) : insertOpen !== null ? (
            <>
              <div className="text-xs text-purple-600 dark:text-purple-400 mb-1">
                {insertOpen < 0
                  ? 'Inserting before the first segment'
                  : `Inserting after segment ${insertOpen + 1}`}
                {' — drag on the waveform to set its time span (optional).'}
              </div>
              <WaveformRegionEditor
                conversationId={conversationId}
                duration={duration!}
                initialRegion={null}
                focusTime={gapTime(insertOpen)}
                pickerMode
                onChange={setInsertRegion}
                onCancel={closeInsert}
                onPlay={handlePlayRegion}
                height={96}
              />
              <div className="mt-2">
                <InsertSegmentForm
                  conversationId={conversationId}
                  afterIndex={insertOpen}
                  allSpeakers={allSpeakers}
                  recentSpeakers={recentSpeakers}
                  onSpeakerUsed={noteRecent}
                  region={insertRegion}
                  onDone={async () => {
                    closeInsert()
                    await reload()
                  }}
                  onCancel={closeInsert}
                />
              </div>
            </>
          ) : (
            <div className="flex items-center gap-3">
              <div className="flex-1">
                <PlayheadWaveform
                  cid={conversationId}
                  duration={duration!}
                  onSeek={(t) => player.play(conversationId, t, { totalDuration: duration! })}
                  height={80}
                  segments={segments}
                  segmentMarker={player.segmentMarker}
                  hoverMarker={hoverMarker}
                />
                <div className="flex items-center gap-3 mt-2">
                  <button
                    onClick={() => player.togglePlay(conversationId, duration!)}
                    className="p-2 rounded-full bg-blue-600 hover:bg-blue-700 text-white"
                    title={player.isActive(conversationId) && player.isPlaying ? 'Pause' : 'Play'}
                  >
                    {player.isActive(conversationId) && player.isPlaying ? (
                      <Pause className="w-4 h-4" />
                    ) : (
                      <Play className="w-4 h-4" />
                    )}
                  </button>
                  <PlayheadTimeLabel cid={conversationId} total={duration} className="text-sm text-gray-600 dark:text-gray-400 font-mono" />
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Pending corrections bar */}
      {totalPending > 0 && (
        <div className="flex items-center justify-between gap-3 px-3 py-2 bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-800 rounded-lg">
          <span className="text-sm text-orange-700 dark:text-orange-300">
            {totalPending} pending correction{totalPending === 1 ? '' : 's'} ({pendingDiar.length} speaker, {pendingText.length} text, {pendingInsert.length} insert, {pendingTiming.length} timing, {pendingDeletion.length} delete) — not yet applied
          </span>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={() => setPreview((p) => !p)}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
              title="Preview the corrected transcript"
            >
              {preview ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              {preview ? 'Exit preview' : 'Preview'}
            </button>
            <button
              onClick={handleApply}
              disabled={applying || clearing}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
              title="Create a new transcript version with these corrections and reprocess memory"
            >
              {applying ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              Apply
            </button>
            <button
              onClick={handleClear}
              disabled={applying || clearing}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50"
              title="Discard all pending corrections"
            >
              {clearing ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              Clear
            </button>
          </div>
        </div>
      )}

      {/* Segments */}
      {segments.length > 0 ? (
        <div className="space-y-0.5">
          <InsertDivider afterIndex={-1} />
          {segments.map((segment, idx) => {
            const speaker = segment.speaker || 'Unknown'
            const isEvent = segment.segment_type === 'event'
            const isNote = segment.segment_type === 'note'
            if (hideUnknownSpeakers && !isNote && isUnknownSpeakerLabel(speaker)) return <InsertDivider key={`d${idx}`} afterIndex={idx} />

            const speakerColor = speakerColorMap[speaker] || SPEAKER_COLOR_PALETTE[0]
            const isEditing = editingSegment === idx
            const diarA = pendingDiar.find((a) => a.segment_index === idx)
            const textA = pendingText.find((a) => a.segment_index === idx)
            const timingA = pendingTiming.find((a) => a.segment_index === idx)
            const delA = pendingDeletion.find((a) => a.segment_index === idx)
            const displaySpeaker = diarA ? diarA.corrected_speaker : speaker
            const displayText = textA ? textA.corrected_text : segment.text

            if (isEvent || isNote) {
              return (
                <div key={idx}>
                  <div
                    className={`group flex items-center gap-2 py-1 px-3 rounded ${
                      isEvent ? 'bg-yellow-50 dark:bg-yellow-900/20 border-l-2 border-yellow-400' : 'bg-green-50 dark:bg-green-900/20 border-l-2 border-green-400'
                    }`}
                    onMouseEnter={isEvent ? () => setHoverMarker({ start: segment.start, end: segment.end }) : undefined}
                    onMouseLeave={isEvent ? () => setHoverMarker(null) : undefined}
                  >
                    {isEvent && hasAudio && (
                      <button onClick={() => handleSegmentPlayPause(idx, segment)} className="flex-shrink-0 p-0.5 rounded hover:bg-yellow-200 opacity-0 group-hover:opacity-100">
                        {player.playingSegmentId === `${conversationId}-${idx}` ? <Pause className="h-3 w-3 text-yellow-600" /> : <Play className="h-3 w-3 text-yellow-600" />}
                      </button>
                    )}
                    <span className="text-xs font-medium text-gray-500 uppercase mr-2">{isEvent ? 'event' : 'note'}</span>
                    <span className="text-sm text-gray-700 dark:text-gray-300 italic">{displayText}</span>
                  </div>
                  <InsertDivider afterIndex={idx} />
                </div>
              )
            }

            return (
              <div key={idx}>
                <div
                  className={`group flex items-start gap-2 py-1 rounded hover:bg-gray-50 dark:hover:bg-gray-700/50 ${
                    delA ? 'bg-red-50 dark:bg-red-900/10 opacity-60' : textA ? 'bg-yellow-50 dark:bg-yellow-900/10' : ''
                  }`}
                  onMouseEnter={() => setHoverMarker({ start: segment.start, end: segment.end })}
                  onMouseLeave={() => setHoverMarker(null)}
                >
                  {hasAudio && (
                    <button onClick={() => handleSegmentPlayPause(idx, segment)} className="flex-shrink-0 mt-0.5 p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-600 opacity-0 group-hover:opacity-100" title={`Play ${formatDuration(segment.end - segment.start)}s`}>
                      {player.playingSegmentId === `${conversationId}-${idx}` ? <Pause className="h-3 w-3 text-blue-600" /> : <Play className="h-3 w-3 text-gray-500" />}
                    </button>
                  )}

                  <div className="flex-shrink-0 w-28 inline-flex items-start gap-1">
                    {preview ? (
                      <span className={`text-sm font-medium ${speakerColor}`}>{displaySpeaker}</span>
                    ) : (
                      <>
                        {diarA && (
                          <button onClick={() => handleDeleteAnnotation(diarA.id)} className="flex-shrink-0 mt-1 text-gray-400 hover:text-red-500" title={`Revert to "${diarA.original_speaker}"`}>
                            <X className="w-3 h-3" />
                          </button>
                        )}
                        <SpeakerNameDropdown
                          currentSpeaker={displaySpeaker}
                          enrolledSpeakers={allSpeakers}
                          onSpeakerChange={(ns) => handleSpeakerChange(idx, diarA ? diarA.original_speaker : speaker, ns, segment.start)}
                          segmentIndex={idx}
                          conversationId={conversationId}
                          annotated={!!diarA}
                          speakerColor={speakerColor}
                          recentSpeakers={recentSpeakers}
                        />
                      </>
                    )}
                  </div>

                  <div className="flex-1 min-w-0">
                    {isEditing && !preview ? (
                      <div className="space-y-1">
                        <textarea
                          value={editedText}
                          onChange={(e) => setEditedText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                              e.preventDefault()
                              handleSaveEdit(idx, segment.text)
                            } else if (e.key === 'Escape') {
                              e.preventDefault()
                              setEditingSegment(null)
                            }
                          }}
                          className="w-full px-2 py-1 text-sm border-2 border-blue-500 rounded focus:outline-none bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 resize-y"
                          autoFocus
                          disabled={savingSegment}
                          rows={2}
                        />
                        <div className="flex items-center gap-1">
                          <button onClick={() => handleSaveEdit(idx, segment.text)} disabled={savingSegment} className="px-2 py-0.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50">
                            {savingSegment ? 'Saving...' : 'Save'}
                          </button>
                          <button onClick={() => setEditingSegment(null)} className="px-2 py-0.5 text-xs bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-300 rounded hover:bg-gray-300">
                            Cancel
                          </button>
                          {segmentEditError && <span className="text-xs text-red-500">{segmentEditError}</span>}
                        </div>
                      </div>
                    ) : (
                      <p
                        className={`text-sm text-gray-700 dark:text-gray-300 px-1 rounded ${delA ? 'line-through text-red-500 dark:text-red-400' : ''} ${preview ? '' : 'cursor-pointer hover:bg-yellow-50 dark:hover:bg-yellow-900/10'}`}
                        onClick={preview ? undefined : () => handleStartEdit(idx, segment.text)}
                        title={preview ? undefined : 'Click to edit'}
                      >
                        {displayText}
                      </p>
                    )}
                  </div>

                  {/* Insert after / delete this segment */}
                  {!preview && (
                    <>
                      <button
                        onClick={() => (insertOpen === idx ? closeInsert() : openInsert(idx))}
                        className="flex-shrink-0 mt-0.5 p-0.5 rounded hover:bg-purple-100 dark:hover:bg-purple-900/40 opacity-0 group-hover:opacity-100"
                        title="Insert a new segment after this one"
                      >
                        <Plus className="h-3 w-3 text-gray-500" />
                      </button>
                      <button
                        onClick={() => handleToggleDeleteSegment(idx)}
                        className={`flex-shrink-0 mt-0.5 p-0.5 rounded hover:bg-red-100 dark:hover:bg-red-900/40 ${
                          delA ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
                        }`}
                        title={delA ? 'Undo delete (segment will be kept)' : 'Delete this segment'}
                      >
                        <Trash2 className={`h-3 w-3 ${delA ? 'text-red-500' : 'text-gray-500'}`} />
                      </button>
                    </>
                  )}

                  <span className="flex-shrink-0 text-xs text-gray-400 mt-0.5 tabular-nums">
                    {timingA ? formatDuration(timingA.new_start) : formatDuration(segment.start)}
                  </span>
                </div>
                <InsertDivider afterIndex={idx} />
              </div>
            )
          })}
        </div>
      ) : (
        <p className="text-sm text-gray-500 dark:text-gray-400 italic">{isLive ? 'Waiting for speech...' : 'No transcript segments available'}</p>
      )}
    </div>
  )
}
