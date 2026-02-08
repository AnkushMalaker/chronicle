import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Brain, Search, RefreshCw, Trash2, Calendar, Tag, X, Target, Users, CheckSquare } from 'lucide-react'
import { memoriesApi, systemApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { EntityList, PromisesList } from '../components/knowledge-graph'
import '../styles/slider.css'

interface Memory {
  id: string
  memory: string
  category?: string
  created_at: string
  updated_at: string
  user_id: string
  score?: number
  metadata?: any
  hash?: string
  role?: string
}

type Tab = 'memories' | 'entities' | 'promises'

export default function Memories() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [activeTab, setActiveTab] = useState<Tab>((searchParams.get('tab') as Tab) || 'memories')
  const [memories, setMemories] = useState<Memory[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [totalCount, setTotalCount] = useState<number | null>(null)

  // Semantic search state
  const [semanticResults, setSemanticResults] = useState<Memory[]>([])
  const [isSemanticFilterActive, setIsSemanticFilterActive] = useState(false)
  const [semanticQuery, setSemanticQuery] = useState('')
  const [semanticLoading, setSemanticLoading] = useState(false)
  const [relevanceThreshold, setRelevanceThreshold] = useState(0) // 0-100 percentage

  // System configuration state
  const [memoryProviderSupportsThreshold, setMemoryProviderSupportsThreshold] = useState(false)
  const [memoryProvider, setMemoryProvider] = useState<string>('')

  const { user } = useAuth()

  const handleTabChange = (tab: Tab) => {
    setActiveTab(tab)
    setSearchParams({ tab })
  }

  const loadSystemConfig = async () => {
    try {
      const response = await systemApi.getMetrics()
      const supports = response.data.memory_provider_supports_threshold || false
      const provider = response.data.memory_provider || 'unknown'
      setMemoryProviderSupportsThreshold(supports)
      setMemoryProvider(provider)
      console.log('🔧 Memory provider:', provider, 'supports threshold:', supports)
    } catch (err: any) {
      console.error('❌ Failed to load system config:', err)
      // Default to false if we can't determine
      setMemoryProviderSupportsThreshold(false)
      setMemoryProvider('unknown')
    }
  }

  const loadMemories = async () => {
    if (!user?.id) return

    try {
      setLoading(true)
      const response = await memoriesApi.getAll(user.id)

      console.log('🧠 Memories API response:', response.data)

      // Handle the API response structure
      const memoriesData = response.data.memories || response.data || []
      const totalCount = response.data.total_count
      console.log('🧠 Processed memories data:', memoriesData)
      console.log('🧠 Total count:', totalCount)

      // Log first few memories to inspect structure
      if (memoriesData.length > 0) {
        console.log('🧠 First memory object:', memoriesData[0])
        console.log('🧠 Memory fields:', Object.keys(memoriesData[0]))
      }

      setMemories(Array.isArray(memoriesData) ? memoriesData : [])
      // Store total count in state for display
      setTotalCount(totalCount)
      setError(null)
    } catch (err: any) {
      console.error('❌ Memory loading error:', err)
      setError(err.message || 'Failed to load memories')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadSystemConfig()
  }, [])

  useEffect(() => {
    loadMemories()
  }, [user?.id])

  // Semantic search handlers
  const handleSemanticSearch = async () => {
    if (!searchQuery.trim() || !user?.id) return

    try {
      setSemanticLoading(true)

      // Use current threshold for server-side filtering if memory provider supports it
      const thresholdToUse = memoryProviderSupportsThreshold
        ? relevanceThreshold
        : undefined

      const response = await memoriesApi.search(
        searchQuery.trim(),
        user.id,
        50,
        thresholdToUse
      )

      console.log('🔍 Search response:', response.data)
      console.log('🎯 Used threshold:', thresholdToUse)

      setSemanticResults(response.data.results || [])
      setSemanticQuery(searchQuery.trim())
      setIsSemanticFilterActive(true)
      setSearchQuery('') // Clear search box
      setError(null)
    } catch (err: any) {
      setError(err.message || 'Semantic search failed')
    } finally {
      setSemanticLoading(false)
    }
  }

  const clearSemanticFilter = () => {
    setIsSemanticFilterActive(false)
    setSemanticResults([])
    setSemanticQuery('')
    setSearchQuery('')
    setRelevanceThreshold(0) // Reset threshold
  }

  const handleDeleteMemory = async (memoryId: string) => {
    if (!confirm('Are you sure you want to delete this memory?')) return

    try {
      await memoriesApi.delete(memoryId)
      // Remove from both regular memories and semantic results if present
      setMemories(memories.filter(m => m.id !== memoryId))
      if (isSemanticFilterActive) {
        setSemanticResults(semanticResults.filter(m => m.id !== memoryId))
      }
    } catch (err: any) {
      setError(err.message || 'Failed to delete memory')
    }
  }

  // Update filtering logic with client-side threshold filtering after search
  const currentMemories = isSemanticFilterActive ? semanticResults : memories

  // Apply relevance threshold filter (client-side for all providers after search)
  const thresholdFilteredMemories = isSemanticFilterActive && relevanceThreshold > 0
    ? currentMemories.filter(memory => {
        if (!memory.score) return true // If no score, show it
        const relevancePercentage = memory.score * 100
        return relevancePercentage >= relevanceThreshold
      })
    : currentMemories

  // Apply text search filter
  const filteredMemories = thresholdFilteredMemories.filter(memory =>
    memory.memory.toLowerCase().includes(searchQuery.toLowerCase()) ||
    (memory.category?.toLowerCase() || '').includes(searchQuery.toLowerCase())
  )

  const formatDate = (dateInput: string | number | undefined | null) => {
    // Handle missing/undefined dates
    if (dateInput === undefined || dateInput === null || dateInput === '') {
      return 'N/A'
    }

    // Handle both timestamp numbers and date strings
    let date: Date

    if (typeof dateInput === 'number') {
      // Unix timestamp - multiply by 1000 if needed
      date = dateInput > 1e10 ? new Date(dateInput) : new Date(dateInput * 1000)
    } else if (typeof dateInput === 'string') {
      // Try parsing as ISO string first, then as timestamp
      if (dateInput.match(/^\d+$/)) {
        // String containing only digits - treat as timestamp
        const timestamp = parseInt(dateInput)
        date = timestamp > 1e10 ? new Date(timestamp) : new Date(timestamp * 1000)
      } else {
        // Regular date string
        date = new Date(dateInput)
      }
    } else {
      date = new Date(dateInput)
    }

    // Check if date is valid
    if (isNaN(date.getTime())) {
      console.warn('Invalid date:', dateInput)
      return 'N/A'
    }

    return date.toLocaleString()
  }

  const getCategoryColor = (category: string) => {
    const colors = {
      'personal': 'bg-blue-100 text-blue-800',
      'work': 'bg-green-100 text-green-800',
      'health': 'bg-red-100 text-red-800',
      'entertainment': 'bg-purple-100 text-purple-800',
      'education': 'bg-yellow-100 text-yellow-800',
      'default': 'bg-gray-100 text-gray-800'
    }
    return colors[category as keyof typeof colors] || colors.default
  }

  // Simple function to render memory content with proper formatting
  const renderMemoryText = (content: string) => {
    // Handle multi-line content (bullet points from backend normalization)
    const lines = content.split('\n').filter(line => line.trim())

    if (lines.length > 1) {
      return (
        <div className="space-y-1">
          {lines.map((line, index) => (
            <div key={index} className="text-gray-900 dark:text-gray-100">
              {line}
            </div>
          ))}
        </div>
      )
    }

    // Single line content
    return (
      <p className="text-gray-900 dark:text-gray-100 leading-relaxed">
        {content}
      </p>
    )
  }

  const renderMemoryContent = (memory: Memory) => {
    // Backend now handles all normalization, so we can directly display the content
    return renderMemoryText(memory.memory)
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-2">
          <Brain className="h-6 w-6 text-blue-600" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Knowledge & Memory
            </h1>
            {memoryProvider && activeTab === 'memories' && (
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                Provider: {memoryProvider === 'chronicle' ? 'Chronicle' : memoryProvider === 'openmemory_mcp' ? 'OpenMemory MCP' : memoryProvider}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="flex space-x-1 mb-6 border-b border-gray-200 dark:border-gray-700">
        <button
          onClick={() => handleTabChange('memories')}
          className={`flex items-center space-x-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
            activeTab === 'memories'
              ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-b-2 border-blue-600'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'
          }`}
        >
          <Brain className="h-4 w-4" />
          <span>Memories</span>
        </button>
        <button
          onClick={() => handleTabChange('entities')}
          className={`flex items-center space-x-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
            activeTab === 'entities'
              ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-b-2 border-blue-600'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'
          }`}
        >
          <Users className="h-4 w-4" />
          <span>Entities</span>
        </button>
        <button
          onClick={() => handleTabChange('promises')}
          className={`flex items-center space-x-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
            activeTab === 'promises'
              ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-b-2 border-blue-600'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'
          }`}
        >
          <CheckSquare className="h-4 w-4" />
          <span>Promises</span>
        </button>
      </div>

      {/* Tab Content */}
      {activeTab === 'entities' && (
        <EntityList
          onEntityClick={(entity) => {
            console.log('Entity clicked:', entity)
            // Could navigate to entity detail page in the future
          }}
        />
      )}

      {activeTab === 'promises' && (
        <PromisesList
          onPromiseClick={(promise) => {
            console.log('Promise clicked:', promise)
            // Could navigate to conversation source
            if (promise.source_conversation_id) {
              navigate(`/conversations/${promise.source_conversation_id}`)
            }
          }}
        />
      )}

      {activeTab === 'memories' && (
        <>

      {/* Controls */}
      <div className="space-y-4 mb-6">
        <div className="flex items-center justify-end">
          <button
            onClick={loadMemories}
            disabled={loading || !user}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>

        {/* Search */}
        {memories.length > 0 && (
          <div className="space-y-4">
            <div className="relative flex items-center">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400 z-10" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search memories..."
                className="w-full pl-10 pr-32 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
                onKeyDown={(e) => e.key === 'Enter' && handleSemanticSearch()}
              />
              <button
                onClick={handleSemanticSearch}
                disabled={!searchQuery.trim() || semanticLoading || !user}
                className="absolute right-2 flex items-center space-x-1 px-3 py-1 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                title="Semantic search using AI"
              >
                <Brain className={`h-3 w-3 ${semanticLoading ? 'animate-pulse' : ''}`} />
                <span>{semanticLoading ? 'Searching...' : 'Semantic'}</span>
              </button>
            </div>

            {/* Initial Search Threshold Slider - Show for Chronicle provider */}
            {memoryProviderSupportsThreshold && (
              <div className="bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md p-4">
                <div className="flex items-center justify-between mb-2">
                  <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    {isSemanticFilterActive ? 'Result Filtering (Client-side)' : 'Initial Search Threshold (Server-side)'}
                  </label>
                  <div className="flex items-center space-x-2">
                    <span className="text-sm text-gray-600 dark:text-gray-400">
                      {relevanceThreshold}%
                    </span>
                  </div>
                </div>
                <div className="flex items-center space-x-3">
                  <input
                    type="range"
                    min="0"
                    max="100"
                    value={relevanceThreshold}
                    onChange={(e) => setRelevanceThreshold(Number(e.target.value))}
                    className="flex-1 h-2 bg-gray-200 dark:bg-gray-700 rounded-lg appearance-none cursor-pointer slider"
                    style={{
                      ['--progress' as any]: `${relevanceThreshold}%`,
                      background: `linear-gradient(to right, #3B82F6 0%, #3B82F6 ${relevanceThreshold}%, #E5E7EB ${relevanceThreshold}%, #E5E7EB 100%)`
                    }}
                    disabled={semanticLoading}
                  />
                  <button
                    onClick={() => setRelevanceThreshold(0)}
                    className="text-xs px-2 py-1 text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 border border-gray-300 dark:border-gray-600 rounded disabled:opacity-50"
                    title="Reset threshold"
                    disabled={semanticLoading}
                  >
                    Reset
                  </button>
                </div>
                <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  {isSemanticFilterActive ? (
                    relevanceThreshold > 0 ?
                      `Filtering loaded results: showing memories with ≥ ${relevanceThreshold}% relevance` :
                      'Showing all loaded results'
                  ) : (
                    relevanceThreshold > 0 ?
                      `Next search will filter server-side: memories with ≥ ${relevanceThreshold}% relevance` :
                      'Next search will return all results (no server-side filtering)'
                  )}
                </p>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Semantic Filter Indicator */}
      {isSemanticFilterActive && (
        <div className="mb-4">
          <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md p-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Brain className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                <span className="text-sm text-blue-700 dark:text-blue-300">
                  Semantic search active: "{semanticQuery}"
                </span>
              </div>
              <button
                onClick={clearSemanticFilter}
                className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-200 transition-colors"
                title="Clear semantic filter"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Status Messages */}
      {user && (memories.length > 0 || isSemanticFilterActive) && (
        <div className="mb-4">
          <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md p-4">
            <p className="text-sm text-blue-700 dark:text-blue-300">
              {isSemanticFilterActive ? (
                relevanceThreshold > 0 ? (
                  searchQuery ? (
                    `Showing ${filteredMemories.length} of ${semanticResults.length} semantic matches (filtered by ≥${relevanceThreshold}% relevance + "${searchQuery}")`
                  ) : (
                    `Showing ${thresholdFilteredMemories.length} of ${semanticResults.length} semantic matches (filtered by ≥${relevanceThreshold}% relevance)`
                  )
                ) : (
                  searchQuery ? (
                    `Showing ${filteredMemories.length} of ${semanticResults.length} semantic matches (filtered by "${searchQuery}")`
                  ) : (
                    `Showing all ${semanticResults.length} semantic matches for "${semanticQuery}"`
                  )
                )
              ) : (
                totalCount !== null ? (
                  `Showing ${memories.length} of ${totalCount} memories`
                ) : (
                  `Showing ${memories.length} memories`
                )
              )}
            </p>
          </div>
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-4 mb-6">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center h-32">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
          <span className="ml-2 text-gray-600 dark:text-gray-400">Loading memories...</span>
        </div>
      )}

      {/* Memories List */}
      {!loading && filteredMemories.length > 0 && (
        <div className="space-y-4">
          <div className="flex justify-between items-center">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {isSemanticFilterActive ? (
                relevanceThreshold > 0 ? (
                  searchQuery ? (
                    `Relevance filtered (≥${relevanceThreshold}%) + text filtered: ${filteredMemories.length} results`
                  ) : (
                    `Relevance filtered (≥${relevanceThreshold}%): ${thresholdFilteredMemories.length} results`
                  )
                ) : (
                  searchQuery ? (
                    `Found ${filteredMemories.length} semantic results matching "${searchQuery}"`
                  ) : (
                    `Found ${filteredMemories.length} semantic matches`
                  )
                )
              ) : (
                searchQuery ? (
                  `Found ${filteredMemories.length} memories matching "${searchQuery}"`
                ) : totalCount !== null ? (
                  `Showing ${filteredMemories.length} of ${totalCount} memories`
                ) : (
                  `Found ${filteredMemories.length} memories`
                )
              )}
            </p>
          </div>

          <div className="space-y-4">
            {filteredMemories.map((memory) => (
              <div
                key={memory.id}
                className="bg-gray-50 dark:bg-gray-700 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-blue-400 dark:hover:border-blue-500 transition-colors group"
              >
                {/* Clickable Memory Content Area */}
                <div
                  onClick={() => navigate(`/memories/${memory.id}`)}
                  className="p-6 cursor-pointer"
                >
                  {/* Memory Header */}
                  <div className="flex justify-between items-start mb-4">
                    <div className="flex items-center space-x-4">
                      <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                        <Calendar className="h-4 w-4" />
                        <span>{formatDate(memory.created_at)}</span>
                      </div>
                      {memory.category && (
                        <div className="flex items-center space-x-2">
                          <Tag className="h-4 w-4 text-gray-400" />
                          <span className={`px-2 py-1 rounded-full text-xs font-medium ${getCategoryColor(memory.category)}`}>
                            {memory.category}
                          </span>
                        </div>
                      )}
                      {memory.score != null && isSemanticFilterActive && (
                        <div className="flex items-center space-x-1 text-xs text-gray-500 dark:text-gray-400">
                          <Target className="h-3 w-3" />
                          <span>Relevance: {(memory.score * 100).toFixed(1)}%</span>
                        </div>
                      )}
                      {memory.score != null && !isSemanticFilterActive && (
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          Score: {memory.score.toFixed(3)}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Memory Title (if available) */}
                  {memory.metadata?.name && (
                    <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3">
                      {memory.metadata.name}
                    </h3>
                  )}

                  {/* Memory Content */}
                  <div className="prose prose-sm max-w-none group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
                    {renderMemoryContent(memory)}
                  </div>

                  {/* Metadata */}
                  {memory.metadata && (
                    <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                      <details className="text-sm">
                        <summary className="cursor-pointer text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100">
                          View metadata
                        </summary>
                        <pre className="mt-2 p-2 bg-gray-100 dark:bg-gray-800 rounded text-xs overflow-x-auto">
                          {JSON.stringify(memory.metadata, null, 2)}
                        </pre>
                      </details>
                    </div>
                  )}
                </div>

                {/* Delete Button - Outside clickable area */}
                <div className="px-6 pb-4">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleDeleteMemory(memory.id)
                    }}
                    className="w-full p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-md transition-colors border border-red-200 dark:border-red-800 hover:border-red-400 dark:hover:border-red-600"
                    title="Delete memory"
                  >
                    <div className="flex items-center justify-center space-x-2">
                      <Trash2 className="h-4 w-4" />
                      <span className="text-sm">Delete Memory</span>
                    </div>
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty States */}
      {!loading && !user && (
        <div className="text-center text-gray-500 dark:text-gray-400 py-12">
          <Brain className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>Please log in to view your memories</p>
        </div>
      )}

      {!loading && user && filteredMemories.length === 0 && !error && (
        <div className="text-center text-gray-500 dark:text-gray-400 py-12">
          <Brain className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>
            {isSemanticFilterActive ? (
              searchQuery ? (
                `No semantic results found matching "${searchQuery}"`
              ) : (
                `No semantic matches found for "${semanticQuery}"`
              )
            ) : (
              searchQuery
                ? `No memories found matching "${searchQuery}"`
                : `No memories found`
            )}
          </p>
          {isSemanticFilterActive && (
            <button
              onClick={clearSemanticFilter}
              className="mt-4 text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-200 text-sm underline"
            >
              Clear semantic filter and view all memories
            </button>
          )}
        </div>
      )}
      </>
      )}
    </div>
  )
}
