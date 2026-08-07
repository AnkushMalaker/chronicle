/**
 * Gapless audio player — a single app-wide Web Audio scheduler.
 *
 * WHY THIS EXISTS
 * Conversation audio used to play as chained `<audio>` elements (one per 10 s
 * window / per segment), auto-advancing on the `ended` event. The audible gap at
 * every boundary was NOT in the data — each window decodes to exactly 10.0000 s
 * with ~0 ms of seam silence. The gap was the *main-thread handoff* between two
 * media elements (`ended` → React state → `currentTime=0` → `play()`).
 *
 * The fix: decode each window to an `AudioBuffer` once and schedule
 * `AudioBufferSourceNode`s back-to-back on a single `AudioContext` clock with a
 * look-ahead pump (`src.start(when, offset, duration)`). Audio then renders on the
 * browser audio thread from pre-scheduled buffers, so main-thread jank can no
 * longer create gaps. Precise seeking is preserved by starting the first source at
 * a sample-accurate buffer offset.
 *
 * Only one thing plays at a time across the whole app, so this is a module-level
 * singleton (stable across React re-renders / navigation). The React glue lives in
 * `src/hooks/useGaplessPlayer.ts`.
 *
 * TIME MODEL
 * - "audio time"   = seconds within the actual recording (what the waveform shows).
 * - "program time" = position along the concatenated program of ranges (silence
 *                    between speech regions removed). For a single full range
 *                    [0, total] the two are identical.
 * The clock: pt(now) = anchorProgramTime + (now − anchorCtxTime)·rate.
 * Public API is all in audio time; we convert internally.
 *
 * See `untracked/mycelia/frontend/src/modules/audio/player.tsx` for the prior-art
 * anchor-clock + generation-guard this validates, and the plan at
 * `~/.claude/plans/lets-properly-implement-the-dazzling-pinwheel.md`.
 */

import { BACKEND_URL } from '../services/api'
import { getStorageKey } from '../utils/storage'
import { decodeFormat, demoteToWav } from '../utils/audioFormat'

const WINDOW = 10 // seconds per cache/fetch window (internal granularity only)
const SCHEDULE_AHEAD = 0.3 // schedule chunks up to this many ctx-seconds ahead
const LEAD = 0.08 // first source starts this far in the future (decode safety margin)
const GUARD = 0.05 // if a chunk's start time is within this of "now", we missed it
const PREFETCH_WINDOWS = 2 // decode this many windows ahead of the cursor
const PUMP_INTERVAL_MS = 250 // periodic scheduling tick
const MAX_CACHED_WINDOWS = 30 // bounded LRU (~19 MB of decoded PCM)

export interface Range {
  start: number
  end: number
  /**
   * Recording this range's audio comes from. Omitted means the active id.
   *
   * A timeline episode is one event that can span several recordings — continuous
   * capture is cut into bounded compute spans, so an hour-long standup is commonly
   * three of them. Letting a range name its own recording is what makes such an
   * episode play as one continuous thing instead of three fragments. The window cache
   * is already keyed by recording, so ranges from different ones coexist safely.
   */
  cid?: string
}

export interface SegmentMarker {
  cid: string
  start: number
  end: number
  playing: boolean
}

export interface ControlSnapshot {
  activeConversationId: string | null
  isPlaying: boolean // truly producing sound (active, not paused)
  isPaused: boolean
  playingSegmentId: string | null
  segmentMarker: SegmentMarker | null
  buffering: boolean
}

// ---- module state ---------------------------------------------------------

let ctx: AudioContext | null = null
let gain: GainNode | null = null

let program: Range[] = []
let cumStart: number[] = [] // program-time at which each range begins
let programDuration = 0
let rate = 1

let anchorCtxTime = 0
let anchorProgramTime = 0
let anchored = false // false during the LEAD before the first window decodes
let cursor = 0 // program-time up to which we have scheduled

let playing = false
let paused = false
let buffering = false
let epoch = 0
let activeCid: string | null = null
let playingSegmentId: string | null = null
let segmentMarker: SegmentMarker | null = null

// Last audio-time per conversation (for resume / inactive playhead).
const positions = new Map<string, number>()

// Decoded-window cache + in-flight fetches.
const windowCache = new Map<string, AudioBuffer>()
const windowFetches = new Map<string, Promise<AudioBuffer>>()
const lru: string[] = []

// Live scheduled sources + per-window refcount so the LRU never evicts a buffer
// that is currently scheduled.
const scheduled = new Set<AudioBufferSourceNode>()
const schedKeyCount = new Map<string, number>()

let pumpTimer: ReturnType<typeof setInterval> | null = null
let rafId: number | null = null

// ---- listeners (useSyncExternalStore) -------------------------------------

const controlListeners = new Set<() => void>()
const timeListeners = new Map<string, Set<() => void>>()

let controlSnapshot: ControlSnapshot = {
  activeConversationId: null,
  isPlaying: false,
  isPaused: false,
  playingSegmentId: null,
  segmentMarker: null,
  buffering: false,
}

function rebuildControl() {
  controlSnapshot = {
    activeConversationId: activeCid,
    isPlaying: playing && !paused,
    isPaused: paused,
    playingSegmentId,
    segmentMarker,
    buffering,
  }
  controlListeners.forEach((cb) => cb())
}

function notifyTime(cid: string) {
  timeListeners.get(cid)?.forEach((cb) => cb())
}

// ---- helpers --------------------------------------------------------------

const tokenVal = () => localStorage.getItem(getStorageKey('token')) || ''
const wkey = (cid: string, w: number) => `${cid}_${w}`
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

function setProgram(ranges: Range[]) {
  program = ranges.filter((r) => r.end > r.start)
  cumStart = []
  let acc = 0
  for (const r of program) {
    cumStart.push(acc)
    acc += r.end - r.start
  }
  programDuration = acc
}

// Which recording range `i` reads from. Ranges without their own id belong to the
// active recording, which is every program except a cross-recording episode.
function rangeCid(i: number): string {
  return program[i]?.cid ?? (activeCid as string)
}

// program-time → { range index, audio time }
function programToRange(pt: number): { i: number; audioTime: number } {
  if (program.length === 0) return { i: 0, audioTime: 0 }
  if (pt >= programDuration) {
    const last = program.length - 1
    return { i: last, audioTime: program[last].end }
  }
  for (let i = 0; i < program.length; i++) {
    const len = program[i].end - program[i].start
    if (pt < cumStart[i] + len) return { i, audioTime: program[i].start + (pt - cumStart[i]) }
  }
  const last = program.length - 1
  return { i: last, audioTime: program[last].end }
}

// audio-time → program-time (snaps forward to the next range if in a gap)
function audioToProgram(at: number): number {
  for (let i = 0; i < program.length; i++) {
    const { start, end } = program[i]
    if (at < start) return cumStart[i] // inside a gap before range i → snap forward
    if (at < end) return cumStart[i] + (at - start)
  }
  return programDuration
}

function ctxTimeFor(pt: number): number {
  return anchorCtxTime + (pt - anchorProgramTime) / rate
}

function programTimeNow(): number {
  if (!ctx) return anchorProgramTime
  // AudioContext.currentTime is frozen while suspended, so this is correct paused too.
  return anchorProgramTime + (ctx.currentTime - anchorCtxTime) * rate
}

function ensureContext() {
  if (!ctx) {
    const Ctor = window.AudioContext || (window as any).webkitAudioContext
    ctx = new Ctor()
    gain = ctx.createGain()
    gain.gain.value = 1
    gain.connect(ctx.destination)
  }
  if (ctx.state === 'suspended') ctx.resume().catch(() => {})
}

// ---- window cache ---------------------------------------------------------

function touchLRU(key: string) {
  const i = lru.indexOf(key)
  if (i !== -1) lru.splice(i, 1)
  lru.push(key)
}

function evict() {
  while (windowCache.size > MAX_CACHED_WINDOWS) {
    // Evict the oldest window that is not currently scheduled.
    const idx = lru.findIndex((k) => (schedKeyCount.get(k) || 0) === 0)
    if (idx === -1) break
    const [key] = lru.splice(idx, 1)
    windowCache.delete(key)
  }
}

async function ensureBuffer(cid: string, w: number): Promise<AudioBuffer> {
  const key = wkey(cid, w)
  const cached = windowCache.get(key)
  if (cached) {
    touchLRU(key)
    return cached
  }
  const inflight = windowFetches.get(key)
  if (inflight) return inflight

  const p = (async (): Promise<AudioBuffer> => {
    const fmt = decodeFormat()
    const url =
      `${BACKEND_URL}/api/conversations/${cid}/audio-segments` +
      `?start=${w * WINDOW}&duration=${WINDOW}&format=${fmt}`
    const resp = await fetch(url, { headers: { Authorization: `Bearer ${tokenVal()}` } })
    if (!resp.ok) throw new Error(`Audio fetch failed: ${resp.status}`)
    const ab = await resp.arrayBuffer()
    let buf: AudioBuffer
    try {
      buf = await ctx!.decodeAudioData(ab)
    } catch (e) {
      // Safari: advertises opus but throws here. Permanently fall back to wav.
      if (fmt === 'opus') {
        demoteToWav()
        windowCache.clear()
        lru.length = 0
        windowFetches.delete(key)
        return ensureBuffer(cid, w)
      }
      throw e
    }
    windowCache.set(key, buf)
    touchLRU(key)
    windowFetches.delete(key)
    evict()
    return buf
  })()

  windowFetches.set(key, p)
  p.catch(() => windowFetches.delete(key))
  return p
}

// ---- scheduling pump ------------------------------------------------------

function stopSources() {
  scheduled.forEach((src) => {
    src.onended = null
    try {
      src.stop()
    } catch {
      /* already stopped */
    }
    try {
      src.disconnect()
    } catch {
      /* noop */
    }
  })
  scheduled.clear()
  schedKeyCount.clear()
}

function startPumpTimer() {
  if (pumpTimer == null) pumpTimer = setInterval(pump, PUMP_INTERVAL_MS)
}
function stopPumpTimer() {
  if (pumpTimer != null) {
    clearInterval(pumpTimer)
    pumpTimer = null
  }
}

function pump() {
  if (!playing || paused || !ctx || !gain) return
  const myEpoch = epoch
  const now = ctx.currentTime

  while (cursor < programDuration - 1e-6) {
    // ctx time when the chunk at `cursor` should begin
    let when = ctxTimeFor(cursor)
    if (when >= now + SCHEDULE_AHEAD / rate) break // far enough ahead; stop for now

    const { i, audioTime: a0 } = programToRange(cursor)
    const w = Math.floor(a0 / WINDOW)
    const windowAudioStart = w * WINDOW
    const a1 = Math.min((w + 1) * WINDOW, program[i].end) // clip window to range end
    const segDur = a1 - a0
    if (segDur <= 0) {
      cursor += 1e-6
      continue
    }

    const cid = rangeCid(i)
    const buf = windowCache.get(wkey(cid, w))
    if (!buf) {
      // Not decoded yet — fetch and re-kick the pump when ready, then wait.
      ensureBuffer(cid, w)
        .then(() => {
          if (epoch === myEpoch) pump()
        })
        .catch(() => {})
      break
    }

    // Boundary deadline missed (cold-cache near-boundary seek): re-anchor so this
    // chunk starts cleanly LEAD seconds out. One tiny gap, then gapless onward.
    if (when <= now + GUARD) {
      anchorProgramTime = cursor
      anchorCtxTime = now + LEAD
      when = ctxTimeFor(cursor)
    }

    const offset = a0 - windowAudioStart
    const isLast = cursor + segDur >= programDuration - 1e-6
    const key = wkey(cid, w)

    const src = ctx.createBufferSource()
    src.buffer = buf
    src.playbackRate.value = rate
    src.connect(gain)
    src.onended = () => {
      if (epoch !== myEpoch) return
      scheduled.delete(src)
      const c = (schedKeyCount.get(key) || 1) - 1
      if (c <= 0) schedKeyCount.delete(key)
      else schedKeyCount.set(key, c)
      if (isLast) finalize()
    }
    src.start(when, offset, segDur)
    scheduled.add(src)
    schedKeyCount.set(key, (schedKeyCount.get(key) || 0) + 1)

    // Program time is continuous across range boundaries, so simply advancing the
    // cursor schedules the next range adjacently → seamless silence-skip.
    cursor += segDur
  }

  prefetch()
}

function prefetch() {
  if (!activeCid) return
  let pt = cursor
  for (let n = 0; n < PREFETCH_WINDOWS && pt < programDuration - 1e-6; n++) {
    const { i, audioTime: a0 } = programToRange(pt)
    const w = Math.floor(a0 / WINDOW)
    ensureBuffer(rangeCid(i), w).catch(() => {})
    const a1 = Math.min((w + 1) * WINDOW, program[i].end)
    pt += Math.max(1e-6, a1 - a0)
  }
}

// ---- playhead rAF ---------------------------------------------------------

function rafTick() {
  if (!playing || paused) {
    rafId = null
    return
  }
  if (activeCid) {
    positions.set(activeCid, getTime(activeCid) ?? 0)
    notifyTime(activeCid)
  }
  rafId = requestAnimationFrame(rafTick)
}
function startRAF() {
  if (rafId == null) rafId = requestAnimationFrame(rafTick)
}
function stopRAF() {
  if (rafId != null) {
    cancelAnimationFrame(rafId)
    rafId = null
  }
}

// ---- lifecycle ------------------------------------------------------------

function beginPlayback(pt0: number) {
  epoch += 1
  const myEpoch = epoch
  stopSources()
  stopPumpTimer()

  if (programDuration <= 0) {
    // Nothing to play (e.g. empty speech program).
    playing = false
    paused = false
    buffering = false
    rebuildControl()
    return
  }

  cursor = clamp(pt0, 0, programDuration)
  anchored = false
  playing = true
  paused = false
  buffering = true
  ensureContext()
  rebuildControl()
  startRAF()

  // Anchor only AFTER the first window decodes, so first-window decode latency can
  // never underrun the schedule.
  const { i: i0, audioTime: a0 } = programToRange(cursor)
  const w = Math.floor(a0 / WINDOW)
  ensureBuffer(rangeCid(i0), w)
    .then(() => {
      if (epoch !== myEpoch || !ctx) return
      anchorCtxTime = ctx.currentTime + LEAD
      anchorProgramTime = cursor
      anchored = true
      buffering = false
      rebuildControl()
      startPumpTimer()
      pump()
    })
    .catch(() => {
      if (epoch !== myEpoch) return
      playing = false
      buffering = false
      rebuildControl()
    })
}

function finalize() {
  const cid = activeCid
  epoch += 1
  stopSources()
  stopPumpTimer()
  stopRAF()
  playing = false
  paused = false
  buffering = false
  anchored = false
  if (cid) positions.set(cid, 0) // end-of-playback decision: cursor returns to start
  activeCid = null
  playingSegmentId = null
  if (segmentMarker) segmentMarker = { ...segmentMarker, playing: false }
  rebuildControl()
  if (cid) notifyTime(cid)
}

// ---- public API -----------------------------------------------------------

function play(
  cid: string,
  audioTime: number,
  opts: { totalDuration: number; rate?: number }
) {
  // Freeze the outgoing conversation's position before switching.
  if (activeCid && activeCid !== cid) positions.set(activeCid, getTime(activeCid) ?? 0)
  activeCid = cid
  rate = opts.rate ?? 1
  playingSegmentId = null
  segmentMarker = segmentMarker && segmentMarker.cid === cid ? { ...segmentMarker, playing: false } : null
  setProgram([{ start: 0, end: opts.totalDuration }])
  beginPlayback(audioToProgram(audioTime))
}

function playSegment(cid: string, segmentId: string, start: number, end: number) {
  if (activeCid && activeCid !== cid) positions.set(activeCid, getTime(activeCid) ?? 0)
  activeCid = cid
  rate = 1
  playingSegmentId = segmentId
  segmentMarker = { cid, start, end, playing: true }
  setProgram([{ start, end }])
  beginPlayback(0)
}

function playProgram(
  cid: string,
  ranges: Range[],
  opts?: { rate?: number; fromAudioTime?: number }
) {
  if (activeCid && activeCid !== cid) positions.set(activeCid, getTime(activeCid) ?? 0)
  activeCid = cid
  rate = opts?.rate ?? 1
  playingSegmentId = null
  segmentMarker = null
  setProgram(ranges)
  beginPlayback(audioToProgram(opts?.fromAudioTime ?? 0))
}

function seek(audioTime: number) {
  if (!activeCid) return
  beginPlayback(audioToProgram(audioTime))
}

/**
 * Seek by position within the program rather than by audio time.
 *
 * `seek` maps an audio time back onto the program, which only works while every range
 * shares one recording's timeline. A cross-recording episode program has several
 * timelines that each restart near zero, so an audio time does not identify a position
 * in it — `audioToProgram(1900)` on a two-recording program runs off the end and seeks
 * to the finish. Callers holding such a program must use this instead.
 */
function seekProgram(programTime: number) {
  if (!activeCid) return
  beginPlayback(clamp(programTime, 0, programDuration))
}

function setRate(r: number) {
  rate = r
  // Clean re-anchor + reschedule at the new rate (rare user action).
  if (activeCid && ctx && playing && !paused) {
    const at = programToRange(clamp(programTimeNow(), 0, programDuration)).audioTime
    beginPlayback(audioToProgram(at))
  }
}

function pause() {
  if (!playing || paused || !ctx) return
  paused = true
  ctx.suspend().catch(() => {})
  stopRAF()
  rebuildControl()
}

function resume() {
  if (!paused || !ctx) return
  paused = false
  ctx.resume().catch(() => {})
  startRAF()
  startPumpTimer()
  pump()
  rebuildControl()
}

function togglePlay(cid: string, totalDuration: number) {
  if (activeCid === cid && (playing || paused)) {
    if (paused) resume()
    else pause()
  } else {
    play(cid, positions.get(cid) ?? 0, { totalDuration })
  }
}

function stop() {
  const cid = activeCid
  if (cid && ctx) positions.set(cid, getTime(cid) ?? 0)
  epoch += 1
  stopSources()
  stopPumpTimer()
  stopRAF()
  playing = false
  paused = false
  buffering = false
  anchored = false
  activeCid = null
  playingSegmentId = null
  if (segmentMarker) segmentMarker = { ...segmentMarker, playing: false }
  rebuildControl()
  if (cid) notifyTime(cid)
}

function getTime(cid: string): number | undefined {
  if (cid === activeCid && ctx) {
    // Before the first window decodes (during LEAD) the clock isn't anchored yet,
    // so report the intended start position rather than the stale clock math.
    const pt = anchored ? programTimeNow() : cursor
    return programToRange(clamp(pt, 0, programDuration)).audioTime
  }
  return positions.get(cid)
}

function isActive(cid: string): boolean {
  return activeCid === cid
}

// ---- store subscriptions --------------------------------------------------

function subscribeControl(cb: () => void): () => void {
  controlListeners.add(cb)
  return () => controlListeners.delete(cb)
}

function getControlSnapshot(): ControlSnapshot {
  return controlSnapshot
}

function subscribeTime(cid: string, cb: () => void): () => void {
  let set = timeListeners.get(cid)
  if (!set) {
    set = new Set()
    timeListeners.set(cid, set)
  }
  set.add(cb)
  return () => {
    const s = timeListeners.get(cid)
    if (!s) return
    s.delete(cb)
    if (s.size === 0) timeListeners.delete(cid)
  }
}

export const gaplessPlayer = {
  play,
  playSegment,
  playProgram,
  seek,
  seekProgram,
  setRate,
  pause,
  resume,
  togglePlay,
  stop,
  getTime,
  isActive,
  subscribeControl,
  getControlSnapshot,
  subscribeTime,
}

// Dev-only handle for the headless-Chromium gap harness.
if (import.meta.env?.DEV) {
  ;(window as any).__gp = gaplessPlayer
}
