import { KeyboardEvent, useId } from 'react'

const COMMON_EPISODE_KINDS = [
  'media',
  'conversation',
  'spoken_activity',
  'technical_work',
  'focused_work',
  'meeting',
  'voice_check',
]

export default function EpisodeKindField({
  value,
  onChange,
  onCommit,
  disabled,
  className,
}: {
  value: string
  onChange: (value: string) => void
  onCommit?: (value: string) => void
  disabled?: boolean
  className?: string
}) {
  const listId = `episode-kinds-${useId().replace(/:/g, '')}`
  const options = Array.from(new Set([value, ...COMMON_EPISODE_KINDS].filter(Boolean)))

  const commit = () => {
    const next = value.trim()
    if (next) onCommit?.(next)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') event.currentTarget.blur()
  }

  return (
    <>
      <input
        type="text"
        role="combobox"
        aria-label="Episode type"
        aria-autocomplete="list"
        aria-controls={listId}
        list={listId}
        value={value}
        disabled={disabled}
        onChange={event => onChange(event.target.value)}
        onBlur={commit}
        onKeyDown={handleKeyDown}
        className={className}
      />
      <datalist id={listId}>
        {options.map(option => <option key={option} value={option} />)}
      </datalist>
    </>
  )
}
