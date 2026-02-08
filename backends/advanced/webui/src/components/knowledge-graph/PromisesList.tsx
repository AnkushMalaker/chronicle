import { useState, useEffect } from 'react'
import { CheckSquare, Clock, CheckCircle, XCircle, AlertCircle, RefreshCw, Calendar, User, Trash2 } from 'lucide-react'
import { knowledgeGraphApi } from '../../services/api'

export interface Promise {
  id: string
  user_id: string
  action: string
  to_entity_id?: string
  to_entity_name?: string
  status: string
  due_date?: string
  completed_at?: string
  source_conversation_id?: string
  context?: string
  metadata?: any
  created_at?: string
  updated_at?: string
}

interface PromisesListProps {
  onPromiseClick?: (promise: Promise) => void
}

const statusConfig: Record<string, { icon: React.ReactNode; color: string; label: string }> = {
  pending: {
    icon: <Clock className="h-4 w-4" />,
    color: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
    label: 'Pending',
  },
  in_progress: {
    icon: <AlertCircle className="h-4 w-4" />,
    color: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
    label: 'In Progress',
  },
  completed: {
    icon: <CheckCircle className="h-4 w-4" />,
    color: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
    label: 'Completed',
  },
  cancelled: {
    icon: <XCircle className="h-4 w-4" />,
    color: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
    label: 'Cancelled',
  },
  overdue: {
    icon: <AlertCircle className="h-4 w-4" />,
    color: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
    label: 'Overdue',
  },
}

const statusFilters = [
  { value: '', label: 'All' },
  { value: 'pending', label: 'Pending' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'completed', label: 'Completed' },
  { value: 'cancelled', label: 'Cancelled' },
]

export default function PromisesList({ onPromiseClick }: PromisesListProps) {
  const [promises, setPromises] = useState<Promise[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [updatingId, setUpdatingId] = useState<string | null>(null)

  const loadPromises = async (status?: string) => {
    try {
      setLoading(true)
      setError(null)
      const response = await knowledgeGraphApi.getPromises(status || undefined)
      setPromises(response.data.promises || [])
    } catch (err: any) {
      console.error('Failed to load promises:', err)
      setError(err.response?.data?.message || 'Failed to load promises')
    } finally {
      setLoading(false)
    }
  }

  const handleStatusChange = async (promiseId: string, newStatus: string) => {
    try {
      setUpdatingId(promiseId)
      await knowledgeGraphApi.updatePromiseStatus(promiseId, newStatus)
      // Reload to get updated data
      await loadPromises(statusFilter)
    } catch (err: any) {
      console.error('Failed to update promise status:', err)
      setError(err.response?.data?.message || 'Failed to update status')
    } finally {
      setUpdatingId(null)
    }
  }

  const handleDelete = async (promiseId: string) => {
    if (!confirm('Are you sure you want to delete this promise?')) return

    try {
      await knowledgeGraphApi.deletePromise(promiseId)
      setPromises(promises.filter((p) => p.id !== promiseId))
    } catch (err: any) {
      console.error('Failed to delete promise:', err)
      setError(err.response?.data?.message || 'Failed to delete promise')
    }
  }

  const handleFilterChange = (status: string) => {
    setStatusFilter(status)
    loadPromises(status)
  }

  useEffect(() => {
    loadPromises()
  }, [])

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return null
    try {
      return new Date(dateStr).toLocaleDateString()
    } catch {
      return null
    }
  }

  const isOverdue = (promise: Promise) => {
    if (promise.status !== 'pending' && promise.status !== 'in_progress') return false
    if (!promise.due_date) return false
    return new Date(promise.due_date) < new Date()
  }

  return (
    <div className="space-y-4">
      {/* Header and Controls */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3">
        <div className="flex items-center space-x-2">
          <CheckSquare className="h-5 w-5 text-blue-600" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Promises & Tasks
          </h2>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            ({promises.length})
          </span>
        </div>

        <div className="flex items-center space-x-3">
          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => handleFilterChange(e.target.value)}
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {statusFilters.map((filter) => (
              <option key={filter.value} value={filter.value}>
                {filter.label}
              </option>
            ))}
          </select>

          {/* Refresh Button */}
          <button
            onClick={() => loadPromises(statusFilter)}
            disabled={loading}
            className="px-3 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-4">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="flex items-center justify-center h-32">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
          <span className="ml-2 text-gray-600 dark:text-gray-400">Loading promises...</span>
        </div>
      )}

      {/* Promises List */}
      {!loading && promises.length > 0 && (
        <div className="space-y-3">
          {promises.map((promise) => {
            const status = isOverdue(promise) ? 'overdue' : promise.status
            const config = statusConfig[status] || statusConfig.pending

            return (
              <div
                key={promise.id}
                className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
              >
                <div className="flex items-start justify-between gap-4">
                  {/* Left: Status and Content */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center space-x-2 mb-2">
                      <span className={`flex items-center space-x-1 px-2 py-1 rounded-full text-xs ${config.color}`}>
                        {config.icon}
                        <span>{config.label}</span>
                      </span>
                      {promise.due_date && (
                        <span className="flex items-center space-x-1 text-xs text-gray-500 dark:text-gray-400">
                          <Calendar className="h-3 w-3" />
                          <span>Due: {formatDate(promise.due_date)}</span>
                        </span>
                      )}
                    </div>

                    <p
                      className="text-gray-900 dark:text-gray-100 font-medium cursor-pointer hover:text-blue-600 dark:hover:text-blue-400"
                      onClick={() => onPromiseClick?.(promise)}
                    >
                      {promise.action}
                    </p>

                    {promise.to_entity_name && (
                      <div className="flex items-center space-x-1 mt-1 text-sm text-gray-500 dark:text-gray-400">
                        <User className="h-3 w-3" />
                        <span>To: {promise.to_entity_name}</span>
                      </div>
                    )}

                    {promise.context && (
                      <p className="mt-2 text-sm text-gray-500 dark:text-gray-400 line-clamp-2">
                        {promise.context}
                      </p>
                    )}

                    <div className="mt-2 text-xs text-gray-400 dark:text-gray-500">
                      Created {formatDate(promise.created_at)}
                    </div>
                  </div>

                  {/* Right: Actions */}
                  <div className="flex items-center space-x-2">
                    {/* Status Change Buttons */}
                    {promise.status !== 'completed' && promise.status !== 'cancelled' && (
                      <button
                        onClick={() => handleStatusChange(promise.id, 'completed')}
                        disabled={updatingId === promise.id}
                        className="p-2 text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20 rounded-md transition-colors disabled:opacity-50"
                        title="Mark as completed"
                      >
                        <CheckCircle className="h-5 w-5" />
                      </button>
                    )}

                    {promise.status === 'pending' && (
                      <button
                        onClick={() => handleStatusChange(promise.id, 'in_progress')}
                        disabled={updatingId === promise.id}
                        className="p-2 text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-md transition-colors disabled:opacity-50"
                        title="Mark as in progress"
                      >
                        <AlertCircle className="h-5 w-5" />
                      </button>
                    )}

                    {promise.status !== 'cancelled' && promise.status !== 'completed' && (
                      <button
                        onClick={() => handleStatusChange(promise.id, 'cancelled')}
                        disabled={updatingId === promise.id}
                        className="p-2 text-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 rounded-md transition-colors disabled:opacity-50"
                        title="Cancel"
                      >
                        <XCircle className="h-5 w-5" />
                      </button>
                    )}

                    {/* Delete Button */}
                    <button
                      onClick={() => handleDelete(promise.id)}
                      className="p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-md transition-colors"
                      title="Delete promise"
                    >
                      <Trash2 className="h-5 w-5" />
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Empty State */}
      {!loading && promises.length === 0 && !error && (
        <div className="text-center text-gray-500 dark:text-gray-400 py-12">
          <CheckSquare className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>
            {statusFilter
              ? `No ${statusFilters.find((f) => f.value === statusFilter)?.label.toLowerCase()} promises found`
              : 'No promises found'}
          </p>
          <p className="mt-2 text-sm">
            Promises are automatically extracted when you mention commitments in conversations.
          </p>
        </div>
      )}
    </div>
  )
}
