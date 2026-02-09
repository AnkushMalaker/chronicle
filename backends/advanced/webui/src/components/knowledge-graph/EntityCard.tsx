import { useState } from 'react'
import { User, MapPin, Building, Calendar, Package, Link2, Pencil, Check, X } from 'lucide-react'
import { knowledgeGraphApi } from '../../services/api'

export interface Entity {
  id: string
  name: string
  type: string
  user_id: string
  details?: string
  icon?: string
  metadata?: any
  created_at?: string
  updated_at?: string
  location?: { lat: number; lon: number }
  start_time?: string
  end_time?: string
  conversation_id?: string
  relationship_count?: number
}

interface EntityCardProps {
  entity: Entity
  onClick?: (entity: Entity) => void
  onEntityUpdated?: (entity: Entity) => void
  compact?: boolean
}

const typeIcons: Record<string, React.ReactNode> = {
  person: <User className="h-4 w-4" />,
  place: <MapPin className="h-4 w-4" />,
  organization: <Building className="h-4 w-4" />,
  event: <Calendar className="h-4 w-4" />,
  thing: <Package className="h-4 w-4" />,
}

const typeColors: Record<string, string> = {
  person: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  place: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  organization: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  event: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  thing: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
}

export default function EntityCard({ entity, onClick, onEntityUpdated, compact = false }: EntityCardProps) {
  const [isEditing, setIsEditing] = useState(false)
  const [editName, setEditName] = useState(entity.name)
  const [editDetails, setEditDetails] = useState(entity.details || '')
  const [saving, setSaving] = useState(false)

  const icon = typeIcons[entity.type] || <Package className="h-4 w-4" />
  const colorClass = typeColors[entity.type] || typeColors.thing

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return null
    try {
      return new Date(dateStr).toLocaleDateString()
    } catch {
      return null
    }
  }

  const handleEditClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    setEditName(entity.name)
    setEditDetails(entity.details || '')
    setIsEditing(true)
  }

  const handleCancel = (e: React.MouseEvent) => {
    e.stopPropagation()
    setIsEditing(false)
  }

  const handleSave = async (e: React.MouseEvent) => {
    e.stopPropagation()
    const updates: { name?: string; details?: string } = {}
    if (editName.trim() !== entity.name) updates.name = editName.trim()
    if (editDetails.trim() !== (entity.details || '')) updates.details = editDetails.trim()

    if (Object.keys(updates).length === 0) {
      setIsEditing(false)
      return
    }

    try {
      setSaving(true)
      const response = await knowledgeGraphApi.updateEntity(entity.id, updates)
      setIsEditing(false)
      onEntityUpdated?.(response.data.entity)
    } catch (err) {
      console.error('Failed to update entity:', err)
    } finally {
      setSaving(false)
    }
  }

  if (compact) {
    return (
      <div
        onClick={() => onClick?.(entity)}
        className={`flex items-center space-x-2 p-2 rounded-lg ${colorClass} cursor-pointer hover:opacity-80 transition-opacity`}
      >
        {entity.icon ? (
          <span className="text-lg">{entity.icon}</span>
        ) : (
          icon
        )}
        <span className="font-medium truncate">{entity.name}</span>
        {entity.relationship_count != null && entity.relationship_count > 0 && (
          <span className="flex items-center text-xs opacity-75">
            <Link2 className="h-3 w-3 mr-0.5" />
            {entity.relationship_count}
          </span>
        )}
      </div>
    )
  }

  return (
    <div
      onClick={() => !isEditing && onClick?.(entity)}
      className={`bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 hover:border-blue-400 dark:hover:border-blue-500 transition-colors ${isEditing ? '' : 'cursor-pointer'} group`}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center space-x-3 min-w-0 flex-1">
          <div className={`p-2 rounded-lg ${colorClass} shrink-0`}>
            {entity.icon ? (
              <span className="text-xl">{entity.icon}</span>
            ) : (
              icon
            )}
          </div>
          <div className="min-w-0 flex-1">
            {isEditing ? (
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                className="w-full px-2 py-1 text-sm font-semibold border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
                autoFocus
              />
            ) : (
              <h3 className="font-semibold text-gray-900 dark:text-gray-100 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors truncate">
                {entity.name}
              </h3>
            )}
            <span className={`inline-block px-2 py-0.5 rounded text-xs capitalize ${colorClass}`}>
              {entity.type}
            </span>
          </div>
        </div>

        <div className="flex items-center space-x-2 shrink-0">
          {isEditing ? (
            <>
              <button
                onClick={handleSave}
                disabled={saving || !editName.trim()}
                className="p-1 text-green-600 hover:text-green-700 dark:text-green-400 dark:hover:text-green-300 disabled:opacity-50"
                title="Save"
              >
                <Check className="h-4 w-4" />
              </button>
              <button
                onClick={handleCancel}
                disabled={saving}
                className="p-1 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                title="Cancel"
              >
                <X className="h-4 w-4" />
              </button>
            </>
          ) : (
            <>
              <button
                onClick={handleEditClick}
                className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 opacity-0 group-hover:opacity-100 transition-opacity"
                title="Edit entity"
              >
                <Pencil className="h-4 w-4" />
              </button>
              {entity.relationship_count != null && entity.relationship_count > 0 && (
                <div className="flex items-center space-x-1 text-sm text-gray-500 dark:text-gray-400">
                  <Link2 className="h-4 w-4" />
                  <span>{entity.relationship_count}</span>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {isEditing ? (
        <textarea
          value={editDetails}
          onChange={(e) => setEditDetails(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          placeholder="Add details..."
          rows={2}
          className="mt-3 w-full px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
        />
      ) : (
        entity.details && (
          <p className="mt-3 text-sm text-gray-600 dark:text-gray-400 line-clamp-2">
            {entity.details}
          </p>
        )
      )}

      {/* Event-specific: show time info */}
      {entity.type === 'event' && entity.start_time && (
        <div className="mt-3 flex items-center space-x-2 text-sm text-gray-500 dark:text-gray-400">
          <Calendar className="h-4 w-4" />
          <span>
            {formatDate(entity.start_time)}
            {entity.end_time && ` - ${formatDate(entity.end_time)}`}
          </span>
        </div>
      )}

      {entity.created_at && !isEditing && (
        <div className="mt-2 text-xs text-gray-400 dark:text-gray-500">
          Added {formatDate(entity.created_at)}
        </div>
      )}
    </div>
  )
}
