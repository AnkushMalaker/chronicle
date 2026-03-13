import React, { useState } from 'react'
import { User, Plus, ChevronDown } from 'lucide-react'
import { useUser } from '../contexts/UserContext'

export default function UserSelector() {
  const { user, users, isLoading, selectUser, createUser } = useUser()
  const [isDropdownOpen, setIsDropdownOpen] = useState(false)
  const [isCreating, setIsCreating] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)

  const handleSelectUser = async (username: string) => {
    try {
      await selectUser(username)
      setIsDropdownOpen(false)
    } catch (error) {
      console.error('Failed to select user:', error)
    }
  }

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newUsername.trim()) return

    setIsSubmitting(true)
    try {
      await createUser(newUsername.trim())
      setNewUsername('')
      setIsCreating(false)
      setIsDropdownOpen(false)
    } catch (error) {
      console.error('Failed to create user:', error)
    } finally {
      setIsSubmitting(false)
    }
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
            {/* Existing Users */}
            {users.length > 0 && (
              <>
                <div className="px-3 py-1 text-xs font-medium text-muted uppercase tracking-wide">
                  Select User
                </div>
                {users.map((u) => (
                  <button
                    key={u.id}
                    onClick={() => handleSelectUser(u.username)}
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
                <div className="border-t my-2 border-gray-200 dark:border-gray-700"></div>
              </>
            )}

            {/* Create New User */}
            {!isCreating ? (
              <button
                onClick={() => setIsCreating(true)}
                className="w-full text-left px-3 py-2 text-sm transition-colors flex items-center space-x-2 text-secondary hover-bg"
              >
                <Plus className="h-4 w-4" />
                <span>Create New User</span>
              </button>
            ) : (
              <div className="px-3 py-2">
                <form onSubmit={handleCreateUser} className="space-y-2">
                  <input
                    type="text"
                    value={newUsername}
                    onChange={(e) => setNewUsername(e.target.value)}
                    placeholder="Username"
                    className="input-primary"
                    autoFocus
                    disabled={isSubmitting}
                  />
                  <div className="flex space-x-2">
                    <button
                      type="submit"
                      disabled={!newUsername.trim() || isSubmitting}
                      className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isSubmitting ? 'Creating...' : 'Create'}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setIsCreating(false)
                        setNewUsername('')
                      }}
                      className="btn-secondary"
                      disabled={isSubmitting}
                    >
                      Cancel
                    </button>
                  </div>
                </form>
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
