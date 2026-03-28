import { useState, useEffect, useMemo } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Brain, Search, RefreshCw, Calendar, Users, ChevronDown, ChevronRight, List, FolderTree, X } from 'lucide-react'
import { knowledgeGraphApi } from '../services/api'
import { EntityList } from '../components/knowledge-graph'

interface ConvDocPerson {
  name: string
  description: string
}

interface ConvDoc {
  conversation_id: string
  title: string
  summary: string | null
  date: string
  updated_at: string
  people: ConvDocPerson[]
}

interface Person {
  name: string
  description: string
  mention_count: number
}

type Tab = 'conversations' | 'entities'
type ViewMode = 'list' | 'tree'
type GroupBy = 'date' | 'person'

export default function Memories() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [activeTab, setActiveTab] = useState<Tab>((searchParams.get('tab') as Tab) || 'conversations')

  // Conversation docs state
  const [docs, setDocs] = useState<ConvDoc[]>([])
  const [people, setPeople] = useState<Person[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Controls
  const [searchQuery, setSearchQuery] = useState('')
  const [personFilter, setPersonFilter] = useState<string>('')
  const [viewMode, setViewMode] = useState<ViewMode>('list')
  const [groupBy, setGroupBy] = useState<GroupBy>('date')
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())

  const handleTabChange = (tab: Tab) => {
    setActiveTab(tab)
    setSearchParams({ tab })
  }

  const loadDocs = async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await knowledgeGraphApi.getConversationDocs(
        personFilter || undefined
      )
      setDocs(response.data.conversations || [])
    } catch (err: any) {
      setError(err.message || 'Failed to load conversation documents')
    } finally {
      setLoading(false)
    }
  }

  const loadPeople = async () => {
    try {
      const response = await knowledgeGraphApi.getPeople()
      setPeople(response.data.people || [])
    } catch (err: any) {
      console.error('Failed to load people:', err)
    }
  }

  useEffect(() => {
    if (activeTab === 'conversations') {
      loadDocs()
      loadPeople()
    }
  }, [activeTab, personFilter])

  // Client-side search filtering
  const filteredDocs = useMemo(() => {
    if (!searchQuery.trim()) return docs
    const q = searchQuery.toLowerCase()
    return docs.filter(doc =>
      (doc.title?.toLowerCase() || '').includes(q) ||
      (doc.summary?.toLowerCase() || '').includes(q) ||
      doc.people.some(p => p.name.toLowerCase().includes(q))
    )
  }, [docs, searchQuery])

  // Grouping logic
  const groupedDocs = useMemo(() => {
    if (viewMode !== 'tree') return null

    const groups: Record<string, ConvDoc[]> = {}

    if (groupBy === 'date') {
      for (const doc of filteredDocs) {
        const date = new Date(doc.date)
        const key = isNaN(date.getTime())
          ? 'Unknown Date'
          : date.toLocaleDateString('en-US', { year: 'numeric', month: 'long' })
        if (!groups[key]) groups[key] = []
        groups[key].push(doc)
      }
    } else {
      // Group by person
      for (const doc of filteredDocs) {
        if (doc.people.length === 0) {
          const key = 'No People'
          if (!groups[key]) groups[key] = []
          groups[key].push(doc)
        } else {
          for (const person of doc.people) {
            if (!groups[person.name]) groups[person.name] = []
            groups[person.name].push(person.name ? doc : doc) // same doc in multiple groups
          }
        }
      }
    }

    // Sort groups
    const entries = Object.entries(groups)
    if (groupBy === 'date') {
      // Sort by most recent first (parse month names back)
      entries.sort((a, b) => {
        if (a[0] === 'Unknown Date') return 1
        if (b[0] === 'Unknown Date') return -1
        const da = new Date(a[1][0]?.date || 0)
        const db = new Date(b[1][0]?.date || 0)
        return db.getTime() - da.getTime()
      })
    } else {
      // Sort by count descending
      entries.sort((a, b) => b[1].length - a[1].length)
    }

    return entries
  }, [filteredDocs, viewMode, groupBy])

  const toggleGroup = (key: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
  }

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr)
    if (isNaN(date.getTime())) return ''
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  }

  const renderDocRow = (doc: ConvDoc) => (
    <div
      key={doc.conversation_id}
      onClick={() => navigate(`/conversations/${doc.conversation_id}`)}
      className="flex items-start gap-4 px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer rounded-lg transition-colors group"
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate group-hover:text-blue-600 dark:group-hover:text-blue-400">
            {doc.title || 'Untitled'}
          </h3>
          <span className="text-xs text-gray-400 dark:text-gray-500 whitespace-nowrap">
            {formatDate(doc.date)}
          </span>
        </div>
        {doc.summary && (
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
            {doc.summary}
          </p>
        )}
        {doc.people.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1.5">
            {doc.people.map((p, i) => (
              <span
                key={i}
                className="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
              >
                {p.name}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-2">
          <Brain className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Knowledge & Memory
          </h1>
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="flex space-x-1 mb-6 border-b border-gray-200 dark:border-gray-700">
        <button
          onClick={() => handleTabChange('conversations')}
          className={`flex items-center space-x-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
            activeTab === 'conversations'
              ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border-b-2 border-blue-600'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'
          }`}
        >
          <Calendar className="h-4 w-4" />
          <span>Conversations</span>
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
      </div>

      {/* Tab Content */}
      {activeTab === 'entities' && (
        <EntityList
          onEntityClick={(entity) => {
            console.log('Entity clicked:', entity)
          }}
        />
      )}

      {activeTab === 'conversations' && (
        <>
          {/* Controls Bar */}
          <div className="flex flex-wrap items-center gap-3 mb-4">
            {/* Search */}
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Filter by title, summary, or person..."
                className="w-full pl-10 pr-8 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>

            {/* Person filter */}
            <select
              value={personFilter}
              onChange={(e) => setPersonFilter(e.target.value)}
              className="text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">All People</option>
              {people.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name} ({p.mention_count})
                </option>
              ))}
            </select>

            {/* Group by (only in tree mode) */}
            {viewMode === 'tree' && (
              <select
                value={groupBy}
                onChange={(e) => setGroupBy(e.target.value as GroupBy)}
                className="text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="date">Group by Date</option>
                <option value="person">Group by Person</option>
              </select>
            )}

            {/* View toggle */}
            <div className="flex border border-gray-300 dark:border-gray-600 rounded-md overflow-hidden">
              <button
                onClick={() => setViewMode('list')}
                className={`p-2 ${viewMode === 'list'
                  ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-600'
                  : 'bg-white dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700'
                }`}
                title="List view"
              >
                <List className="h-4 w-4" />
              </button>
              <button
                onClick={() => setViewMode('tree')}
                className={`p-2 ${viewMode === 'tree'
                  ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-600'
                  : 'bg-white dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700'
                }`}
                title="Tree view"
              >
                <FolderTree className="h-4 w-4" />
              </button>
            </div>

            {/* Refresh */}
            <button
              onClick={() => { loadDocs(); loadPeople() }}
              disabled={loading}
              className="p-2 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 transition-colors disabled:opacity-50"
              title="Refresh"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>

          {/* Status */}
          {!loading && filteredDocs.length > 0 && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
              {filteredDocs.length} conversation{filteredDocs.length !== 1 ? 's' : ''}
              {personFilter && ` with ${personFilter}`}
              {searchQuery && ` matching "${searchQuery}"`}
            </p>
          )}

          {/* Error */}
          {error && (
            <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-4 mb-4">
              <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
            </div>
          )}

          {/* Loading */}
          {loading && (
            <div className="flex items-center justify-center h-32">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
              <span className="ml-2 text-gray-600 dark:text-gray-400">Loading...</span>
            </div>
          )}

          {/* List View */}
          {!loading && viewMode === 'list' && filteredDocs.length > 0 && (
            <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg divide-y divide-gray-100 dark:divide-gray-700">
              {filteredDocs.map(renderDocRow)}
            </div>
          )}

          {/* Tree View */}
          {!loading && viewMode === 'tree' && groupedDocs && groupedDocs.length > 0 && (
            <div className="space-y-2">
              {groupedDocs.map(([groupKey, groupDocs]) => {
                const isCollapsed = collapsedGroups.has(groupKey)
                return (
                  <div key={groupKey} className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                    <button
                      onClick={() => toggleGroup(groupKey)}
                      className="w-full flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                    >
                      {isCollapsed ? (
                        <ChevronRight className="h-4 w-4 text-gray-400" />
                      ) : (
                        <ChevronDown className="h-4 w-4 text-gray-400" />
                      )}
                      <span>{groupKey}</span>
                      <span className="text-xs text-gray-400 ml-1">({groupDocs.length})</span>
                    </button>
                    {!isCollapsed && (
                      <div className="border-t border-gray-100 dark:border-gray-700 divide-y divide-gray-100 dark:divide-gray-700">
                        {groupDocs.map(renderDocRow)}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* Empty State */}
          {!loading && filteredDocs.length === 0 && !error && (
            <div className="text-center text-gray-500 dark:text-gray-400 py-12">
              <Brain className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>
                {searchQuery
                  ? `No conversations matching "${searchQuery}"`
                  : personFilter
                    ? `No conversations with ${personFilter}`
                    : 'No conversation documents found'}
              </p>
              <p className="text-xs mt-2">
                Conversation documents are created when the knowledge graph processes audio.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
