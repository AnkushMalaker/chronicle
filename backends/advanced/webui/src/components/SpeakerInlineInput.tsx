import { useState, useRef, useEffect } from 'react'
import { Plus } from 'lucide-react'
import { useSortedSpeakers } from '../hooks/useSortedSpeakers'

interface SpeakerInlineInputProps {
  value: string
  onChange: (value: string) => void
  onSelect: (speaker: string) => void
  enrolledSpeakers: Array<{ speaker_id: string; name: string }>
  recentSpeakers?: string[]
  placeholder?: string
}

/**
 * Small inline text input with autocomplete dropdown for speaker selection.
 * Uses the same sorting logic (recent first, then alphabetical) as SpeakerNameDropdown.
 */
export default function SpeakerInlineInput({
  value,
  onChange,
  onSelect,
  enrolledSpeakers,
  recentSpeakers = [],
  placeholder = 'Type speaker name...',
}: SpeakerInlineInputProps) {
  const [isFocused, setIsFocused] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const sortedSpeakers = useSortedSpeakers(enrolledSpeakers, value, recentSpeakers)
  const hasResults = sortedSpeakers.recent.length > 0 || sortedSpeakers.rest.length > 0
  const showDropdown = isFocused && (hasResults || value.trim())
  const canCreate = value.trim() && !enrolledSpeakers.some(s => s.name.toLowerCase() === value.trim().toLowerCase())

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsFocused(false)
      }
    }
    if (isFocused) {
      document.addEventListener('mousedown', handleClickOutside)
    }
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isFocused])

  const handleSelect = (name: string) => {
    onSelect(name)
    setIsFocused(false)
  }

  const renderItem = (speaker: { speaker_id: string; name: string }) => (
    <button
      key={speaker.speaker_id}
      onMouseDown={(e) => { e.preventDefault(); handleSelect(speaker.name) }}
      className="w-full text-left px-3 py-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-900 dark:text-gray-100 truncate"
    >
      {speaker.name}
    </button>
  )

  return (
    <div className="relative flex-1" ref={containerRef}>
      <input
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        onFocus={() => setIsFocused(true)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && value.trim()) {
            // If there's an exact match or results, select first; otherwise create
            const first = sortedSpeakers.recent[0] || sortedSpeakers.rest[0]
            handleSelect(first ? first.name : value.trim())
          }
        }}
        placeholder={placeholder}
        className="w-full px-2 py-1 text-xs border rounded bg-white dark:bg-gray-700 dark:border-gray-600 focus:outline-none focus:ring-1 focus:ring-purple-500"
      />

      {showDropdown && (
        <div className="absolute top-full left-0 mt-1 w-full min-w-[180px] bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 z-50 max-h-40 overflow-y-auto">
          {sortedSpeakers.recent.length > 0 && (
            <>
              <div className="px-3 py-0.5 text-[10px] text-gray-400 dark:text-gray-500 uppercase tracking-wider bg-gray-50 dark:bg-gray-800/50">
                Recent
              </div>
              {sortedSpeakers.recent.map(renderItem)}
              {sortedSpeakers.rest.length > 0 && (
                <div className="border-t border-gray-100 dark:border-gray-700" />
              )}
            </>
          )}
          {sortedSpeakers.rest.map(renderItem)}
          {!hasResults && !canCreate && (
            <div className="px-3 py-2 text-xs text-gray-400">No speakers found</div>
          )}
          {canCreate && (
            <div className={hasResults ? "border-t border-gray-100 dark:border-gray-700" : ""}>
              <button
                onMouseDown={(e) => { e.preventDefault(); handleSelect(value.trim()) }}
                className="w-full text-left px-3 py-1.5 text-xs text-blue-600 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1"
              >
                <Plus className="h-3 w-3" />
                Create "{value.trim()}"
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
