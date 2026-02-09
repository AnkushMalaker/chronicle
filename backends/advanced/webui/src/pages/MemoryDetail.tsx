import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Calendar, Tag, Trash2, RefreshCw, Edit3, Save, X } from 'lucide-react'
import { memoriesApi, annotationsApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'

interface Memory {
  id: string
  memory: string
  category?: string
  created_at: string
  updated_at: string
  user_id: string
  score?: number
  metadata?: {
    name?: string
    timeRanges?: Array<{
      start: string
      end: string
      name?: string
    }>
    isPerson?: boolean
    isEvent?: boolean
    isPlace?: boolean
    extractedWith?: {
      model: string
      timestamp: string
    }
    [key: string]: any
  }
  hash?: string
  role?: string
}

export default function MemoryDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [memory, setMemory] = useState<Memory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Inline editing state
  const [isEditing, setIsEditing] = useState(false)
  const [editedContent, setEditedContent] = useState('')
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const loadMemory = async () => {
    if (!user?.id || !id) {
      console.log('⏭️ MemoryDetail: Missing user or id', { userId: user?.id, memoryId: id })
      return
    }

    try {
      console.log('🔍 MemoryDetail: Loading memory', id)
      setLoading(true)
      setError(null)
      const response = await memoriesApi.getById(id, user.id)
      const memoryData = response.data.memory
      console.log('📦 MemoryDetail: Loaded memory', memoryData?.id)

      if (memoryData) {
        setMemory(memoryData)
      } else {
        setError('Memory not found')
      }
    } catch (err: any) {
      console.error('❌ Failed to load memory:', err)
      if (err.response?.status === 404) {
        setError('Memory not found')
      } else {
        setError(err.message || 'Failed to load memory')
      }
    } finally {
      setLoading(false)
    }
  }

  const handleStartEdit = () => {
    if (memory) {
      setEditedContent(memory.memory)
      setIsEditing(true)
      setSaveError(null)
    }
  }

  const handleCancelEdit = () => {
    setIsEditing(false)
    setEditedContent('')
    setSaveError(null)
  }

  const handleSaveEdit = async () => {
    if (!memory || !id || !user?.id) return

    // Don't save if content hasn't changed
    if (editedContent === memory.memory) {
      setIsEditing(false)
      return
    }

    setIsSaving(true)
    setSaveError(null)

    try {
      // Create annotation to update memory
      await annotationsApi.createMemoryAnnotation({
        memory_id: id,
        original_text: memory.memory,
        corrected_text: editedContent
      })

      // Update local state
      setMemory({
        ...memory,
        memory: editedContent,
        updated_at: new Date().toISOString()
      })

      setIsEditing(false)
      console.log('✅ Memory updated successfully')
    } catch (err: any) {
      console.error('❌ Failed to save memory:', err)
      setSaveError(err.response?.data?.detail || err.message || 'Failed to save changes')
    } finally {
      setIsSaving(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSaveEdit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      handleCancelEdit()
    }
  }

  const handleDelete = async () => {
    if (!memory || !id) return

    const confirmed = window.confirm('Are you sure you want to delete this memory?')
    if (!confirmed) return

    try {
      await memoriesApi.delete(id)
      navigate('/memories')
    } catch (err: any) {
      console.error('❌ Failed to delete memory:', err)
      alert('Failed to delete memory: ' + (err.message || 'Unknown error'))
    }
  }

  useEffect(() => {
    loadMemory()
  }, [id, user?.id])

  const formatDate = (dateInput: string | number | undefined | null) => {
    if (dateInput === undefined || dateInput === null || dateInput === '') {
      return 'N/A'
    }

    let date: Date

    if (typeof dateInput === 'number') {
      date = dateInput > 1e10 ? new Date(dateInput) : new Date(dateInput * 1000)
    } else if (typeof dateInput === 'string') {
      if (dateInput.match(/^\d+$/)) {
        const timestamp = parseInt(dateInput)
        date = timestamp > 1e10 ? new Date(timestamp) : new Date(timestamp * 1000)
      } else {
        date = new Date(dateInput)
      }
    } else {
      date = new Date(dateInput)
    }

    if (isNaN(date.getTime())) {
      return 'N/A'
    }

    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  const getMemoryTypeIcon = () => {
    if (memory?.metadata?.isEvent) return '📅'
    if (memory?.metadata?.isPerson) return '👤'
    if (memory?.metadata?.isPlace) return '📍'
    return '🧠'
  }

  const getMemoryTypeLabel = () => {
    if (memory?.metadata?.isEvent) return 'Event'
    if (memory?.metadata?.isPerson) return 'Person'
    if (memory?.metadata?.isPlace) return 'Place'
    return 'Memory'
  }

  if (loading) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <div className="flex items-center gap-4 mb-6">
          <button
            onClick={() => navigate('/memories')}
            className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
          >
            <ArrowLeft className="w-5 h-5" />
            Back
          </button>
        </div>
        <div className="flex items-center justify-center py-12">
          <RefreshCw className="w-6 h-6 animate-spin text-blue-600" />
          <span className="ml-3 text-gray-600 dark:text-gray-400">Loading memory...</span>
        </div>
      </div>
    )
  }

  if (error || !memory) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <div className="flex items-center gap-4 mb-6">
          <button
            onClick={() => navigate('/memories')}
            className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
          >
            <ArrowLeft className="w-5 h-5" />
            Back
          </button>
        </div>
        <div className="border border-red-200 dark:border-red-800 rounded-lg p-8 text-center bg-red-50 dark:bg-red-900/20">
          <p className="text-red-600 dark:text-red-400">
            {error || 'Memory not found'}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button
          onClick={() => navigate('/memories')}
          className="flex items-center gap-2 text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100 transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
          Back to Memories
        </button>
        <button
          onClick={handleDelete}
          className="flex items-center gap-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors"
        >
          <Trash2 className="w-4 h-4" />
          Delete
        </button>
      </div>

      {/* Main Content */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column - Memory Content */}
        <div className="lg:col-span-2 space-y-6">
          {/* Memory Card */}
          <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
            <div className="flex items-start gap-3 mb-4">
              <div className="text-3xl">{getMemoryTypeIcon()}</div>
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-2">
                  <span className="px-2 py-1 text-xs font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 rounded">
                    {getMemoryTypeLabel()}
                  </span>
                  {memory.category && (
                    <span className="px-2 py-1 text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded flex items-center gap-1">
                      <Tag className="w-3 h-3" />
                      {memory.category}
                    </span>
                  )}
                </div>
                {memory.metadata?.name && (
                  <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-3">
                    {memory.metadata.name}
                  </h1>
                )}

                {/* Editable Memory Content */}
                <div className="relative">
                  {isEditing ? (
                    // Edit mode
                    <div className="space-y-3">
                      <textarea
                        value={editedContent}
                        onChange={(e) => setEditedContent(e.target.value)}
                        onKeyDown={handleKeyDown}
                        className="w-full min-h-[150px] px-4 py-3 text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border-2 border-blue-500 dark:border-blue-400 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 dark:focus:ring-blue-400 resize-y leading-relaxed"
                        placeholder="Enter memory content..."
                        autoFocus
                        disabled={isSaving}
                      />

                      {saveError && (
                        <div className="text-sm text-red-600 dark:text-red-400">
                          {saveError}
                        </div>
                      )}

                      <div className="flex items-center gap-2">
                        <button
                          onClick={handleSaveEdit}
                          disabled={isSaving || editedContent === memory.memory}
                          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <Save className="w-4 h-4" />
                          {isSaving ? 'Saving...' : 'Save'}
                        </button>
                        <button
                          onClick={handleCancelEdit}
                          disabled={isSaving}
                          className="flex items-center gap-2 px-4 py-2 bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <X className="w-4 h-4" />
                          Cancel
                        </button>
                        <span className="text-xs text-gray-500 dark:text-gray-400 ml-2">
                          Ctrl+Enter to save, Esc to cancel
                        </span>
                      </div>
                    </div>
                  ) : (
                    // View mode with hover to edit
                    <div
                      onClick={handleStartEdit}
                      className="group cursor-pointer rounded-lg p-3 -mx-3 transition-colors hover:bg-yellow-50 dark:hover:bg-yellow-900/10"
                      title="Click to edit"
                    >
                      <p className="text-gray-700 dark:text-gray-300 leading-relaxed whitespace-pre-wrap">
                        {memory.memory}
                      </p>
                      <div className="opacity-0 group-hover:opacity-100 transition-opacity mt-2 flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400">
                        <Edit3 className="w-4 h-4" />
                        <span>Click to edit</span>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Time Ranges */}
          {memory.metadata?.timeRanges && memory.metadata.timeRanges.length > 0 && (
            <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <Calendar className="w-5 h-5" />
                Time Ranges
              </h2>
              <div className="space-y-3">
                {memory.metadata.timeRanges.map((range, index) => (
                  <div key={index} className="flex items-start gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                    <Calendar className="w-4 h-4 mt-1 text-blue-600 dark:text-blue-400" />
                    <div className="flex-1">
                      {range.name && (
                        <div className="font-medium text-gray-900 dark:text-gray-100 mb-1">
                          {range.name}
                        </div>
                      )}
                      <div className="text-sm text-gray-600 dark:text-gray-400">
                        <div><strong>Start:</strong> {formatDate(range.start)}</div>
                        <div><strong>End:</strong> {formatDate(range.end)}</div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right Column - Metadata */}
        <div className="space-y-6">
          {/* Metadata Card */}
          <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
              Metadata
            </h3>
            <dl className="space-y-3 text-sm">
              <div className="flex justify-between items-start">
                <dt className="text-gray-600 dark:text-gray-400">Created:</dt>
                <dd className="text-gray-900 dark:text-gray-100 text-right">
                  {formatDate(memory.created_at)}
                </dd>
              </div>
              <div className="flex justify-between items-start">
                <dt className="text-gray-600 dark:text-gray-400">Updated:</dt>
                <dd className="text-gray-900 dark:text-gray-100 text-right">
                  {formatDate(memory.updated_at)}
                </dd>
              </div>
              {memory.score !== undefined && memory.score !== null && (
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400">Score:</dt>
                  <dd className="text-gray-900 dark:text-gray-100">
                    {memory.score.toFixed(3)}
                  </dd>
                </div>
              )}
              {memory.hash && (
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400">Hash:</dt>
                  <dd className="font-mono text-xs text-gray-900 dark:text-gray-100 truncate max-w-[150px]" title={memory.hash}>
                    {memory.hash.substring(0, 12)}...
                  </dd>
                </div>
              )}
            </dl>
          </div>

          {/* Extraction Metadata */}
          {memory.metadata?.extractedWith && (
            <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
                Extraction
              </h3>
              <dl className="space-y-3 text-sm">
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400">Model:</dt>
                  <dd className="font-mono text-xs text-gray-900 dark:text-gray-100">
                    {memory.metadata.extractedWith.model}
                  </dd>
                </div>
                <div className="flex justify-between items-start">
                  <dt className="text-gray-600 dark:text-gray-400">Time:</dt>
                  <dd className="text-gray-900 dark:text-gray-100 text-right">
                    {formatDate(memory.metadata.extractedWith.timestamp)}
                  </dd>
                </div>
              </dl>
            </div>
          )}

          {/* Additional Metadata */}
          {memory.metadata && Object.keys(memory.metadata).filter(key =>
            !['name', 'timeRanges', 'isPerson', 'isEvent', 'isPlace', 'extractedWith'].includes(key)
          ).length > 0 && (
            <div className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase mb-3">
                Additional Data
              </h3>
              <dl className="space-y-2 text-sm">
                {Object.entries(memory.metadata)
                  .filter(([key]) => !['name', 'timeRanges', 'isPerson', 'isEvent', 'isPlace', 'extractedWith'].includes(key))
                  .map(([key, value]) => (
                    <div key={key} className="flex justify-between items-start gap-2">
                      <dt className="text-gray-600 dark:text-gray-400 capitalize">{key}:</dt>
                      <dd className="text-gray-900 dark:text-gray-100 text-right truncate max-w-[150px]" title={String(value)}>
                        {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                      </dd>
                    </div>
                  ))}
              </dl>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
