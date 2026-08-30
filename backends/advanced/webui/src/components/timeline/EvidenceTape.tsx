import { KeyboardEvent, PointerEvent, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Film, MessageSquare, Moon, ScanLine } from 'lucide-react'
import { DayReviewGroup, DayReviewProjection, TimelineEpisode } from '../../services/api'
import { episodeDisplayTitle, isMediaKind } from './episodePresentation'

export type EvidenceLens = 'all' | 'media' | 'conversation' | 'foreground' | 'background' | 'coverage'

export interface TapeCoverageInterval {
  started_at: string
  ended_at: string
  kind: 'no_capture' | 'unexplained' | 'unreconciled'
  label: string
}

type TapeInterval = {
  key: string
  group: DayReviewGroup
  episode: TimelineEpisode
  startedAt: number
  endedAt: number
  occurrence: number
}

const lensOptions: Array<{ value: EvidenceLens; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'media', label: 'Media' },
  { value: 'conversation', label: 'Conversation' },
  { value: 'foreground', label: 'Foreground' },
  { value: 'background', label: 'Background' },
  { value: 'coverage', label: 'Coverage' },
]

const laneLabel: Record<DayReviewGroup['lane'], string> = {
  conversation: 'Conversation',
  foreground: 'Activity',
  background: 'Background',
}

function clock(value: string | number) {
  return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function duration(startedAt: number, endedAt: number) {
  const minutes = Math.max(1, Math.round((endedAt - startedAt) / 60_000))
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

export function evidenceChannel(episode: Pick<TimelineEpisode, 'kind' | 'activity_mode'>, lane: DayReviewGroup['lane']) {
  if (isMediaKind(episode.kind)) return 'media' as const
  if (lane === 'conversation') return 'conversation' as const
  if (lane === 'background' || ['background', 'ambient', 'idle'].includes(episode.activity_mode)) return 'background' as const
  return 'foreground' as const
}

function channelColor(channel: ReturnType<typeof evidenceChannel>) {
  if (channel === 'media') return 'var(--tape-media)'
  if (channel === 'conversation') return 'var(--tape-conversation)'
  if (channel === 'background') return 'var(--tape-background)'
  return 'var(--tape-activity)'
}

function matchesLens(interval: TapeInterval, lens: EvidenceLens) {
  if (lens === 'all') return true
  if (lens === 'coverage') return false
  if (lens === 'media') return isMediaKind(interval.episode.kind)
  return interval.group.lane === lens
}

function intervalLabel(interval: TapeInterval) {
  const channel = evidenceChannel(interval.episode, interval.group.lane)
  const prefix = channel === 'media' ? 'Media' : laneLabel[interval.group.lane]
  return `${prefix}: ${episodeDisplayTitle(interval.episode)}, ${clock(interval.startedAt)} to ${clock(interval.endedAt)}`
}

function clippedGeometry(startedAt: number, endedAt: number, bandStart: number, bandEnd: number) {
  const clippedStart = Math.max(startedAt, bandStart)
  const clippedEnd = Math.min(endedAt, bandEnd)
  if (clippedEnd <= clippedStart) return null
  const width = bandEnd - bandStart
  return {
    left: ((clippedStart - bandStart) / width) * 100,
    span: Math.max(0.45, ((clippedEnd - clippedStart) / width) * 100),
  }
}

function TapePreview({ interval, coverage }: { interval: TapeInterval | null; coverage: TapeCoverageInterval | null }) {
  if (coverage) {
    return (
      <div data-testid="tape-preview" className="flex h-[7.25rem] items-start gap-3 overflow-hidden rounded-lg border border-dashed border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 py-2.5 sm:h-[5.5rem]">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" />
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">Coverage</p>
          <p className="mt-1 text-sm font-semibold text-gray-900 dark:text-gray-100">{coverage.label}</p>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{clock(coverage.started_at)}–{clock(coverage.ended_at)}</p>
        </div>
      </div>
    )
  }
  if (!interval) {
    return (
      <div data-testid="tape-preview" className="flex h-[7.25rem] items-center gap-3 overflow-hidden rounded-lg border border-dashed border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 py-2.5 text-sm text-gray-500 dark:text-gray-400 sm:h-[5.5rem]">
        <ScanLine className="h-4 w-4" />
        Move across the tape or focus an interval to inspect the day without opening it.
      </div>
    )
  }
  const channel = evidenceChannel(interval.episode, interval.group.lane)
  const Icon = channel === 'media' ? Film : interval.group.lane === 'conversation' ? MessageSquare : interval.group.lane === 'background' ? Moon : ScanLine
  return (
    <div data-testid="tape-preview" className="flex h-[7.25rem] items-start gap-3 overflow-hidden rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 py-2.5 sm:h-[5.5rem]">
      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-white" style={{ backgroundColor: channelColor(channel) }}>
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <p className="truncate text-sm font-semibold text-gray-900 dark:text-gray-100">{episodeDisplayTitle(interval.episode)}</p>
          <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-gray-500 dark:text-gray-400">{channel === 'media' ? 'Media' : laneLabel[interval.group.lane]}</span>
        </div>
        <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          {clock(interval.startedAt)}–{clock(interval.endedAt)} · {duration(interval.startedAt, interval.endedAt)} · {interval.episode.status} · {Math.round(interval.episode.confidence * 100)}% confidence
        </p>
        <p className="mt-1 line-clamp-1 text-xs text-gray-600 dark:text-gray-300">{interval.episode.summary || 'No summary recorded for this episode.'}</p>
      </div>
      {interval.group.needs_attention && <AlertTriangle className="h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" aria-label="Needs attention" />}
    </div>
  )
}

export default function EvidenceTape({
  projection,
  episodes,
  coverage,
  lens,
  selectedEpisodeId,
  previewEpisodeIds = new Set<string>(),
  onLensChange,
  onSelectEpisode,
}: {
  projection: DayReviewProjection
  episodes: TimelineEpisode[]
  coverage: TapeCoverageInterval[]
  lens: EvidenceLens
  selectedEpisodeId: string | null
  previewEpisodeIds?: ReadonlySet<string>
  onLensChange: (lens: EvidenceLens) => void
  onSelectEpisode: (episodeId: string) => void
}) {
  const episodeMap = useMemo(() => new Map(episodes.map(episode => [episode.episode_id, episode])), [episodes])
  const intervals = useMemo(() => projection.groups.flatMap(group => group.intervals.flatMap((interval, occurrence) => {
    const episode = episodeMap.get(interval.episode_id)
    if (!episode) return []
    return [{
      key: `${group.group_id}:${interval.episode_id}:${interval.started_at}:${interval.ended_at}:${occurrence}`,
      group,
      episode,
      startedAt: Date.parse(interval.started_at),
      endedAt: Date.parse(interval.ended_at),
      occurrence,
    } satisfies TapeInterval]
  })), [episodeMap, projection.groups])
  const [previewKey, setPreviewKey] = useState<string | null>(null)
  const [previewCoverageIndex, setPreviewCoverageIndex] = useState<number | null>(null)
  const selected = intervals.find(interval => interval.episode.episode_id === selectedEpisodeId) || null
  const preview = intervals.find(interval => interval.key === previewKey) || selected
  const coveragePreview = previewCoverageIndex == null ? null : coverage[previewCoverageIndex] || null
  const dayStart = Date.parse(projection.day_started_at)
  const dayEnd = Date.parse(projection.day_ended_at)
  const dayLength = dayEnd - dayStart

  useEffect(() => {
    setPreviewKey(null)
    setPreviewCoverageIndex(null)
  }, [projection.day_started_at])

  const uniqueCount = (predicate: (interval: TapeInterval) => boolean) => new Set(intervals.filter(predicate).map(interval => interval.episode.episode_id)).size
  const counts: Record<EvidenceLens, number> = {
    all: uniqueCount(() => true),
    media: uniqueCount(interval => isMediaKind(interval.episode.kind)),
    conversation: uniqueCount(interval => interval.group.lane === 'conversation'),
    foreground: uniqueCount(interval => interval.group.lane === 'foreground'),
    background: uniqueCount(interval => interval.group.lane === 'background'),
    coverage: coverage.length,
  }

  const focusSibling = (event: KeyboardEvent<HTMLButtonElement>, interval: TapeInterval) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    const ordered = [...intervals].sort((a, b) => a.startedAt - b.startedAt || a.endedAt - b.endedAt)
    const current = ordered.findIndex(item => item.key === interval.key)
    const next = ordered[event.key === 'ArrowRight' ? Math.min(ordered.length - 1, current + 1) : Math.max(0, current - 1)]
    if (!next) return
    setPreviewKey(next.key)
    document.querySelector<HTMLButtonElement>(`[data-tape-key="${CSS.escape(next.key)}"]`)?.focus()
  }

  const renderBand = (startFraction: number, endFraction: number, mobile: boolean) => {
    const bandStart = dayStart + dayLength * startFraction
    const bandEnd = dayStart + dayLength * endFraction
    const bandIntervals = intervals.filter(interval => interval.endedAt > bandStart && interval.startedAt < bandEnd)
    const bandCoverage = coverage.map((item, index) => ({ item, index, start: Date.parse(item.started_at), end: Date.parse(item.ended_at) }))
      .filter(item => item.end > bandStart && item.start < bandEnd)
    const selectAtPointer = (event: PointerEvent<HTMLDivElement>, lane: DayReviewGroup['lane']) => {
      const bounds = event.currentTarget.getBoundingClientRect()
      if (!bounds.width) return
      const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width))
      const at = bandStart + ratio * (bandEnd - bandStart)
      const candidate = bandIntervals
        .filter(interval => interval.group.lane === lane && interval.startedAt <= at && interval.endedAt >= at)
        .sort((a, b) => (a.endedAt - a.startedAt) - (b.endedAt - b.startedAt))[0]
      if (candidate) {
        setPreviewCoverageIndex(null)
        setPreviewKey(candidate.key)
      }
    }
    return (
      <div key={`${startFraction}-${endFraction}`} className={mobile ? 'sm:hidden' : 'hidden sm:block'}>
        <div className="mb-1 flex items-center justify-between pl-[5.4rem] font-mono text-[10px] text-gray-400">
          <span>{String(Math.round(startFraction * 24)).padStart(2, '0')}:00</span>
          <span>{String(Math.round(((startFraction + endFraction) / 2) * 24)).padStart(2, '0')}:00</span>
          <span>{String(Math.round(endFraction * 24)).padStart(2, '0')}:00</span>
        </div>
        <div className="space-y-1">
          {(['conversation', 'foreground', 'background'] as const).map(lane => (
            <div key={lane} className="grid grid-cols-[5rem_1fr] items-center gap-1.5">
              <span className="truncate text-right text-[10px] font-semibold uppercase tracking-[0.1em] text-gray-400">{laneLabel[lane]}</span>
              <div
                className="relative h-7 overflow-hidden rounded-sm border border-[var(--tape-line)] bg-[var(--tape-track)]"
                onPointerMove={event => selectAtPointer(event, lane)}
              >
                <span className="pointer-events-none absolute inset-y-0 left-1/4 border-l border-[var(--tape-grid)]" />
                <span className="pointer-events-none absolute inset-y-0 left-1/2 border-l border-[var(--tape-grid)]" />
                <span className="pointer-events-none absolute inset-y-0 left-3/4 border-l border-[var(--tape-grid)]" />
                {bandIntervals.filter(interval => interval.group.lane === lane).map(interval => {
                  const geometry = clippedGeometry(interval.startedAt, interval.endedAt, bandStart, bandEnd)
                  if (!geometry) return null
                  const channel = evidenceChannel(interval.episode, lane)
                  const visible = matchesLens(interval, lens)
                  const previewingSuggestion = previewEpisodeIds.size > 0
                  const suggestionMember = previewEpisodeIds.has(interval.episode.episode_id)
                  const active = interval.episode.episode_id === selectedEpisodeId || interval.key === previewKey || suggestionMember
                  const opacity = visible && lens !== 'coverage'
                    ? previewingSuggestion && !suggestionMember ? 'opacity-20' : 'opacity-100'
                    : 'opacity-15'
                  return (
                    <button
                      key={`${mobile ? 'mobile' : 'desktop'}:${startFraction}:${interval.key}`}
                      type="button"
                      data-tape-key={interval.key}
                      data-suggestion-preview={previewingSuggestion ? suggestionMember ? 'included' : 'excluded' : undefined}
                      aria-label={intervalLabel(interval)}
                      aria-current={suggestionMember ? 'true' : undefined}
                      onFocus={() => { setPreviewCoverageIndex(null); setPreviewKey(interval.key) }}
                      onPointerEnter={() => { setPreviewCoverageIndex(null); setPreviewKey(interval.key) }}
                      onPointerMove={event => event.stopPropagation()}
                      onClick={() => onSelectEpisode(interval.episode.episode_id)}
                      onKeyDown={event => focusSibling(event, interval)}
                      className={`absolute inset-y-1 rounded-[3px] outline-none transition-[opacity,box-shadow] duration-150 focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)] focus-visible:ring-offset-1 ${active ? 'z-10 ring-2 ring-[var(--tape-focus)] ring-offset-1' : ''} ${opacity}`}
                      style={{ left: `${geometry.left}%`, width: `${geometry.span}%`, backgroundColor: channelColor(channel) }}
                    />
                  )
                })}
              </div>
            </div>
          ))}
          <div className="grid grid-cols-[5rem_1fr] items-center gap-1.5">
            <span className="truncate text-right text-[10px] font-semibold uppercase tracking-[0.1em] text-gray-400">Coverage</span>
            <div className="relative h-5 overflow-hidden rounded-sm border border-[var(--tape-line)] bg-[var(--tape-track)]">
              {bandCoverage.map(({ item, index, start, end }) => {
                const geometry = clippedGeometry(start, end, bandStart, bandEnd)
                if (!geometry) return null
                return (
                  <button
                    key={`${mobile ? 'mobile' : 'desktop'}:${startFraction}:coverage:${index}`}
                    type="button"
                    aria-label={`${item.label}, ${clock(item.started_at)} to ${clock(item.ended_at)}`}
                    onFocus={() => { setPreviewKey(null); setPreviewCoverageIndex(index) }}
                    onPointerEnter={() => { setPreviewKey(null); setPreviewCoverageIndex(index) }}
                    className={`absolute inset-y-0.5 rounded-[2px] border ${item.kind === 'no_capture' ? 'border-gray-400 tape-hatch-neutral' : 'border-amber-600 tape-hatch-attention'} ${lens === 'all' || lens === 'coverage' ? 'opacity-100' : 'opacity-20'}`}
                    style={{ left: `${geometry.left}%`, width: `${geometry.span}%` }}
                  />
                )
              })}
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <section className="rounded-xl border border-[var(--tape-line)] bg-[var(--tape-paper)] p-3 shadow-[0_1px_0_rgba(69,52,35,0.04)] sm:p-4" aria-label="Evidence tape">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Evidence tape</h3>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">Duration-true intervals across the day. Position shows context; color and labels show what kind.</p>
        </div>
        <div className="flex flex-wrap gap-1" aria-label="Filter evidence tape">
          {lensOptions.map(option => (
            <button
              key={option.value}
              type="button"
              aria-pressed={lens === option.value}
              onClick={() => onLensChange(lens === option.value && option.value !== 'all' ? 'all' : option.value)}
              className={`min-h-8 rounded-md px-2.5 text-xs font-semibold outline-none transition-colors focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)] ${lens === option.value ? 'bg-[var(--tape-ink)] text-[var(--tape-paper)]' : 'bg-[var(--tape-chip)] text-gray-600 hover:bg-[var(--tape-chip-hover)] dark:text-gray-300'}`}
            >
              {option.label} {counts[option.value]}
            </button>
          ))}
        </div>
      </div>
      <div className="space-y-3" onPointerLeave={() => { setPreviewKey(null); setPreviewCoverageIndex(null) }}>
        {renderBand(0, 1, false)}
        <div className="space-y-3 sm:hidden">
          {[0, 0.25, 0.5, 0.75].map(start => renderBand(start, start + 0.25, true))}
        </div>
        <TapePreview interval={preview} coverage={coveragePreview} />
      </div>
    </section>
  )
}
