import { useState } from 'react'
import { User, ChevronDown } from 'lucide-react'
import { useUser } from '../contexts/UserContext'

export default function UserSelector() {
  const { user, users, isLoading, selectUser } = useUser()
  const [isDropdownOpen, setIsDropdownOpen] = useState(false)

  const handleSelectUser = (userId: string) => {
    selectUser(userId)
    setIsDropdownOpen(false)
  }

  if (isLoading) {
    return (
      <div className="flex items-center space-x-2 text-muted">
        <User className="h-5 w-5" />
        <span>Loading...</span>
      </div>
    )
  }

  return (
    <div className="relative">
      {/* Current User Display / Dropdown Trigger */}
      <button
        onClick={() => setIsDropdownOpen(!isDropdownOpen)}
        className="flex items-center space-x-2 px-3 py-2 card-secondary hover-bg rounded-md transition-colors"
      >
        <User className="h-5 w-5 text-secondary" />
        <span className="text-sm font-medium text-primary">
          {user ? user.username : 'Select User'}
        </span>
        <ChevronDown className="h-4 w-4 text-muted" />
      </button>

      {/* Dropdown Menu */}
      {isDropdownOpen && (
        <div className="absolute right-0 mt-2 w-64 card shadow-lg z-50">
          <div className="py-2">
            {users.length > 0 ? (
              <>
                <div className="px-3 py-1 text-xs font-medium text-muted uppercase tracking-wide">
                  Select User
                </div>
                {users.map((u) => (
                  <button
                    key={u.id}
                    onClick={() => handleSelectUser(u.id)}
                    className={`w-full text-left px-3 py-2 text-sm transition-colors hover-bg ${
                      user?.id === u.id ? 'bg-blue-50 dark:bg-blue-900 text-blue-900 dark:text-blue-100' : 'text-secondary'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span>{u.username}</span>
                      {user?.id === u.id && (
                        <span className="text-xs text-blue-600 dark:text-blue-300">Current</span>
                      )}
                    </div>
                  </button>
                ))}
              </>
            ) : (
              <div className="px-3 py-2 text-sm text-muted">
                No users yet. A user appears here once Chronicle enrolls a speaker for them.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Backdrop to close dropdown */}
      {isDropdownOpen && (
        <div
          className="fixed inset-0 z-40"
          onClick={() => setIsDropdownOpen(false)}
        />
      )}
    </div>
  )
}
