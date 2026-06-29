/**
 * Leaf isolation for the conversation audio player.
 *
 * Wraps `WaveformDisplay` (and a companion time label) and subscribes to the
 * high-frequency playhead via `usePlayheadTime(cid)` INTERNALLY, so only this
 * subtree re-renders during playback — the heavy conversation list / detail page
 * around it does not. (Audio is off-thread now, so this is a CPU/perf nicety, not
 * a correctness requirement — but the list is heavy, so we do it.)
 */

import React from 'react'
import { WaveformDisplay, SegmentMarker } from './WaveformDisplay'
import { usePlayheadTime } from '../../hooks/useGaplessPlayer'
import type { SegmentMarker as PlayerSegmentMarker } from '../../lib/gaplessPlayer'

interface PlayheadWaveformProps {
  cid: string
  duration: number
  onSeek: (time: number) => void
  height?: number
  segments?: { start: number; end: number }[]
  // The active segment band (from the control snapshot). Rendered only when it
  // belongs to this conversation.
  segmentMarker?: PlayerSegmentMarker | null
  hoverMarker?: { start: number; end: number } | null
}

export const PlayheadWaveform: React.FC<PlayheadWaveformProps> = ({
  cid,
  duration,
  onSeek,
  height = 80,
  segments,
  segmentMarker,
  hoverMarker,
}) => {
  const currentTime = usePlayheadTime(cid)

  const markers: SegmentMarker[] = [
    ...(segmentMarker && segmentMarker.cid === cid
      ? [
          {
            start: segmentMarker.start,
            end: segmentMarker.end,
            kind: segmentMarker.playing ? ('playing' as const) : ('anchor' as const),
          },
        ]
      : []),
    ...(hoverMarker ? [{ start: hoverMarker.start, end: hoverMarker.end, kind: 'hover' as const }] : []),
  ]

  return (
    <WaveformDisplay
      conversationId={cid}
      duration={duration}
      currentTime={currentTime ?? 0}
      onSeek={onSeek}
      height={height}
      segments={segments}
      segmentMarkers={markers}
    />
  )
}

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

interface PlayheadTimeLabelProps {
  cid: string
  total?: number
  className?: string
}

/** High-frequency time readout, isolated from the surrounding page. */
export const PlayheadTimeLabel: React.FC<PlayheadTimeLabelProps> = ({ cid, total, className }) => {
  const currentTime = usePlayheadTime(cid)
  return (
    <span className={className}>
      {formatDuration(currentTime ?? 0)}
      {total ? ` / ${formatDuration(total)}` : ''}
    </span>
  )
}
