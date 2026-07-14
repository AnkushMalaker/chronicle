import { useEffect, useRef, useState } from 'react'
import { Plus, RefreshCw, X } from 'lucide-react'
import { AUDIT_FILTERS, FilterContext } from './filters'

interface Props {
  filters: Record<string, unknown>
  onChangeFilter: (key: string, value: unknown) => void
  onResetFilter: (key: string) => void
  /** Set a toggle filter and refetch synchronously (single-click on/off). */
  onToggleFilter: (key: string, value: unknown) => void
  /** Called when an edit is committed (popover closed / chip removed). */
  onApply: () => void
  ctx: FilterContext
  loading: boolean
}

/**
 * Generic chip-based filter bar driven by the AUDIT_FILTERS registry:
 * "＋ Filter" opens a menu of inactive filters; each active filter renders as
 * a chip whose popover hosts the filter's own editor. Changes auto-apply when
 * a popover closes. Adding a new filter = one registry entry in filters.tsx.
 */
export default function AuditFilterBar({
  filters,
  onChangeFilter,
  onResetFilter,
  onToggleFilter,
  onApply,
  ctx,
  loading,
}: Props) {
  // Which filter's editor popover is open ('+' menu uses the 'add' sentinel).
  const [openKey, setOpenKey] = useState<string | null>(null)
  const barRef = useRef<HTMLDivElement | null>(null)
  // Track whether the open popover had edits, to skip no-op refetches.
  const dirtyRef = useRef(false)

  const close = () => {
    if (openKey && openKey !== 'add' && dirtyRef.current) onApply()
    dirtyRef.current = false
    setOpenKey(null)
  }

  // Re-bound on every openKey/onApply change so close() sees current state.
  useEffect(() => {
    const onClickOutside = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) close()
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openKey, onApply])

  const active = AUDIT_FILTERS.filter(
    (def) => def.isActive(filters[def.key] ?? def.defaultValue) || def.key === openKey
  )
  const inactive = AUDIT_FILTERS.filter((def) => !active.includes(def))

  return (
    <div
      ref={barRef}
      className="flex flex-wrap items-center gap-2 bg-gray-50 dark:bg-gray-900/40 rounded-lg p-3 border border-gray-200 dark:border-gray-700"
    >
      {active.map((def) => {
        const value = filters[def.key] ?? def.defaultValue
        const Icon = def.icon
        // Toggle filters are a single-click chip: clicking it turns the filter
        // off (active chips are always "on"). No popover, no separate X.
        if (def.toggle) {
          return (
            <button
              key={def.key}
              onClick={() => onToggleFilter(def.key, def.defaultValue)}
              className="flex items-center space-x-1.5 rounded-full border text-sm pl-3 pr-2.5 py-1.5 border-blue-400 bg-blue-50 text-blue-700 dark:bg-blue-900/40 dark:text-blue-200 dark:border-blue-600 transition-colors"
              title={`Turn off ${def.label.toLowerCase()} filter`}
            >
              <Icon className="h-3.5 w-3.5" />
              <span>{def.chipLabel(value)}</span>
              <X className="h-3.5 w-3.5 opacity-70" />
            </button>
          )
        }
        return (
          <div key={def.key} className="relative">
            <div
              className={`flex items-center rounded-full border text-sm transition-colors ${
                openKey === def.key
                  ? 'border-blue-400 bg-blue-50 text-blue-700 dark:bg-blue-900/40 dark:text-blue-200 dark:border-blue-600'
                  : 'border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200'
              }`}
            >
              <button
                onClick={() => (openKey === def.key ? close() : setOpenKey(def.key))}
                className="flex items-center space-x-1.5 pl-3 pr-1.5 py-1.5"
                title={`Edit ${def.label.toLowerCase()} filter`}
              >
                <Icon className="h-3.5 w-3.5" />
                <span>{def.chipLabel(value)}</span>
              </button>
              <button
                onClick={() => {
                  // onResetFilter applies the refetch itself (it knows the
                  // next filter state synchronously; we don't).
                  onResetFilter(def.key)
                  if (openKey === def.key) setOpenKey(null)
                  dirtyRef.current = false
                }}
                className="pr-2 pl-0.5 py-1.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                title="Remove filter"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {openKey === def.key && def.Editor && (
              <div className="absolute z-20 mt-1 p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-lg">
                <def.Editor
                  value={value}
                  onChange={(v) => {
                    dirtyRef.current = true
                    onChangeFilter(def.key, v)
                  }}
                  ctx={ctx}
                />
              </div>
            )}
          </div>
        )
      })}

      {/* Add-filter menu */}
      {inactive.length > 0 && (
        <div className="relative">
          <button
            onClick={() => setOpenKey(openKey === 'add' ? null : 'add')}
            className="flex items-center space-x-1 px-3 py-1.5 rounded-full border border-dashed border-gray-300 dark:border-gray-600 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-400 transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>Filter</span>
          </button>
          {openKey === 'add' && (
            <div className="absolute z-20 mt-1 w-44 py-1 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-lg">
              {inactive.map((def) => {
                const Icon = def.icon
                return (
                  <button
                    key={def.key}
                    onClick={() => {
                      if (def.toggle) {
                        // Single-click: activate and refetch, no popover.
                        onToggleFilter(def.key, true)
                        setOpenKey(null)
                      } else {
                        setOpenKey(def.key)
                      }
                    }}
                    className="flex items-center space-x-2 w-full px-3 py-1.5 text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700"
                  >
                    <Icon className="h-3.5 w-3.5 text-gray-400" />
                    <span>{def.label}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}

      <div className="flex-1" />
      <button
        onClick={onApply}
        disabled={loading}
        className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 transition-colors"
        title="Refresh results"
      >
        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        <span>Refresh</span>
      </button>
    </div>
  )
}
