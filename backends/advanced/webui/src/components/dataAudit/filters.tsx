/**
 * Declarative filter registry for the Data Audit page.
 *
 * Each filter is one self-contained definition: default value, active-test,
 * chip label, query params, and a popover editor. The filter bar renders
 * whatever is in AUDIT_FILTERS — adding a new filter is one entry here plus
 * (if server-side) one query param in the backend list endpoint.
 */
import { ComponentType, useState } from 'react'
import {
  Ban,
  CalendarRange,
  CheckCheck,
  Clock,
  FileArchive,
  LucideIcon,
  Mic,
  PackageOpen,
  Search,
  Users,
} from 'lucide-react'
import { formatDuration } from './format'

export type SpeakerFilterState = 'include' | 'exclude'

export interface FilterContext {
  speakers: string[]
  datasets: string[]
}

export interface EditorProps<V> {
  value: V
  onChange: (v: V) => void
  ctx: FilterContext
}

export interface FilterDef<V = any> {
  key: string
  label: string
  icon: LucideIcon
  defaultValue: V
  isActive: (v: V) => boolean
  chipLabel: (v: V) => string
  /** Query params merged into dataAuditApi.getConversations. */
  toParams: (v: V) => Record<string, unknown>
  /**
   * Single-click toggle filter (a boolean): no popover/editor — picking it from
   * the menu turns it on, clicking the active chip turns it off. Omit `Editor`.
   */
  toggle?: boolean
  Editor?: ComponentType<EditorProps<V>>
}

const inputCls =
  'w-24 px-2 py-1 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm text-gray-700 dark:text-gray-200'

function NumberField({
  label,
  value,
  onChange,
  placeholder,
  min,
  max,
  step,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  placeholder: string
  min: number
  max?: number
  step?: number
}) {
  return (
    <label className="flex items-center justify-between space-x-3 text-sm text-gray-700 dark:text-gray-200">
      <span>{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        step={step ?? 1}
        // 0 means "unbounded" for these filters; show as empty
        value={value === 0 ? '' : value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value === '' ? 0 : Number(e.target.value))}
        className={inputCls}
      />
    </label>
  )
}

// ---------------------------------------------------------------------------
// Speech %
// ---------------------------------------------------------------------------

interface SpeechValue {
  min: number // percent, 0 = off
  max: number // percent, 100 = off
  threshold: number // VAD frame probability
}

const speechFilter: FilterDef<SpeechValue> = {
  key: 'speech',
  label: 'Speech %',
  icon: Mic,
  defaultValue: { min: 0, max: 100, threshold: 0.5 },
  isActive: (v) => v.min > 0 || v.max < 100 || v.threshold !== 0.5,
  chipLabel: (v) => {
    let label = 'Speech'
    if (v.min > 0 && v.max < 100) label += ` ${v.min}–${v.max}%`
    else if (v.min > 0) label += ` ≥ ${v.min}%`
    else if (v.max < 100) label += ` ≤ ${v.max}%`
    else label += ' %'
    if (v.threshold !== 0.5) label += ` · VAD ${v.threshold.toFixed(2)}`
    return label
  },
  toParams: (v) => ({
    speech_threshold: v.threshold,
    min_speech_fraction: v.min / 100,
    max_speech_fraction: v.max / 100,
  }),
  Editor: ({ value, onChange }) => (
    <div className="space-y-3 w-60">
      <NumberField
        label="Min speech %"
        value={value.min}
        onChange={(min) => onChange({ ...value, min: Math.min(100, Math.max(0, min)) })}
        placeholder="any"
        min={0}
        max={100}
      />
      <label className="flex items-center justify-between space-x-3 text-sm text-gray-700 dark:text-gray-200">
        <span>Max speech %</span>
        <input
          type="number"
          min={0}
          max={100}
          value={value.max === 100 ? '' : value.max}
          placeholder="any"
          onChange={(e) =>
            onChange({
              ...value,
              max: e.target.value === '' ? 100 : Math.min(100, Math.max(0, Number(e.target.value))),
            })
          }
          className={inputCls}
        />
      </label>
      <div className="pt-2 border-t border-gray-200 dark:border-gray-700">
        <label className="flex justify-between text-xs font-medium text-gray-700 dark:text-gray-200">
          <span>VAD threshold</span>
          <span className="text-gray-500 dark:text-gray-400">{value.threshold.toFixed(2)}</span>
        </label>
        <input
          type="range"
          min={0.1}
          max={0.9}
          step={0.05}
          value={value.threshold}
          onChange={(e) => onChange({ ...value, threshold: Number(e.target.value) })}
          className="w-full mt-1"
        />
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          Frame probability that counts as speech.
        </p>
      </div>
      <p className="text-[11px] text-gray-500 dark:text-gray-400">
        Unanalyzed conversations are hidden while a speech bound is set.
      </p>
    </div>
  ),
}

// ---------------------------------------------------------------------------
// Duration
// ---------------------------------------------------------------------------

interface DurationValue {
  min: number // seconds, 0 = off
  max: number // seconds, 0 = off
}

const durationFilter: FilterDef<DurationValue> = {
  key: 'duration',
  label: 'Duration',
  icon: Clock,
  defaultValue: { min: 0, max: 0 },
  isActive: (v) => v.min > 0 || v.max > 0,
  chipLabel: (v) => {
    if (v.min > 0 && v.max > 0)
      return `Duration ${formatDuration(v.min)}–${formatDuration(v.max)}`
    if (v.min > 0) return `Duration ≥ ${formatDuration(v.min)}`
    if (v.max > 0) return `Duration ≤ ${formatDuration(v.max)}`
    return 'Duration'
  },
  toParams: (v) => ({ min_duration: v.min, max_duration: v.max }),
  Editor: ({ value, onChange }) => (
    <div className="space-y-3 w-60">
      <NumberField
        label="Min (seconds)"
        value={value.min}
        onChange={(min) => onChange({ ...value, min: Math.max(0, min) })}
        placeholder="any"
        min={0}
        step={5}
      />
      <NumberField
        label="Max (seconds)"
        value={value.max}
        onChange={(max) => onChange({ ...value, max: Math.max(0, max) })}
        placeholder="any"
        min={0}
        step={5}
      />
      {(value.min > 0 || value.max > 0) && (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          {value.min > 0 && `min ${formatDuration(value.min)}`}
          {value.min > 0 && value.max > 0 && ' · '}
          {value.max > 0 && `max ${formatDuration(value.max)}`}
        </p>
      )}
    </div>
  ),
}

// ---------------------------------------------------------------------------
// Speakers (tri-state include/exclude)
// ---------------------------------------------------------------------------

type SpeakersValue = Record<string, SpeakerFilterState>
export const UNKNOWN_SPEAKERS_FILTER_KEY = '__unknown_speakers__'

export const speakersFilter: FilterDef<SpeakersValue> = {
  key: 'speakers',
  label: 'Speakers',
  icon: Users,
  defaultValue: {},
  isActive: (v) => Object.keys(v).length > 0,
  chipLabel: (v) => {
    const inc = Object.values(v).filter((s) => s === 'include').length
    const exc = Object.values(v).filter((s) => s === 'exclude').length
    const parts: string[] = []
    if (inc) parts.push(`+${inc}`)
    if (exc) parts.push(`−${exc}`)
    return `Speakers ${parts.join(' ')}`
  },
  toParams: (v) => ({
    include_speakers: Object.entries(v)
      .filter(([k, s]) => k !== UNKNOWN_SPEAKERS_FILTER_KEY && s === 'include')
      .map(([k]) => k),
    exclude_speakers: Object.entries(v)
      .filter(([k, s]) => k !== UNKNOWN_SPEAKERS_FILTER_KEY && s === 'exclude')
      .map(([k]) => k),
    unknown_speakers: v[UNKNOWN_SPEAKERS_FILTER_KEY],
  }),
  Editor: ({ value, onChange, ctx }) => {
    const [query, setQuery] = useState('')
    // Cycle: neutral → include → exclude → neutral
    const cycle = (s: string) => {
      const next = { ...value }
      const current = value[s]
      if (!current) next[s] = 'include'
      else if (current === 'include') next[s] = 'exclude'
      else delete next[s]
      onChange(next)
    }

    // Empty search shows the full speaker list; typing narrows it.
    const q = query.trim().toLowerCase()
    const visible = q
      ? ctx.speakers.filter((s) => s.toLowerCase().includes(q))
      : ctx.speakers
    const choices = [
      ...(q && !'unknown speakers'.includes(q) ? [] : [UNKNOWN_SPEAKERS_FILTER_KEY]),
      ...visible,
    ]

    return (
      <div className="space-y-2 w-72">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search speakers…"
            autoFocus
            className="w-full pl-7 pr-2 py-1.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm text-gray-700 dark:text-gray-200"
          />
        </div>
        <div className="flex items-center justify-between">
          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            Cycle: <span className="text-blue-600 dark:text-blue-400">include</span> →{' '}
            <span className="text-red-600 dark:text-red-400">exclude</span> → off
          </p>
          {Object.keys(value).length > 0 && (
            <button
              onClick={() => onChange({})}
              className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            >
              Clear
            </button>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5 max-h-44 overflow-y-auto">
          {choices.length === 0 && (
            <span className="text-xs text-gray-400">
              {q ? 'No speakers match.' : 'No speaker labels found.'}
            </span>
          )}
          {choices.map((s) => {
            const state = value[s]
            const label = s === UNKNOWN_SPEAKERS_FILTER_KEY ? 'Unknown speakers' : s
            return (
              <button
                key={s}
                onClick={() => cycle(s)}
                className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                  state === 'include'
                    ? 'bg-blue-100 border-blue-400 text-blue-700 dark:bg-blue-900 dark:text-blue-100 dark:border-blue-600'
                    : state === 'exclude'
                      ? 'bg-red-100 border-red-400 text-red-700 line-through dark:bg-red-900/40 dark:text-red-200 dark:border-red-600'
                      : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
                }`}
              >
                {label}
              </button>
            )
          })}
        </div>
      </div>
    )
  },
}

// ---------------------------------------------------------------------------
// Date range
// ---------------------------------------------------------------------------

interface DateValue {
  after: string // datetime-local value, '' = off
  before: string
}

function shortDate(v: string): string {
  return new Date(v).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const dateFilter: FilterDef<DateValue> = {
  key: 'date',
  label: 'Date',
  icon: CalendarRange,
  defaultValue: { after: '', before: '' },
  isActive: (v) => v.after !== '' || v.before !== '',
  chipLabel: (v) => {
    if (v.after && v.before) return `${shortDate(v.after)} – ${shortDate(v.before)}`
    if (v.after) return `After ${shortDate(v.after)}`
    if (v.before) return `Before ${shortDate(v.before)}`
    return 'Date'
  },
  toParams: (v) => ({
    created_after: v.after ? new Date(v.after).toISOString() : undefined,
    created_before: v.before ? new Date(v.before).toISOString() : undefined,
  }),
  Editor: ({ value, onChange }) => (
    <div className="space-y-3 w-64">
      {(['after', 'before'] as const).map((k) => (
        <label
          key={k}
          className="flex items-center justify-between space-x-3 text-sm text-gray-700 dark:text-gray-200"
        >
          <span className="capitalize">{k}</span>
          <input
            type="datetime-local"
            value={value[k]}
            onChange={(e) => onChange({ ...value, [k]: e.target.value })}
            className="px-2 py-1 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm text-gray-700 dark:text-gray-200"
          />
        </label>
      ))}
    </div>
  ),
}

// ---------------------------------------------------------------------------
// Imported annotation dataset
// ---------------------------------------------------------------------------

const datasetFilter: FilterDef<string> = {
  key: 'dataset',
  label: 'Dataset',
  icon: FileArchive,
  defaultValue: '',
  isActive: (v) => v.trim() !== '',
  chipLabel: (v) => `Dataset: ${v}`,
  toParams: (v) => ({ dataset_id: v.trim() || undefined }),
  Editor: ({ value, onChange, ctx }) => (
    <div className="w-[min(20rem,calc(100vw-3rem))] space-y-2">
      <label className="block text-xs font-medium text-gray-600 dark:text-gray-300">
        Annotation dataset
      </label>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-700 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
      >
        <option value="">All datasets</option>
        {!ctx.datasets.includes(value) && value && <option value={value}>{value}</option>}
        {ctx.datasets.map((dataset) => (
          <option key={dataset} value={dataset}>
            {dataset}
          </option>
        ))}
      </select>
    </div>
  ),
}

// ---------------------------------------------------------------------------
// Export history (from the on-disk annotation-export metadata)
// ---------------------------------------------------------------------------

type ExportedValue = '' | 'never' | 'exported'

const exportedFilter: FilterDef<ExportedValue> = {
  key: 'exported',
  label: 'Export history',
  icon: PackageOpen,
  defaultValue: '',
  isActive: (v) => v !== '',
  chipLabel: (v) => (v === 'never' ? 'Not yet exported' : 'Previously exported'),
  toParams: (v) => ({ exported: v || undefined }),
  Editor: ({ value, onChange }) => (
    <div className="w-56 space-y-1">
      {(
        [
          { key: '', label: 'All conversations' },
          { key: 'never', label: 'Not yet exported' },
          { key: 'exported', label: 'Previously exported' },
        ] as const
      ).map((opt) => (
        <label
          key={opt.key}
          className="flex items-center space-x-2 text-sm text-gray-700 dark:text-gray-200 cursor-pointer"
        >
          <input
            type="radio"
            name="exported-filter"
            checked={value === opt.key}
            onChange={() => onChange(opt.key)}
          />
          <span>{opt.label}</span>
        </label>
      ))}
      <p className="pt-1 text-[11px] text-gray-500 dark:text-gray-400">
        Whether a previous annotation export shipped the conversation.
      </p>
    </div>
  ),
}

// ---------------------------------------------------------------------------
// Hide failed (processing_status == 'failed')
// ---------------------------------------------------------------------------

const hideFailedFilter: FilterDef<boolean> = {
  key: 'hideFailed',
  label: 'Hide failed',
  icon: Ban,
  defaultValue: false,
  isActive: (v) => v === true,
  chipLabel: () => 'Hiding failed',
  toParams: (v) => (v ? { hide_failed: true } : {}),
  toggle: true,
}

// ---------------------------------------------------------------------------
// Hide reviewed (no unidentified speech segments left to triage)
// ---------------------------------------------------------------------------

const hideReviewedFilter: FilterDef<boolean> = {
  key: 'hideReviewed',
  label: 'Needs review',
  icon: CheckCheck,
  defaultValue: false,
  isActive: (v) => v === true,
  chipLabel: () => 'Needs review only',
  toParams: (v) => (v ? { hide_reviewed: true } : {}),
  toggle: true,
}

export const AUDIT_FILTERS: FilterDef[] = [
  speechFilter,
  durationFilter,
  speakersFilter,
  dateFilter,
  datasetFilter,
  exportedFilter,
  hideFailedFilter,
  hideReviewedFilter,
]

export function defaultFilterValues(): Record<string, unknown> {
  return Object.fromEntries(AUDIT_FILTERS.map((f) => [f.key, f.defaultValue]))
}
