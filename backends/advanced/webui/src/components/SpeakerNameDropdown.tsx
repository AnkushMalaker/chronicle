import { useState, useRef, useEffect } from 'react'
import { Check, Plus, UserX } from 'lucide-react'
import { useSortedSpeakers } from '../hooks/useSortedSpeakers'

const UNKNOWN_SPEAKER = 'Unknown Speaker'

interface SpeakerNameDropdownProps {
  currentSpeaker: string
  enrolledSpeakers: Array<{ speaker_id: string; name: string }>
  onSpeakerChange: (newSpeaker: string) => void
  segmentIndex: number
  conversationId: string
  annotated?: boolean
  speakerColor?: string
  recentSpeakers?: string[]
}

export default function SpeakerNameDropdown({
  currentSpeaker,
  enrolledSpeakers,
  onSpeakerChange,
  annotated = false,
  speakerColor = 'text-blue-700 dark:text-blue-300',
  recentSpeakers = [],
}: SpeakerNameDropdownProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)

  const sortedSpeakers = useSortedSpeakers(enrolledSpeakers, searchQuery, recentSpeakers)
  const hasResults = sortedSpeakers.recent.length > 0 || sortedSpeakers.rest.length > 0

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false)
        setSearchQuery('')
      }
    }

    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [isOpen])

  const handleSpeakerSelect = (speaker: string) => {
    onSpeakerChange(speaker)
    setIsOpen(false)
    setSearchQuery('')
  }

  const handleCreateNewSpeaker = () => {
    if (searchQuery.trim()) {
      onSpeakerChange(searchQuery.trim())
      setIsOpen(false)
      setSearchQuery('')
    }
  }

  const renderSpeakerItem = (speaker: { speaker_id: string; name: string }) => (
    <button
      key={speaker.speaker_id}
      onClick={() => handleSpeakerSelect(speaker.name)}
      className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-between"
    >
      <span className="text-gray-900 dark:text-gray-100">{speaker.name}</span>
      {speaker.name === currentSpeaker && (
        <Check className="h-4 w-4 text-green-600" />
      )}
    </button>
  )

  const canCreate = searchQuery && !enrolledSpeakers.some(s => s.name.toLowerCase() === searchQuery.toLowerCase())

  return (
    <div className="relative inline-block" ref={dropdownRef}>
      {/* Speaker name button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`font-medium hover:underline cursor-pointer ${
          annotated ? 'text-orange-600 dark:text-orange-400' : speakerColor
        }`}
        title={annotated ? 'This segment has a pending annotation' : 'Click to edit speaker'}
      >
        {currentSpeaker}
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <div className="absolute top-full left-0 mt-1 w-64 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 z-50">
          {/* Search input */}
          <div className="p-2 border-b border-gray-200 dark:border-gray-700">
            <input
              type="text"
              placeholder="Search or create speaker..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  if (hasResults) {
                    const first = sortedSpeakers.recent[0] || sortedSpeakers.rest[0]
                    if (first) handleSpeakerSelect(first.name)
                  } else if (searchQuery) {
                    handleCreateNewSpeaker()
                  }
                }
              }}
            />
          </div>

          {/* Unknown Speaker option */}
          {(!searchQuery || UNKNOWN_SPEAKER.toLowerCase().includes(searchQuery.toLowerCase())) && (
            <div className="border-b border-gray-200 dark:border-gray-700">
              <button
                onClick={() => handleSpeakerSelect(UNKNOWN_SPEAKER)}
                className="w-full text-left px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-between"
              >
                <span className="flex items-center space-x-2 text-gray-500 dark:text-gray-400">
                  <UserX className="h-4 w-4" />
                  <span>{UNKNOWN_SPEAKER}</span>
                </span>
                {currentSpeaker === UNKNOWN_SPEAKER && (
                  <Check className="h-4 w-4 text-green-600" />
                )}
              </button>
            </div>
          )}

          {/* Speaker list */}
          <div className="max-h-60 overflow-y-auto">
            {hasResults ? (
              <>
                {sortedSpeakers.recent.length > 0 && (
                  <>
                    <div className="px-4 py-1 text-xs text-gray-400 dark:text-gray-500 uppercase tracking-wider bg-gray-50 dark:bg-gray-800/50">
                      Recent
                    </div>
                    {sortedSpeakers.recent.map(renderSpeakerItem)}
                    {sortedSpeakers.rest.length > 0 && (
                      <div className="border-t border-gray-200 dark:border-gray-700" />
                    )}
                  </>
                )}
                {sortedSpeakers.rest.map(renderSpeakerItem)}
              </>
            ) : (
              <div className="px-4 py-3 text-sm text-gray-500">
                No matching speakers found
              </div>
            )}
          </div>

          {/* Create new speaker option */}
          {canCreate && (
            <div className="border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={handleCreateNewSpeaker}
                className="w-full text-left px-4 py-2 text-sm text-blue-600 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2"
              >
                <Plus className="h-4 w-4" />
                <span>Create "{searchQuery}"</span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
