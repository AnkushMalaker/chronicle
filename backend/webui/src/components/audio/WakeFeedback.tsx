import { useWakeFeedback } from '../../hooks/useWakeFeedback'
import { Card } from '../ui'

/**
 * Live wake-word feedback for the Live Recording screen.
 *
 * Shows a small pulse/badge when the acoustic wake word arms ("listening") and
 * when end-of-turn fires ("end of turn"), plus the recognized command and the
 * Hermes reply once they come back from the backend.
 */
export default function WakeFeedback() {
  const { phase, lastCommand, lastReply, lastBlocked } = useWakeFeedback()

  // Nothing happening and no recent command/block — stay out of the way.
  if (phase === 'idle' && !lastCommand && !lastBlocked) return null

  return (
    <Card raised padded={false} className="mt-4 p-3 space-y-2">
      {/* Phase badge with a pulsing dot */}
      {phase !== 'idle' && (
        <div className="flex items-center gap-2">
          {phase === 'listening' ? (
            <>
              <span className="relative flex h-2.5 w-2.5">
                <span className="absolute inline-flex h-2.5 w-2.5 rounded-full bg-amber-400 opacity-75 animate-ping" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-amber-500" />
              </span>
              <span className="text-sm font-medium text-amber-600 dark:text-amber-400">
                Wake word detected — listening…
              </span>
            </>
          ) : phase === 'followup' ? (
            <>
              <span className="relative flex h-2.5 w-2.5">
                <span className="absolute inline-flex h-2.5 w-2.5 rounded-full bg-sky-400 opacity-75 animate-ping" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-sky-500" />
              </span>
              <span className="text-sm font-medium text-sky-600 dark:text-sky-400">
                Listening for follow-up… (no wake word needed)
              </span>
            </>
          ) : (
            <>
              <span className="relative flex h-2.5 w-2.5">
                <span className="absolute inline-flex h-2.5 w-2.5 rounded-full bg-green-400 opacity-75 animate-ping" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-green-500" />
              </span>
              <span className="text-sm font-medium text-green-600 dark:text-green-400">
                End of turn
              </span>
            </>
          )}
        </div>
      )}

      {/* Recognized command + Hermes reply */}
      {lastCommand && (
        <div className="text-sm">
          <span className="text-gray-500 dark:text-gray-400">Heard: </span>
          <span className="font-medium text-gray-900 dark:text-gray-100">“{lastCommand}”</span>
        </div>
      )}
      {lastReply && (
        <div className="text-sm">
          <span className="text-gray-500 dark:text-gray-400">Hermes: </span>
          <span className="text-gray-700 dark:text-gray-300">{lastReply}</span>
        </div>
      )}

      {/* Speaker gate rejection */}
      {lastBlocked && (
        <div className="text-sm text-amber-600 dark:text-amber-400">{lastBlocked}</div>
      )}
    </Card>
  )
}
