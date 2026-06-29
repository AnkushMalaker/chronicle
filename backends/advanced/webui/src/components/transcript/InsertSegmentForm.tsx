import { useState } from 'react'
import { annotationsApi } from '../../services/api'
import SpeakerInlineInput from '../SpeakerInlineInput'

interface InsertSegmentFormProps {
  conversationId: string
  afterIndex: number // -1 = before first segment
  allSpeakers: { speaker_id: string; name: string }[]
  recentSpeakers: string[]
  onSpeakerUsed?: (speaker: string) => void
  /** Optional waveform-drawn span for the new segment (else a zero-duration boundary marker). */
  region?: { start: number; end: number } | null
  onDone: () => void // created → reload + close
  onCancel: () => void
}

const EVENT_TAGS = ['[laughter]', '[music]', '[applause]', '[silence]', '[unintelligible]', '[crosstalk]']

/**
 * The "good little menu" for inserting a new segment between existing ones — the same
 * form used on the conversation list and the detail page (one implementation).
 * Self-contained state; commits a pending INSERT annotation via the API.
 */
export default function InsertSegmentForm({
  conversationId,
  afterIndex,
  allSpeakers,
  recentSpeakers,
  onSpeakerUsed,
  region,
  onDone,
  onCancel,
}: InsertSegmentFormProps) {
  const [text, setText] = useState('')
  const [type, setType] = useState<'event' | 'note' | 'speech'>('speech')
  const [speaker, setSpeaker] = useState('')
  const [saving, setSaving] = useState(false)

  const create = async () => {
    if (!text.trim() || saving) return
    try {
      setSaving(true)
      await annotationsApi.createInsertAnnotation({
        conversation_id: conversationId,
        insert_after_index: afterIndex,
        insert_text: text.trim(),
        insert_segment_type: type,
        ...(type === 'speech' && speaker ? { insert_speaker: speaker } : {}),
        ...(region ? { insert_start: region.start, insert_end: region.end } : {}),
      })
      onDone()
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="w-full border border-purple-200 dark:border-purple-700 rounded-lg p-2 bg-purple-50 dark:bg-purple-900/20 space-y-2"
      onClick={(e) => e.stopPropagation()}
    >
      {type !== 'speech' && (
        <div className="flex flex-wrap gap-1">
          {EVENT_TAGS.map((tag) => (
            <button
              key={tag}
              onClick={() => setText(tag)}
              className={`px-2 py-0.5 text-xs rounded border transition-colors ${
                text === tag
                  ? 'bg-purple-200 dark:bg-purple-700 border-purple-400 dark:border-purple-500'
                  : 'bg-white dark:bg-gray-700 border-gray-300 dark:border-gray-600 hover:border-purple-300'
              }`}
            >
              {tag}
            </button>
          ))}
        </div>
      )}
      {type === 'speech' && (
        <div className="flex items-center gap-2">
          <label className="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">Speaker:</label>
          <SpeakerInlineInput
            value={speaker}
            onChange={setSpeaker}
            onSelect={(s) => {
              setSpeaker(s)
              onSpeakerUsed?.(s)
            }}
            enrolledSpeakers={allSpeakers}
            recentSpeakers={recentSpeakers}
            placeholder="Type or select speaker..."
          />
        </div>
      )}
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={type === 'speech' ? 'What was said...' : 'Custom text...'}
          className="flex-1 px-2 py-1 text-xs border rounded bg-white dark:bg-gray-700 dark:border-gray-600 focus:outline-none focus:ring-1 focus:ring-purple-500"
          onKeyDown={(e) => {
            if (e.key === 'Enter') create()
            if (e.key === 'Escape') onCancel()
          }}
          autoFocus
        />
        <select
          value={type}
          onChange={(e) => setType(e.target.value as 'event' | 'note' | 'speech')}
          className="px-2 py-1 text-xs border rounded bg-white dark:bg-gray-700 dark:border-gray-600"
        >
          <option value="speech">Speech</option>
          <option value="event">Event Tag</option>
          <option value="note">Note</option>
        </select>
        <button
          onClick={create}
          disabled={!text.trim() || saving}
          className="px-2 py-1 text-xs text-white bg-purple-600 rounded hover:bg-purple-700 disabled:opacity-50"
        >
          Insert
        </button>
        <button
          onClick={onCancel}
          className="px-2 py-1 text-xs text-gray-600 dark:text-gray-300 bg-gray-200 dark:bg-gray-600 rounded hover:bg-gray-300"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
