import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Play, X, Check, Plus, RotateCcw } from 'lucide-react'
import { useWaveformData } from './useWaveformData'
import { usePlayheadTime } from '../../hooks/useGaplessPlayer'

export interface Region {
  start: number
  end: number
}

interface WaveformRegionEditorProps {
  conversationId: string
  duration: number
  /**
   * The span to open framed on (move/resize), or `null` to open in free-select/draw mode
   * (used for "insert a new segment near a time").
   */
  initialRegion: Region | null
  /** When `initialRegion` is null, center the auto-zoom around this time. */
  focusTime?: number
  /** Commit: update THIS segment's timing. Omit in pure-insert mode (no Save button). */
  onSaveTiming?: (region: Region) => void
  /** Commit: insert a NEW segment with this region. Omit in picker mode. */
  onAddSegment?: (region: Region) => void
  /** Label for the add/insert button (default "+ New"). */
  addLabel?: string
  onCancel: () => void
  /** Audition the current region. */
  onPlay?: (region: Region) => void
  /**
   * Picker mode: no own commit buttons — just a region selector. Fires `onChange`
   * whenever the region changes; an external form (the insert menu) does the commit.
   */
  pickerMode?: boolean
  onChange?: (region: Region | null) => void
  height?: number
}

const EDGE_PX = 7 // hit zone for grabbing a region edge
const MIN_SPAN = 0.4 // smallest zoom window (seconds)
const MIN_REGION = 0.05 // smallest region (seconds)

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))
const fmt = (t: number) => `${t.toFixed(2)}s`

type DragMode = 'move' | 'resize-l' | 'resize-r' | 'draw' | 'pan' | null

/**
 * Interactive waveform editor for a single segment's time span.
 *
 * - Auto-zooms (smoothly) to frame the segment on open.
 * - Scroll wheel = zoom around the cursor; drag outside the band = pan.
 * - Drag the band = move; drag its edges = resize.
 * - The ✕ "clear" button drops the band and switches to free-select: drag anywhere to
 *   draw a fresh region (used to carve out a brand-new / overlapping segment).
 * - Save commits the region to this segment; "+ New" inserts it as a new segment.
 *
 * Resolution is whatever the shared waveform fetch provides (currently coarse ~3 peaks/s);
 * the data hook is range-upgradeable later without touching this component.
 */
export const WaveformRegionEditor: React.FC<WaveformRegionEditorProps> = ({
  conversationId,
  duration,
  initialRegion,
  focusTime,
  onSaveTiming,
  onAddSegment,
  addLabel = '+ New',
  onCancel,
  onPlay,
  pickerMode = false,
  onChange,
  height = 96,
}) => {
  const { data, loading, error } = useWaveformData(conversationId)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  // Live playback position (so the playhead moves while playing, even when zoomed in).
  const currentTime = usePlayheadTime(conversationId)
  const playheadRef = useRef<number | null>(currentTime ?? null)

  // view = visible [t0,t1] window; region = current selection (null = free-select mode).
  // Mirror in refs so pointer/wheel handlers always read fresh values (no stale closures).
  const paddedView = useCallback(
    (r: Region) => {
      const d = Math.max(0, r.end - r.start)
      const pad = Math.max(0.75, d * 0.6)
      const t0 = clamp(r.start - pad, 0, duration)
      const t1 = clamp(r.end + pad, 0, duration)
      return { t0, t1: Math.max(t1, t0 + MIN_SPAN) }
    },
    [duration]
  )

  const [view, setViewState] = useState<{ t0: number; t1: number }>(() => ({ t0: 0, t1: duration || 1 }))
  const [region, setRegionState] = useState<Region | null>(initialRegion)
  const viewRef = useRef(view)
  const regionRef = useRef<Region | null>(region)
  const setView = (v: { t0: number; t1: number }) => {
    viewRef.current = v
    setViewState(v)
  }
  const setRegion = (r: Region | null) => {
    regionRef.current = r
    setRegionState(r)
    onChange?.(r)
  }

  const dragRef = useRef<{ mode: DragMode; anchorT: number; offset: number; panT0: number; panX: number }>(
    { mode: null, anchorT: 0, offset: 0, panT0: 0, panX: 0 }
  )

  // Smooth auto-zoom from the full clip down to the segment (or to the focus time, in
  // insert/draw mode) on mount.
  useEffect(() => {
    if (!duration) return
    let target: { t0: number; t1: number }
    if (initialRegion) {
      target = paddedView(initialRegion)
    } else if (focusTime != null) {
      const half = 3
      const t0 = clamp(focusTime - half, 0, duration)
      const t1 = clamp(focusTime + half, 0, duration)
      target = { t0, t1: Math.max(t1, t0 + MIN_SPAN) }
    } else {
      target = { t0: 0, t1: duration }
    }
    const from = { t0: 0, t1: duration }
    const ms = 380
    let raf = 0
    let t0Start: number | null = null
    const ease = (p: number) => 1 - Math.pow(1 - p, 3) // ease-out cubic
    const step = (ts: number) => {
      if (t0Start === null) t0Start = ts
      const p = clamp((ts - t0Start) / ms, 0, 1)
      const e = ease(p)
      setView({
        t0: from.t0 + (target.t0 - from.t0) * e,
        t1: from.t1 + (target.t1 - from.t1) * e,
      })
      if (p < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  // ---- coordinate helpers (read current view from ref) ----
  const widthOf = () => canvasRef.current?.clientWidth || 1
  const timeToX = (t: number, w = widthOf()) => {
    const { t0, t1 } = viewRef.current
    return ((t - t0) / (t1 - t0)) * w
  }
  const xToTime = (x: number, w = widthOf()) => {
    const { t0, t1 } = viewRef.current
    return t0 + (x / w) * (t1 - t0)
  }

  // ---- draw ----
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const w = canvas.clientWidth
    const H = height
    const dpr = window.devicePixelRatio || 1
    canvas.width = w * dpr
    canvas.height = H * dpr
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, H)

    const { t0, t1 } = viewRef.current
    const span = t1 - t0 || 1
    const centerY = H / 2

    // waveform bars within the visible window
    if (data && data.samples.length) {
      const sr = data.sample_rate || 3
      const i0 = Math.max(0, Math.floor(t0 * sr))
      const i1 = Math.min(data.samples.length - 1, Math.ceil(t1 * sr))
      ctx.fillStyle = '#3b82f6'
      for (let i = i0; i <= i1; i++) {
        const xA = ((i / sr - t0) / span) * w
        const xB = (((i + 1) / sr - t0) / span) * w
        const amp = data.samples[i]
        const barH = Math.max(1, amp * (centerY - 2))
        ctx.fillRect(xA, centerY - barH, Math.max(1, xB - xA - 0.5), barH * 2)
      }
    }

    // region band
    const r = regionRef.current
    if (r) {
      const xs = ((r.start - t0) / span) * w
      const xe = ((r.end - t0) / span) * w
      ctx.fillStyle = 'rgba(16, 185, 129, 0.18)' // emerald
      ctx.fillRect(xs, 0, xe - xs, H)
      ctx.strokeStyle = 'rgba(5, 150, 105, 0.95)'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.moveTo(xs, 0); ctx.lineTo(xs, H)
      ctx.moveTo(xe, 0); ctx.lineTo(xe, H)
      ctx.stroke()
      // edge grips
      ctx.fillStyle = 'rgba(5, 150, 105, 0.95)'
      ctx.fillRect(xs - 2, centerY - 10, 4, 20)
      ctx.fillRect(xe - 2, centerY - 10, 4, 20)
    }

    // playback position (only when within the visible window)
    const ct = playheadRef.current
    if (ct != null && ct >= t0 && ct <= t1) {
      const xp = ((ct - t0) / span) * w
      ctx.strokeStyle = '#ef4444' // red-500
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.moveTo(xp, 0)
      ctx.lineTo(xp, H)
      ctx.stroke()
    }
  }, [data, height])

  // redraw on state changes + resize
  useEffect(() => {
    draw()
  }, [draw, view, region])

  // redraw the playhead as playback advances
  useEffect(() => {
    playheadRef.current = currentTime ?? null
    draw()
  }, [currentTime, draw])

  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    const ro = new ResizeObserver(() => draw())
    ro.observe(el)
    return () => ro.disconnect()
  }, [draw])

  // wheel zoom (non-passive so we can preventDefault page scroll)
  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const x = e.clientX - rect.left
      const { t0, t1 } = viewRef.current
      const span = t1 - t0
      const tc = t0 + (x / rect.width) * span
      const factor = Math.exp(e.deltaY * 0.0015) // up → <1 → zoom in
      const newSpan = clamp(span * factor, MIN_SPAN, duration || span)
      let nt0 = tc - (tc - t0) * (newSpan / span)
      nt0 = clamp(nt0, 0, Math.max(0, (duration || newSpan) - newSpan))
      setView({ t0: nt0, t1: nt0 + newSpan })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [duration])

  // ---- pointer interaction ----
  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const el = canvasRef.current!
    el.setPointerCapture(e.pointerId)
    const rect = el.getBoundingClientRect()
    const x = e.clientX - rect.left
    const t = xToTime(x, rect.width)
    const r = regionRef.current
    const d = dragRef.current

    if (r) {
      const xs = timeToX(r.start, rect.width)
      const xe = timeToX(r.end, rect.width)
      if (Math.abs(x - xs) <= EDGE_PX) d.mode = 'resize-l'
      else if (Math.abs(x - xe) <= EDGE_PX) d.mode = 'resize-r'
      else if (x > xs && x < xe) {
        d.mode = 'move'
        d.offset = t - r.start
      } else {
        d.mode = 'pan'
        d.panT0 = viewRef.current.t0
        d.panX = x
      }
    } else {
      d.mode = 'draw'
      d.anchorT = t
      setRegion({ start: t, end: t })
    }
  }

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current
    if (!d.mode) return
    const el = canvasRef.current!
    const rect = el.getBoundingClientRect()
    const x = clamp(e.clientX - rect.left, 0, rect.width)
    const t = clamp(xToTime(x, rect.width), 0, duration)
    const r = regionRef.current

    if (d.mode === 'pan') {
      const span = viewRef.current.t1 - viewRef.current.t0
      const dt = ((d.panX - x) / rect.width) * span
      const nt0 = clamp(d.panT0 + dt, 0, Math.max(0, duration - span))
      setView({ t0: nt0, t1: nt0 + span })
      return
    }
    if (!r && d.mode !== 'draw') return

    if (d.mode === 'resize-l' && r) {
      setRegion({ start: clamp(t, 0, r.end - MIN_REGION), end: r.end })
    } else if (d.mode === 'resize-r' && r) {
      setRegion({ start: r.start, end: clamp(t, r.start + MIN_REGION, duration) })
    } else if (d.mode === 'move' && r) {
      const wdt = r.end - r.start
      const ns = clamp(t - d.offset, 0, duration - wdt)
      setRegion({ start: ns, end: ns + wdt })
    } else if (d.mode === 'draw') {
      setRegion({ start: Math.min(d.anchorT, t), end: Math.max(d.anchorT, t) })
    }
  }

  const endDrag = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current
    if (d.mode === 'draw') {
      const r = regionRef.current
      if (r && r.end - r.start < MIN_REGION) setRegion(null) // treat as a stray click
    }
    d.mode = null
    try {
      canvasRef.current?.releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }

  const cursor = (() => {
    return region ? 'grab' : 'crosshair'
  })()

  const dur = region ? region.end - region.start : 0

  return (
    <div className="mt-2 p-3 rounded-lg border border-emerald-200 dark:border-emerald-800 bg-emerald-50/40 dark:bg-emerald-900/10">
      {error ? (
        <div className="text-xs text-gray-500 py-6 text-center">No waveform available</div>
      ) : loading ? (
        <div className="text-xs text-gray-400 py-6 text-center animate-pulse">Loading waveform…</div>
      ) : (
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          className="w-full rounded bg-white/60 dark:bg-gray-900/40 touch-none select-none"
          style={{ height, cursor }}
          title="Scroll to zoom · drag the band to move · drag edges to resize · drag outside to pan"
        />
      )}

      <div className="flex items-center justify-between gap-2 mt-2 flex-wrap">
        <div className="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300 font-mono">
          {region ? (
            <>
              <span>{fmt(region.start)}</span>
              <span className="text-gray-400">→</span>
              <span>{fmt(region.end)}</span>
              <span className="text-gray-400">({dur.toFixed(2)}s)</span>
            </>
          ) : (
            <span className="text-emerald-600 dark:text-emerald-400">Drag on the waveform to select a region</span>
          )}
        </div>

        <div className="flex items-center gap-1.5">
          {region && onPlay && (
            <button
              onClick={() => onPlay(region)}
              className="p-1.5 rounded bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200"
              title="Play selection"
            >
              <Play className="h-3.5 w-3.5" />
            </button>
          )}
          {region ? (
            <button
              onClick={() => setRegion(null)}
              className="p-1.5 rounded bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200"
              title="Clear region and free-select"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          ) : initialRegion ? (
            <button
              onClick={() => setRegion(initialRegion)}
              className="p-1.5 rounded bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200"
              title="Restore the original region"
            >
              <RotateCcw className="h-3.5 w-3.5" />
            </button>
          ) : null}
          {!pickerMode && onAddSegment && (
            <button
              onClick={() => region && onAddSegment(region)}
              disabled={!region}
              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-900/60 disabled:opacity-40"
              title="Insert a NEW segment with this region (e.g. an overlapping speaker)"
            >
              <Plus className="h-3.5 w-3.5" /> {addLabel}
            </button>
          )}
          {!pickerMode && onSaveTiming && (
            <button
              onClick={() => region && onSaveTiming(region)}
              disabled={!region}
              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-40"
              title="Update this segment's timing"
            >
              <Check className="h-3.5 w-3.5" /> Save
            </button>
          )}
          {!pickerMode && (
            <button
              onClick={onCancel}
              className="px-2 py-1 text-xs rounded bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-500"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
