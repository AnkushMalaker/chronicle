import { useEffect, useState } from 'react'

/**
 * Wake-word UI feedback bus.
 *
 * The acoustic wake-word service emits three SSE events as a command turn flows
 * through it: `wake.armed` (wake word detected), `wake.end_of_turn` (capture
 * ended) and `wake.command` (recognized command + Hermes reply). `useSSE` pushes
 * those into this tiny module-level bus; `useWakeFeedback` turns them into
 * transient UI state (a phase + discrete pulse counters) that the Live Recording
 * screen and the global recording indicator both consume — no provider plumbing.
 */

export type WakePhase = 'idle' | 'listening' | 'ended' | 'followup'

export interface WakeEvent {
  type: 'armed' | 'end_of_turn' | 'command' | 'followup'
  score?: number
  reason?: string
  duration?: number
  command?: string
  reply?: string
  /** For 'followup': how long the follow-up window stays open (seconds). */
  window_secs?: number
}

type Listener = (evt: WakeEvent) => void

const listeners = new Set<Listener>()

/**
 * The client_id this browser is currently streaming as (from the `ready` WS frame),
 * or null when not recording. Wake events are keyed by client_id on the backend, but
 * delivered over the per-user SSE bus — so this browser must only react to wake
 * activity for its OWN device, not another of the user's devices (a HAVPE, a phone).
 */
let activeWakeClientId: string | null = null

/** Set/clear the client_id this browser streams as; gates wake-event delivery. */
export function setActiveWakeClientId(clientId: string | null): void {
  activeWakeClientId = clientId
}

/** The client_id this browser streams as, or null when not recording. */
export function getActiveWakeClientId(): string | null {
  return activeWakeClientId
}

/** Fan a wake event out to every mounted `useWakeFeedback` consumer. */
export function emitWakeEvent(evt: WakeEvent): void {
  listeners.forEach((l) => l(evt))
}

export interface WakeFeedback {
  /**
   * 'listening' from arm until end-of-turn; 'ended' briefly after; 'followup'
   * while a follow-up window is open (next utterance taken as a follow-up with
   * no wake word); else 'idle'.
   */
  phase: WakePhase
  /** Increments on every wake-word arm (drives one-shot pulse animations). */
  armedPulse: number
  /** Increments on every end-of-turn (drives one-shot pulse animations). */
  endedPulse: number
  /** Last recognized command text (auto-clears), or null. */
  lastCommand: string | null
  /** Last Hermes reply text (auto-clears), or null. */
  lastReply: string | null
}

// 'ended' lingers briefly so the green pulse is visible, then resets to idle.
const ENDED_RESET_MS = 4000
// Recognized command/reply stays on screen this long, then clears.
const COMMAND_CLEAR_MS = 15000
// Fallback follow-up window if the backend doesn't send window_secs.
const FOLLOWUP_DEFAULT_MS = 12000

export function useWakeFeedback(): WakeFeedback {
  const [phase, setPhase] = useState<WakePhase>('idle')
  const [armedPulse, setArmedPulse] = useState(0)
  const [endedPulse, setEndedPulse] = useState(0)
  const [lastCommand, setLastCommand] = useState<string | null>(null)
  const [lastReply, setLastReply] = useState<string | null>(null)

  useEffect(() => {
    let endedTimer: ReturnType<typeof setTimeout> | undefined
    let commandTimer: ReturnType<typeof setTimeout> | undefined
    let followupTimer: ReturnType<typeof setTimeout> | undefined

    const onEvent: Listener = (evt) => {
      if (evt.type === 'armed') {
        // A fresh acoustic wake supersedes any open follow-up window.
        if (endedTimer) clearTimeout(endedTimer)
        if (followupTimer) clearTimeout(followupTimer)
        setPhase('listening')
        setArmedPulse((n) => n + 1)
      } else if (evt.type === 'end_of_turn') {
        if (endedTimer) clearTimeout(endedTimer)
        setPhase('ended')
        setEndedPulse((n) => n + 1)
        endedTimer = setTimeout(() => setPhase('idle'), ENDED_RESET_MS)
      } else if (evt.type === 'command') {
        if (commandTimer) clearTimeout(commandTimer)
        setLastCommand(evt.command?.trim() || null)
        setLastReply(evt.reply?.trim() || null)
        commandTimer = setTimeout(() => {
          setLastCommand(null)
          setLastReply(null)
        }, COMMAND_CLEAR_MS)
      } else if (evt.type === 'followup') {
        // A follow-up window is open: the next utterance is taken as a follow-up
        // with no wake word. Refresh on each event; self-expire with the window.
        if (endedTimer) clearTimeout(endedTimer)
        if (followupTimer) clearTimeout(followupTimer)
        setPhase('followup')
        const ms = evt.window_secs ? evt.window_secs * 1000 : FOLLOWUP_DEFAULT_MS
        followupTimer = setTimeout(() => setPhase('idle'), ms)
      }
    }

    listeners.add(onEvent)
    return () => {
      listeners.delete(onEvent)
      if (endedTimer) clearTimeout(endedTimer)
      if (commandTimer) clearTimeout(commandTimer)
      if (followupTimer) clearTimeout(followupTimer)
    }
  }, [])

  return { phase, armedPulse, endedPulse, lastCommand, lastReply }
}
