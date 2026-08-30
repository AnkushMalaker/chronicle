import { ReactNode, useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Combine,
  Eye, EyeOff, Film, Loader2, MessageSquare, Moon, Pencil, ScanLine, Sparkles,
} from 'lucide-react'
import {
  DayReviewGroup, DayReviewProjection, TimelineConsolidationProposal,
  TimelineEpisode, timelineApi,
} from '../../services/api'
import { Button } from '../ui'
import EvidenceTape, { EvidenceLens, TapeCoverageInterval, evidenceChannel } from './EvidenceTape'
import { episodeDisplayTitle, isMediaKind } from './episodePresentation'

const reasonLabels: Record<string, string> = {
  low_confidence: 'low confidence',
  missing_evidence: 'missing evidence',
  missing_audio: 'missing audio',
  long_episode: 'unusually long',
}

function clock(value: string) {
  return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function duration(seconds: number) {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`
  const minutes = Math.max(1, Math.round(seconds / 60))
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function kindLabel(kind: string) {
  const words = kind.split('_').join(' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function mostCommonKind(episodes: TimelineEpisode[]) {
  const counts = new Map<string, number>()
  for (const episode of episodes) counts.set(episode.kind, (counts.get(episode.kind) || 0) + 1)
  return [...counts.entries()].sort((left, right) => right[1] - left[1])[0]?.[0] || 'activity'
}

function groupMatchesLens(group: DayReviewGroup, episodes: TimelineEpisode[], lens: EvidenceLens) {
  if (lens === 'all') return true
  if (lens === 'coverage') return false
  if (lens === 'media') return episodes.some(episode => isMediaKind(episode.kind))
  return group.lane === lens
}

function CompactEpisodeRow({ episode, group, selected }: {
  episode: TimelineEpisode
  group: DayReviewGroup
  selected: boolean
}) {
  const channel = evidenceChannel(episode, group.lane)
  const Icon = channel === 'media' ? Film : group.lane === 'conversation' ? MessageSquare : group.lane === 'background' ? Moon : ScanLine
  const color = channel === 'media'
    ? 'var(--tape-media)'
    : channel === 'conversation'
      ? 'var(--tape-conversation)'
      : channel === 'background'
        ? 'var(--tape-background)'
        : 'var(--tape-activity)'
  return (
    <article
      data-episode-id={episode.episode_id}
      className={`grid gap-2 rounded-lg border px-3 py-2.5 sm:grid-cols-[7.5rem_minmax(0,1fr)_auto] sm:items-center ${selected ? 'border-[var(--tape-focus)] bg-[var(--tape-selected)] ring-1 ring-[var(--tape-focus)]' : 'border-[var(--tape-line)] bg-[var(--tape-paper-raised)]'}`}
    >
      <div className="flex items-center gap-2 text-xs font-medium text-gray-500 dark:text-gray-400">
        <span className="flex h-6 w-6 items-center justify-center rounded text-white" style={{ backgroundColor: color }}>
          <Icon className="h-3.5 w-3.5" aria-hidden="true" />
        </span>
        <span>{clock(episode.started_at)}–{clock(episode.ended_at)}</span>
      </div>
      <div className="min-w-0">
        <Link
          to={`/timeline/${episode.episode_id}`}
          className="block truncate rounded text-sm font-semibold text-gray-900 outline-none hover:text-[var(--tape-focus)] focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)] dark:text-gray-100"
        >
          {episodeDisplayTitle(episode)}
        </Link>
        <p className="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400">{episode.summary || 'No summary recorded.'}</p>
      </div>
      <div className="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
        <span className="font-semibold uppercase tracking-[0.1em]">{channel === 'media' ? 'Media' : kindLabel(episode.kind)}</span>
        <span>{Math.round(episode.confidence * 100)}%</span>
      </div>
    </article>
  )
}

function GroupRow({
  group,
  episodes,
  labeling,
  renderEpisode,
  onSelectGroup,
  onRemoveGroup,
  selectedEpisodeId,
  suggestionEpisodeIds,
}: {
  group: DayReviewGroup
  episodes: TimelineEpisode[]
  labeling: boolean
  renderEpisode: (episode: TimelineEpisode) => ReactNode
  onSelectGroup: (episodeIds: string[]) => void
  onRemoveGroup: (groupId: string) => void
  selectedEpisodeId: string | null
  suggestionEpisodeIds: Set<string>
}) {
  const [expanded, setExpanded] = useState(false)
  const selectedInside = !!selectedEpisodeId && group.episode_ids.includes(selectedEpisodeId)
  const suggestionInside = group.episode_ids.some(id => suggestionEpisodeIds.has(id))
  const visibleExpanded = expanded || selectedInside || suggestionInside || labeling
  const dominantKind = mostCommonKind(episodes)
  const primaryKind = isMediaKind(dominantKind)
    ? 'Media'
    : group.lane === 'conversation'
      ? 'Conversation'
      : group.lane === 'background'
        ? 'Background'
          : kindLabel(dominantKind)
  const channel = isMediaKind(dominantKind)
    ? 'media'
    : group.lane === 'conversation'
      ? 'conversation'
      : group.lane === 'background'
        ? 'background'
          : 'foreground'
  const channelColor = channel === 'media'
    ? 'var(--tape-media)'
    : channel === 'conversation'
      ? 'var(--tape-conversation)'
      : channel === 'background'
        ? 'var(--tape-background)'
        : 'var(--tape-activity)'
  const displayTitle = episodes.length === 1 ? episodeDisplayTitle(episodes[0]) : group.title

  return (
    <section
      data-session-group-id={group.group_id}
      className={`overflow-hidden rounded-lg border bg-[var(--tape-paper-raised)] transition-[border-color,box-shadow] duration-150 ${selectedInside ? 'border-[var(--tape-focus)] ring-1 ring-[var(--tape-focus)]' : group.needs_attention ? 'border-amber-500/70' : 'border-[var(--tape-line)]'}`}
    >
      <div className="grid grid-cols-[0.3rem_minmax(0,1fr)]">
        <span style={{ backgroundColor: channelColor }} aria-hidden="true" />
        <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-2 rounded text-left outline-none focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)]"
            onClick={() => setExpanded(value => !value)}
            aria-expanded={visibleExpanded}
          >
            {visibleExpanded ? <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />}
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5">
                {group.semantic && <Combine className="h-3.5 w-3.5 shrink-0 text-amber-700 dark:text-amber-400" aria-label="Accepted semantic group" />}
                <span className="truncate text-sm font-semibold text-gray-900 dark:text-gray-100">{displayTitle}</span>
              </span>
              <span className="mt-0.5 block truncate text-xs text-gray-500 dark:text-gray-400">
                {clock(group.started_at)}–{clock(group.ended_at)} · {duration(group.duration_seconds)} captured{group.gap_seconds >= 60 ? ` across ${duration(group.span_seconds)}` : ''} · {group.episode_count} episode{group.episode_count === 1 ? '' : 's'}
              </span>
            </span>
          </button>
          <div className="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
            <span className="rounded bg-[var(--tape-chip)] px-2 py-1 font-semibold uppercase tracking-[0.1em]">{primaryKind}</span>
            {group.confirmed_count > 0 && <span className="flex items-center gap-1"><CheckCircle2 className="h-3.5 w-3.5 text-green-700 dark:text-green-400" />{group.confirmed_count}</span>}
            {group.needs_attention && <AlertTriangle className="h-4 w-4 text-amber-700 dark:text-amber-400" aria-label={group.attention_reasons.map(reason => reasonLabels[reason] || reason).join(', ')} />}
            {labeling && <Button size="sm" variant="ghost" onClick={() => onSelectGroup(group.episode_ids)}>Select</Button>}
            {labeling && group.semantic && <Button size="sm" variant="ghost" onClick={() => onRemoveGroup(group.group_id)}>Ungroup</Button>}
          </div>
        </div>
      </div>
      {visibleExpanded && (
        <div className="space-y-2 border-t border-[var(--tape-line)] bg-[var(--tape-paper)] p-2.5 sm:p-3">
          {group.semantic && group.summary && <p className="px-1 text-xs leading-5 text-gray-600 dark:text-gray-300">{group.summary}</p>}
          {episodes.map(episode => {
            const previewingSuggestion = suggestionEpisodeIds.size > 0
            const suggestionMember = suggestionEpisodeIds.has(episode.episode_id)
            return (
              <div
                key={episode.episode_id}
                data-grouping-episode-id={episode.episode_id}
                data-suggestion-preview={previewingSuggestion ? suggestionMember ? 'included' : 'excluded' : undefined}
                role={previewingSuggestion ? 'group' : undefined}
                aria-label={previewingSuggestion ? `${suggestionMember ? 'Included in' : 'Outside'} suggested grouping: ${episodeDisplayTitle(episode)}` : undefined}
                className={labeling && previewingSuggestion
                  ? `rounded-xl transition-[opacity,box-shadow] duration-150 ${suggestionMember ? 'ring-2 ring-[var(--tape-focus)] ring-offset-2 ring-offset-[var(--tape-paper)]' : 'opacity-35'}`
                  : ''}
              >
                {labeling
                  ? renderEpisode(episode)
                  : <CompactEpisodeRow episode={episode} group={group} selected={episode.episode_id === selectedEpisodeId || suggestionMember} />}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

function GroupingEditor({
  day,
  timezone,
  proposal,
  onProposal,
  selectedSuggestions,
  onSelectedSuggestions,
  activeSuggestionId,
  onActiveSuggestion,
  episodeMap,
}: {
  day: string
  timezone: string
  proposal: TimelineConsolidationProposal | null
  onProposal: (proposal: TimelineConsolidationProposal | null) => void
  selectedSuggestions: Set<string>
  onSelectedSuggestions: (suggestions: Set<string>) => void
  activeSuggestionId: string | null
  onActiveSuggestion: (id: string | null) => void
  episodeMap: Map<string, TimelineEpisode>
}) {
  const queryClient = useQueryClient()
  const suggest = useMutation({
    mutationFn: async () => (await timelineApi.suggestConsolidation(day, timezone)).data,
    onSuccess: data => {
      onProposal(data)
      onSelectedSuggestions(new Set(data.suggestions.map(item => item.suggestion_id)))
    },
  })
  const applySuggestions = useMutation({
    mutationFn: () => timelineApi.resolveConsolidation(day, timezone, [...selectedSuggestions]),
    onSuccess: async () => {
      onProposal(null)
      onSelectedSuggestions(new Set())
      onActiveSuggestion(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
      ])
    },
  })
  const error = suggest.error || applySuggestions.error
  return (
    <section id="suggested-grouping" className="scroll-mt-4 overflow-hidden rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)]">
      <div className="flex flex-wrap items-center justify-between gap-3 px-3 py-2.5">
        <div className="flex min-w-0 items-start gap-2">
          <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" />
          <div>
            <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Suggested grouping</h4>
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">Qwen can relate over-fragmented episodes without changing captured intervals. This does not start memory extraction.</p>
          </div>
        </div>
        <Button size="sm" variant="secondary" disabled={suggest.isPending || applySuggestions.isPending} onClick={() => suggest.mutate()} icon={suggest.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}>
          {proposal?.state === 'ready' ? 'Run again' : 'Generate'}
        </Button>
      </div>
      {proposal?.state === 'ready' && (
        <div className="space-y-2 border-t border-[var(--tape-line)] bg-[var(--tape-paper)] p-3">
          {proposal.suggestions.length ? proposal.suggestions.map(item => {
            const checked = selectedSuggestions.has(item.suggestion_id)
            const members = item.episode_ids.flatMap(id => episodeMap.get(id) || [])
            return (
              <div key={item.suggestion_id} className={`flex gap-3 rounded-md border p-3 ${activeSuggestionId === item.suggestion_id ? 'border-[var(--tape-focus)] bg-[var(--tape-selected)]' : 'border-[var(--tape-line)] bg-[var(--tape-paper-raised)]'}`}>
                <input
                  type="checkbox"
                  checked={checked}
                  aria-label={`Accept grouping: ${item.title}`}
                  onChange={() => onSelectedSuggestions(new Set(checked ? [...selectedSuggestions].filter(id => id !== item.suggestion_id) : [...selectedSuggestions, item.suggestion_id]))}
                  className="mt-1 h-4 w-4 rounded border-gray-300 text-amber-700 focus:ring-amber-600"
                />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-gray-900 dark:text-gray-100">{item.title}</p>
                  <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{members.length ? `${clock(members[0].started_at)}–${clock(members[members.length - 1].ended_at)} · ` : ''}{item.episode_ids.length} episodes · {Math.round(item.confidence * 100)}%</p>
                  <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">{item.reason}</p>
                </div>
                <button
                  type="button"
                  aria-label={`${activeSuggestionId === item.suggestion_id ? 'Hide' : 'Preview'} ${item.title}`}
                  onClick={() => onActiveSuggestion(activeSuggestionId === item.suggestion_id ? null : item.suggestion_id)}
                  className="flex h-7 w-7 items-center justify-center rounded text-gray-500 hover:bg-[var(--tape-chip)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--tape-focus)]"
                >
                  {activeSuggestionId === item.suggestion_id ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            )
          }) : <p className="text-sm text-gray-500 dark:text-gray-400">Qwen found no confident over-fragmentation.</p>}
          {!!proposal.suggestions.length && (
            <div className="flex justify-end pt-1">
              <Button size="sm" disabled={applySuggestions.isPending} onClick={() => applySuggestions.mutate()} icon={applySuggestions.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Combine className="h-4 w-4" />}>
                {selectedSuggestions.size
                  ? `Accept ${selectedSuggestions.size} selected grouping${selectedSuggestions.size === 1 ? '' : 's'}`
                  : 'Keep episodes separate'}
              </Button>
            </div>
          )}
        </div>
      )}
      {(proposal?.state === 'queued' || proposal?.state === 'generating') && <p className="flex items-center gap-2 border-t border-[var(--tape-line)] px-3 py-3 text-xs text-gray-500 dark:text-gray-400"><Loader2 className="h-4 w-4 animate-spin" /> Reviewing the day tape in the background…</p>}
      {proposal?.state === 'failed' && <p className="border-t border-red-200 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:text-red-300">{proposal.error || 'Grouping generation failed. You can retry it.'}</p>}
      {error && <p className="border-t border-red-200 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:text-red-300">{(error as Error).message}</p>}
    </section>
  )
}

export default function DayReviewBoard({
  day,
  timezone,
  projection,
  episodes,
  coverage = [],
  initialProposal,
  labeling,
  onToggleEditing,
  renderEpisode,
  onSelectGroup,
}: {
  day: string
  timezone: string
  projection: DayReviewProjection
  episodes: TimelineEpisode[]
  coverage?: TapeCoverageInterval[]
  initialProposal: TimelineConsolidationProposal | null
  labeling: boolean
  onToggleEditing: () => void
  renderEpisode: (episode: TimelineEpisode) => ReactNode
  onSelectGroup: (episodeIds: string[]) => void
}) {
  const queryClient = useQueryClient()
  const [lens, setLens] = useState<EvidenceLens>('all')
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null)
  const [proposal, setProposal] = useState<TimelineConsolidationProposal | null>(initialProposal)
  const [selectedSuggestions, setSelectedSuggestions] = useState<Set<string>>(new Set())
  const [activeSuggestionId, setActiveSuggestionId] = useState<string | null>(null)
  const episodeMap = useMemo(() => new Map(episodes.map(episode => [episode.episode_id, episode])), [episodes])

  useEffect(() => {
    setProposal(initialProposal)
    setSelectedSuggestions(new Set(initialProposal?.state === 'ready' ? initialProposal.suggestions.map(item => item.suggestion_id) : []))
  }, [initialProposal])
  useEffect(() => {
    setSelectedEpisodeId(null)
    setLens('all')
  }, [day])

  const removeGroup = useMutation({
    mutationFn: (groupId: string) => timelineApi.removeSemanticGroup(day, timezone, groupId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
  })
  const visibleGroups = projection.groups.filter(group => groupMatchesLens(group, group.episode_ids.flatMap(id => episodeMap.get(id) || []), lens))
  const suggestionEpisodeIds = useMemo(() => new Set(proposal?.suggestions.find(item => item.suggestion_id === activeSuggestionId)?.episode_ids || []), [proposal, activeSuggestionId])
  const selectEpisode = (episodeId: string) => {
    setSelectedEpisodeId(episodeId)
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-episode-id="${CSS.escape(episodeId)}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }))
  }

  return (
    <div className="space-y-3">
      <EvidenceTape
        projection={projection}
        episodes={episodes}
        coverage={coverage}
        lens={lens}
        selectedEpisodeId={selectedEpisodeId}
        previewEpisodeIds={suggestionEpisodeIds}
        onLensChange={setLens}
        onSelectEpisode={selectEpisode}
      />
      <div className="flex flex-wrap items-end justify-between gap-3 pt-1">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Session ledger</h3>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{projection.episode_count} episodes arranged into {projection.group_count} chronological sessions.</p>
        </div>
        <Button size="sm" variant={labeling ? 'primary' : 'secondary'} onClick={onToggleEditing} icon={<Pencil className="h-4 w-4" />} aria-pressed={labeling}>
          {labeling ? 'Close editor' : 'Edit episodes'}
        </Button>
      </div>
      {labeling && (
        <GroupingEditor
          day={day}
          timezone={timezone}
          proposal={proposal}
          onProposal={setProposal}
          selectedSuggestions={selectedSuggestions}
          onSelectedSuggestions={setSelectedSuggestions}
          activeSuggestionId={activeSuggestionId}
          onActiveSuggestion={setActiveSuggestionId}
          episodeMap={episodeMap}
        />
      )}
      <div className="space-y-1.5">
        {visibleGroups.map(group => (
          <GroupRow
            key={group.group_id}
            group={group}
            episodes={group.episode_ids.flatMap(id => episodeMap.get(id) || [])}
            labeling={labeling}
            renderEpisode={renderEpisode}
            onSelectGroup={onSelectGroup}
            onRemoveGroup={groupId => removeGroup.mutate(groupId)}
            selectedEpisodeId={selectedEpisodeId}
            suggestionEpisodeIds={suggestionEpisodeIds}
          />
        ))}
        {!visibleGroups.length && (
          <div className="rounded-lg border border-dashed border-[var(--tape-line)] bg-[var(--tape-paper)] p-5 text-sm text-gray-500 dark:text-gray-400">
            {lens === 'coverage' ? 'Coverage intervals are inspected on the tape above.' : 'No sessions match this lens.'}
          </div>
        )}
      </div>
      {removeGroup.isError && <p className="text-xs text-red-700 dark:text-red-300">{(removeGroup.error as Error).message}</p>}
    </div>
  )
}
