import { useState, useEffect } from 'react'
import { User, MapPin, Building, Calendar, Package, Search, RefreshCw, Filter, X } from 'lucide-react'
import { knowledgeGraphApi } from '../../services/api'
import EntityCard, { Entity } from './EntityCard'

interface EntityListProps {
  onEntityClick?: (entity: Entity) => void
}

const entityTypes = [
  { value: '', label: 'All Types', icon: null },
  { value: 'person', label: 'People', icon: <User className="h-4 w-4" /> },
  { value: 'place', label: 'Places', icon: <MapPin className="h-4 w-4" /> },
  { value: 'organization', label: 'Organizations', icon: <Building className="h-4 w-4" /> },
  { value: 'event', label: 'Events', icon: <Calendar className="h-4 w-4" /> },
  { value: 'thing', label: 'Things', icon: <Package className="h-4 w-4" /> },
]

export default function EntityList({ onEntityClick }: EntityListProps) {
  const [entities, setEntities] = useState<Entity[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Entity[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [selectedType, setSelectedType] = useState('')

  const loadEntities = async (type?: string) => {
    try {
      setLoading(true)
      setError(null)
      const response = await knowledgeGraphApi.getEntities(type || undefined)
      setEntities(response.data.entities || [])
    } catch (err: any) {
      console.error('Failed to load entities:', err)
      setError(err.response?.data?.message || 'Failed to load entities')
    } finally {
      setLoading(false)
    }
  }

  const handleSearch = async () => {
    if (!searchQuery.trim()) {
      setSearchResults(null)
      return
    }

    try {
      setSearching(true)
      setError(null)
      const response = await knowledgeGraphApi.searchEntities(searchQuery.trim())
      setSearchResults(response.data.entities || [])
    } catch (err: any) {
      console.error('Search failed:', err)
      setError(err.response?.data?.message || 'Search failed')
    } finally {
      setSearching(false)
    }
  }

  const clearSearch = () => {
    setSearchQuery('')
    setSearchResults(null)
  }

  const handleTypeChange = (type: string) => {
    setSelectedType(type)
    loadEntities(type)
    clearSearch()
  }

  useEffect(() => {
    loadEntities()
  }, [])

  const displayedEntities = searchResults !== null ? searchResults : entities

  // Group entities by type for display
  const groupedEntities = displayedEntities.reduce((acc, entity) => {
    const type = entity.type || 'thing'
    if (!acc[type]) acc[type] = []
    acc[type].push(entity)
    return acc
  }, {} as Record<string, Entity[]>)

  const typeOrder = ['person', 'organization', 'place', 'event', 'thing']
  const sortedTypes = Object.keys(groupedEntities).sort(
    (a, b) => typeOrder.indexOf(a) - typeOrder.indexOf(b)
  )

  return (
    <div className="space-y-4">
      {/* Search and Filter Controls */}
      <div className="flex flex-col sm:flex-row gap-3">
        {/* Search Input */}
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            placeholder="Search entities..."
            className="w-full pl-10 pr-20 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          {searchResults !== null && (
            <button
              onClick={clearSearch}
              className="absolute right-12 top-1/2 transform -translate-y-1/2 p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
              title="Clear search"
            >
              <X className="h-4 w-4" />
            </button>
          )}
          <button
            onClick={handleSearch}
            disabled={searching || !searchQuery.trim()}
            className="absolute right-2 top-1/2 transform -translate-y-1/2 px-2 py-1 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {searching ? '...' : 'Search'}
          </button>
        </div>

        {/* Type Filter */}
        <div className="flex items-center space-x-2">
          <Filter className="h-4 w-4 text-gray-400" />
          <select
            value={selectedType}
            onChange={(e) => handleTypeChange(e.target.value)}
            disabled={searchResults !== null}
            className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          >
            {entityTypes.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </div>

        {/* Refresh Button */}
        <button
          onClick={() => loadEntities(selectedType)}
          disabled={loading}
          className="px-3 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors disabled:opacity-50"
          title="Refresh"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Search Results Info */}
      {searchResults !== null && (
        <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-md p-3">
          <div className="flex items-center justify-between text-sm text-blue-700 dark:text-blue-300">
            <span>Found {searchResults.length} entities matching "{searchQuery}"</span>
            <button
              onClick={clearSearch}
              className="text-blue-600 dark:text-blue-400 hover:underline"
            >
              Clear
            </button>
          </div>
        </div>
      )}

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
          <span className="ml-2 text-gray-600 dark:text-gray-400">Loading entities...</span>
        </div>
      )}

      {/* Entities Display */}
      {!loading && displayedEntities.length > 0 && (
        <div className="space-y-6">
          {selectedType || searchResults !== null ? (
            // Flat list when filtered or searching
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {displayedEntities.map((entity) => (
                <EntityCard
                  key={entity.id}
                  entity={entity}
                  onClick={onEntityClick}
                />
              ))}
            </div>
          ) : (
            // Grouped by type when showing all
            sortedTypes.map((type) => (
              <div key={type}>
                <h3 className="flex items-center space-x-2 text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3 capitalize">
                  {entityTypes.find((t) => t.value === type)?.icon}
                  <span>{type}s</span>
                  <span className="text-sm font-normal text-gray-500 dark:text-gray-400">
                    ({groupedEntities[type].length})
                  </span>
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {groupedEntities[type].map((entity) => (
                    <EntityCard
                      key={entity.id}
                      entity={entity}
                      onClick={onEntityClick}
                    />
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {/* Empty State */}
      {!loading && displayedEntities.length === 0 && !error && (
        <div className="text-center text-gray-500 dark:text-gray-400 py-12">
          <Package className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>
            {searchResults !== null
              ? `No entities found matching "${searchQuery}"`
              : selectedType
              ? `No ${selectedType}s found`
              : 'No entities found'}
          </p>
          <p className="mt-2 text-sm">
            Entities are automatically extracted from your conversations.
          </p>
        </div>
      )}
    </div>
  )
}
