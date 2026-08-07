import { useState } from 'react'
import { Scissors, Trash2 } from 'lucide-react'
import { TimelineEpisode } from '../../services/api'
import { Button } from '../ui'

/** `HH:MM` in the viewer's timezone, which is the timezone the day was analyzed in. */
function toTimeValue(iso: string) {
  const value = new Date(iso)
  return `${String(value.getHours()).padStart(2, '0')}:${String(value.getMinutes()).padStart(2, '0')}`
}

/**
 * Rebuild an ISO timestamp by replacing the clock time, keeping the original date.
 * An episode that crosses midnight therefore stays anchored to the day it started on.
 */
function fromTimeValue(iso: string, time: string) {
  const [hours, minutes] = time.split(':').map(Number)
  if (Number.isNaN(hours) || Number.isNaN(minutes)) return null
  const value = new Date(iso)
  value.setHours(hours, minutes, 0, 0)
  return value.toISOString()
}

interface Props {
  episode: TimelineEpisode
  selected: boolean
  onToggleSelected: () => void
  onAdjust: (changes: { started_at?: string; ended_at?: string }) => void
  onSplit: (at: string) => void
  onDelete: () => void
  busy?: boolean
  /** Matches the indentation EpisodeCard applies to nested episodes. */
  nested?: boolean
}

export default function EpisodeLabelBar({
  episode, selected, onToggleSelected, onAdjust, onSplit, onDelete, busy, nested,
}: Props) {
  const [splitAt, setSplitAt] = useState('')

  const field = 'min-h-9 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100'
  const splitIso = splitAt ? fromTimeValue(episode.started_at, splitAt) : null
  const splitInside =
    !!splitIso &&
    Date.parse(splitIso) > Date.parse(episode.started_at) &&
    Date.parse(splitIso) < Date.parse(episode.ended_at)

  return (
    <div className={`-mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-b-xl border border-t-0 border-gray-200 bg-gray-50 px-4 py-2 dark:border-gray-700 dark:bg-gray-800/60 ${nested ? 'ml-6' : ''}`}>
      <label className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-200">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelected}
          className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-2 focus:ring-blue-500"
        />
        Select
      </label>

      <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
        Start
        <input
          type="time"
          defaultValue={toTimeValue(episode.started_at)}
          disabled={busy}
          onBlur={event => {
            const next = fromTimeValue(episode.started_at, event.target.value)
            if (next && next !== episode.started_at) onAdjust({ started_at: next })
          }}
          className={field}
        />
      </label>

      <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
        End
        <input
          type="time"
          defaultValue={toTimeValue(episode.ended_at)}
          disabled={busy}
          onBlur={event => {
            const next = fromTimeValue(episode.ended_at, event.target.value)
            if (next && next !== episode.ended_at) onAdjust({ ended_at: next })
          }}
          className={field}
        />
      </label>

      <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
        Split at
        <input
          type="time"
          value={splitAt}
          disabled={busy}
          onChange={event => setSplitAt(event.target.value)}
          className={field}
        />
      </label>
      <Button
        size="sm"
        variant="secondary"
        disabled={busy || !splitInside}
        onClick={() => {
          if (splitIso) onSplit(splitIso)
          setSplitAt('')
        }}
        icon={<Scissors className="h-3.5 w-3.5" />}
      >
        Split
      </Button>
      {splitAt && !splitInside && (
        <span className="text-xs text-amber-700 dark:text-amber-400">Must fall inside the episode.</span>
      )}

      <Button
        size="sm"
        variant="secondary"
        disabled={busy}
        onClick={onDelete}
        icon={<Trash2 className="h-3.5 w-3.5" />}
        className="ml-auto text-red-700 dark:text-red-400"
      >
        Delete
      </Button>
    </div>
  )
}
