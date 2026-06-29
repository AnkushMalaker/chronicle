/**
 * React bindings for the gapless audio singleton.
 *
 * Two-tier subscription so the high-frequency playhead never re-renders heavy
 * lists:
 * - `useGaplessPlayer()` returns a low-frequency CONTROL snapshot (who's active,
 *   playing/paused, segment markers, buffering) + memoized action callbacks. It
 *   only changes at discrete transitions, NOT on every playhead tick.
 * - `usePlayheadTime(cid)` returns the high-frequency playhead as a primitive
 *   `number | undefined`. Only the active conversation's subscribers fire.
 */

import { useCallback, useMemo, useSyncExternalStore } from 'react'
import { gaplessPlayer, ControlSnapshot } from '../lib/gaplessPlayer'

export interface UseGaplessPlayer extends ControlSnapshot {
  play: typeof gaplessPlayer.play
  playSegment: typeof gaplessPlayer.playSegment
  playProgram: typeof gaplessPlayer.playProgram
  togglePlay: typeof gaplessPlayer.togglePlay
  setRate: typeof gaplessPlayer.setRate
  seek: typeof gaplessPlayer.seek
  pause: typeof gaplessPlayer.pause
  resume: typeof gaplessPlayer.resume
  stop: typeof gaplessPlayer.stop
  isActive: typeof gaplessPlayer.isActive
}

export function useGaplessPlayer(): UseGaplessPlayer {
  const snapshot = useSyncExternalStore(
    gaplessPlayer.subscribeControl,
    gaplessPlayer.getControlSnapshot,
    gaplessPlayer.getControlSnapshot
  )

  const actions = useMemo(
    () => ({
      play: gaplessPlayer.play,
      playSegment: gaplessPlayer.playSegment,
      playProgram: gaplessPlayer.playProgram,
      togglePlay: gaplessPlayer.togglePlay,
      setRate: gaplessPlayer.setRate,
      seek: gaplessPlayer.seek,
      pause: gaplessPlayer.pause,
      resume: gaplessPlayer.resume,
      stop: gaplessPlayer.stop,
      isActive: gaplessPlayer.isActive,
    }),
    []
  )

  return { ...snapshot, ...actions }
}

export function usePlayheadTime(cid: string): number | undefined {
  const subscribe = useCallback((cb: () => void) => gaplessPlayer.subscribeTime(cid, cb), [cid])
  const getSnapshot = useCallback(() => gaplessPlayer.getTime(cid), [cid])
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
