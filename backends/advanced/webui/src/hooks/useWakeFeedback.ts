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

export type WakePhase = 'idle' | 'listening' | 'ended'

export interface WakeEvent {
  type: 'armed' | 'end_of_turn' | 'command'
  score?: number
  reason?: string
  duration?: number
  command?: string
  reply?: string
}

type Listener = (evt: WakeEvent) => void

const listeners = new Set<Listener>()

/** Fan a wake event out to every mounted `useWakeFeedback` consumer. */
export function emitWakeEvent(evt: WakeEvent): void {
  listeners.forEach((l) => l(evt))
}

export interface WakeFeedback {
  /** 'listening' from arm until end-of-turn; 'ended' briefly after; else 'idle'. */
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

export function useWakeFeedback(): WakeFeedback {
  const [phase, setPhase] = useState<WakePhase>('idle')
  const [armedPulse, setArmedPulse] = useState(0)
  const [endedPulse, setEndedPulse] = useState(0)
  const [lastCommand, setLastCommand] = useState<string | null>(null)
  const [lastReply, setLastReply] = useState<string | null>(null)

  useEffect(() => {
    let endedTimer: ReturnType<typeof setTimeout> | undefined
    let commandTimer: ReturnType<typeof setTimeout> | undefined

    const onEvent: Listener = (evt) => {
      if (evt.type === 'armed') {
        if (endedTimer) clearTimeout(endedTimer)
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
      }
    }

    listeners.add(onEvent)
    return () => {
      listeners.delete(onEvent)
      if (endedTimer) clearTimeout(endedTimer)
      if (commandTimer) clearTimeout(commandTimer)
    }
  }, [])

  return { phase, armedPulse, endedPulse, lastCommand, lastReply }
}
