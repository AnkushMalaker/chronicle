import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, Bookmark, CalendarDays, ChevronLeft, ChevronRight,
  Combine, Image, MoreHorizontal, RefreshCw, ScrollText,
} from 'lucide-react'
import EpisodeCard from '../components/timeline/EpisodeCard'
import EpisodeLabelBar from '../components/timeline/EpisodeLabelBar'
import DayReviewBoard from '../components/timeline/DayReviewBoard'
import EpisodeReviewCheckpoint from '../components/timeline/EpisodeReviewCheckpoint'
import { TapeCoverageInterval } from '../components/timeline/EvidenceTape'
import { isSemanticMemoryEligible } from '../components/timeline/episodePresentation'
import { EmptyDayHandoff, ReviewBacklogMenu } from '../components/timeline/ReviewCursor'
import { dateFromSearch, localDate, shiftDate } from '../components/timeline/timelineNavigation'
import { useTimelineTimezone } from '../hooks/useTimelineTimezone'
import {
  ManualMemory, TimelineEpisodeUpdate, deviceInputApi,
  manualMemoriesApi, timelineApi,
} from '../services/api'
import { Button } from '../components/ui'

function analysisMessage(state?: string, retryAfter?: string | null) {
  if (state === 'pending' || state === 'preparing') return 'Timeline analysis is queued.'
  if (state === 'running') return 'Reading the day’s evidence and forming episodes.'
  if (state === 'validating') return 'Checking episode boundaries and evidence citations.'
  if (state === 'quota_deferred') return `Analysis is waiting for Codex capacity${retryAfter ? ` until ${new Date(retryAfter).toLocaleString()}` : ''}.`
  if (state === 'awaiting_evidence') return 'No usable evidence has arrived for this day yet.'
  return null
}

function reviewLabel(state?: string) {
  if (state === 'memory_pending') return 'Memory decision ready'
  if (state === 'failed') return 'Memory review needs attention'
  return null
}

function clockTime(value: string) {
  return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function ManualMemoryPreview({ memory }: { memory: ManualMemory }) {
  const attachment = memory.attachments[0]
  const thumbnail = useQuery({
    queryKey: ['manual-memory-thumbnail', memory.memory_id, attachment?.attachment_id],
    queryFn: async () => (await manualMemoriesApi.getThumbnail(memory.memory_id, attachment.attachment_id)).data,
    enabled: Boolean(attachment),
    staleTime: Infinity,
  })
  const url = useMemo(() => thumbnail.data ? URL.createObjectURL(thumbnail.data) : null, [thumbnail.data])
  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url)
  }, [url])
  if (thumbnail.isLoading) return <div className="aspect-[4/3] animate-pulse bg-gray-100 dark:bg-gray-800" />
  if (!url) return <div className="flex aspect-[4/3] items-center justify-center bg-gray-100 text-gray-400 dark:bg-gray-800"><Image className="h-7 w-7" /></div>
  return <img src={url} alt="" className="aspect-[4/3] w-full bg-gray-100 object-contain dark:bg-gray-800" />
}

function ManualMemories({ items }: { items: ManualMemory[] }) {
  return (
    <section className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-4">
      <h3 className="font-medium text-gray-900 dark:text-gray-100">Manual memories</h3>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Explicitly saved material for this account, independent of timeline analysis.</p>
      {items.length ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {items.map(item => {
            const description = item.attachments.find(attachment => attachment.description)?.description || ''
            return (
              <article key={item.memory_id} className="overflow-hidden rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)]">
                <ManualMemoryPreview memory={item} />
                <div className="p-3">
                  <p className="line-clamp-2 text-sm text-gray-800 dark:text-gray-200">{item.note || description || 'Manual memory.'}</p>
                  <p className="mt-1.5 text-xs text-gray-500 dark:text-gray-400">{new Date(item.shared_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</p>
                </div>
              </article>
            )
          })}
        </div>
      ) : <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">No manual memories saved yet.</p>}
    </section>
  )
}

function CoverageInspector({ coverage }: { coverage: TapeCoverageInterval[] }) {
  if (!coverage.length) return null
  return (
    <details className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2.5">
      <summary className="cursor-pointer text-sm font-semibold text-gray-800 marker:text-gray-400 dark:text-gray-200">
        Inspect {coverage.length} coverage interval{coverage.length === 1 ? '' : 's'}
      </summary>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {coverage.map((interval, index) => (
          <div key={`${interval.kind}:${interval.started_at}:${interval.ended_at}:${index}`} className="rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 py-2 text-xs">
            <p className="font-semibold text-gray-800 dark:text-gray-200">{interval.label}</p>
            <p className="mt-0.5 text-gray-500 dark:text-gray-400">{clockTime(interval.started_at)}–{clockTime(interval.ended_at)}</p>
          </div>
        ))}
      </div>
    </details>
  )
}

export default function Timeline() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const {
    timezone, browserTimezone, storedTimezone, shouldOfferBrowserTimezone,
    saveBrowserTimezone, savingBrowserTimezone,
  } = useTimelineTimezone()
  const today = localDate(new Date(), timezone)
  const day = dateFromSearch(`?${searchParams.toString()}`, today)
  const [showRaw, setShowRaw] = useState(false)
  const [showManualMemories, setShowManualMemories] = useState(false)
  const [labeling, setLabeling] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const setDay = (nextDay: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('date', nextDay)
    setSearchParams(next)
    setSelected(new Set())
    setLabeling(false)
  }

  const timeline = useQuery({
    queryKey: ['semantic-timeline', day, timezone],
    queryFn: async () => (await timelineApi.getDay(day, timezone)).data,
    refetchInterval: query => {
      const state = query.state.data?.analysis?.state
      const consolidationState = query.state.data?.consolidation?.state
      if (consolidationState === 'queued' || consolidationState === 'generating') return 5_000
      return state && !['complete', 'failed', 'awaiting_evidence'].includes(state) ? 10_000 : false
    },
  })
  const reviewQueue = useQuery({
    queryKey: ['timeline-review-queue', timezone],
    queryFn: async () => (await timelineApi.getReviewQueue(timezone)).data.items,
    refetchInterval: query => query.state.data?.some(item => ['memory_queued', 'memory_generating', 'memory_applying'].includes(item.state)) ? 5_000 : false,
  })
  const projectionStart = timeline.data?.review_projection?.day_started_at
  const projectionEnd = timeline.data?.review_projection?.day_ended_at
  const raw = useQuery({
    queryKey: ['raw-device-timeline', day, timezone],
    queryFn: async () => (await deviceInputApi.getTimeline(projectionStart!, projectionEnd!)).data.items,
    enabled: showRaw && Boolean(projectionStart && projectionEnd),
  })
  const manualMemories = useQuery({
    queryKey: ['manual-memories'],
    queryFn: async () => (await manualMemoriesApi.list()).data.items,
    enabled: showManualMemories,
  })
  const analyze = useMutation({
    mutationFn: (force: boolean) => timelineApi.analyze(day, timezone, force),
    onSuccess: () => Promise.all([
      queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
      queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
    ]),
  })
  const refreshDay = () => {
    setSelected(new Set())
    return queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] })
  }
  const adjust = useMutation({ mutationFn: ({ episodeId, changes }: { episodeId: string; changes: TimelineEpisodeUpdate }) => timelineApi.updateEpisode(episodeId, changes), onSuccess: refreshDay })
  const split = useMutation({ mutationFn: ({ episodeId, at }: { episodeId: string; at: string }) => timelineApi.splitEpisode(episodeId, at), onSuccess: refreshDay })
  const group = useMutation({ mutationFn: (episodeIds: string[]) => timelineApi.groupEpisodes(day, timezone, episodeIds), onSuccess: refreshDay })
  const remove = useMutation({ mutationFn: (episodeId: string) => timelineApi.deleteEpisode(episodeId), onSuccess: refreshDay })
  const finalizeEpisodes = useMutation({
    mutationFn: () => timelineApi.finalizeEpisodes(day, timezone),
    onSuccess: async () => {
      setLabeling(false)
      setSelected(new Set())
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-review-queue', timezone] }),
      ])
    },
  })
  const mutating = adjust.isPending || split.isPending || group.isPending || remove.isPending
  const mutationError = [adjust, split, group, remove].find(mutation => mutation.error)?.error

  const episodes = timeline.data?.episodes || []
  const memoryEligibleCount = episodes.filter(isSemanticMemoryEligible).length
  const referenceOnlyCount = episodes.length - memoryEligibleCount
  const status = timeline.data?.analysis
  const unaccounted = timeline.data?.coverage?.unassigned_intervals || []
  const classified = unaccounted.some(interval => interval.cause)
  const unexplained = classified ? unaccounted.filter(item => item.cause === 'unexplained') : unaccounted
  const uncaptured = classified ? unaccounted.filter(item => item.cause === 'no_capture') : []
  const unreconciled = timeline.data?.reconciliation?.ranges || []
  const progressMessage = analysisMessage(status?.state, status?.retry_after)
  const processing = !!status && ['pending', 'preparing', 'running', 'validating', 'quota_deferred'].includes(status.state)
  const currentMemoryReviewLabel = reviewLabel(timeline.data?.review?.state)
  const coverage = useMemo<TapeCoverageInterval[]>(() => [
    ...unexplained.map(item => ({ started_at: item.started_at, ended_at: item.ended_at, kind: 'unexplained' as const, label: item.reason || 'Captured but unexplained' })),
    ...uncaptured.map(item => ({ started_at: item.started_at, ended_at: item.ended_at, kind: 'no_capture' as const, label: item.reason || 'No capture' })),
    ...unreconciled.map(item => ({ started_at: item.started_at, ended_at: item.ended_at, kind: 'unreconciled' as const, label: `Awaiting reconciliation · ${item.state}` })),
  ], [unexplained, uncaptured, unreconciled])
  const reviewGrouping = () => {
    setLabeling(true)
    setSelected(new Set())
    requestAnimationFrame(() => document.querySelector<HTMLElement>('#suggested-grouping')?.scrollIntoView?.({ block: 'center', behavior: 'smooth' }))
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold text-gray-900 dark:text-gray-100"><CalendarDays className="h-6 w-6 text-[var(--tape-media)]" /> Timeline</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">A semantic account of the day, grounded in capture evidence.</p>
        </div>
        <div className="flex items-end gap-1.5">
          <Button variant="ghost" size="sm" aria-label="Previous day" onClick={() => setDay(shiftDate(day, -1))}><ChevronLeft className="h-4 w-4" /></Button>
          <label className="flex flex-col gap-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-gray-500 dark:text-gray-400">
            Date
            <input type="date" value={day} onChange={event => setDay(event.target.value)} className="min-h-9 rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-2.5 py-1.5 text-sm font-medium normal-case tracking-normal text-gray-900 outline-none focus:ring-2 focus:ring-[var(--tape-focus)] dark:text-gray-100" />
          </label>
          <Button variant="ghost" size="sm" aria-label="Next day" onClick={() => setDay(shiftDate(day, 1))}><ChevronRight className="h-4 w-4" /></Button>
        </div>
      </header>

      {shouldOfferBrowserTimezone && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2 text-xs text-gray-600 dark:text-gray-300">
          <span>{storedTimezone ? `Times are shown in ${storedTimezone}; this browser reports ${browserTimezone}.` : `Times are shown in the browser timezone, ${browserTimezone}. Save it to keep day boundaries consistent on other devices.`}</span>
          <Button variant="ghost" size="sm" onClick={saveBrowserTimezone} disabled={savingBrowserTimezone}>{storedTimezone ? 'Use browser timezone' : 'Save browser timezone'}</Button>
        </div>
      )}

      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2.5 text-xs text-gray-600 dark:text-gray-300">
        {timeline.isFetching && <RefreshCw className="h-4 w-4 animate-spin text-gray-400" />}
        <span className="font-semibold text-gray-800 dark:text-gray-200">{episodes.length} episodes</span>
        {timeline.data?.coverage?.window_count != null && <span>· {timeline.data.coverage.window_count} evidence windows</span>}
        {!!coverage.length && <span className="flex items-center gap-1 text-amber-800 dark:text-amber-300"><AlertTriangle className="h-3.5 w-3.5" />{coverage.length} coverage intervals</span>}
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1">
          {currentMemoryReviewLabel && <Link to={`/memory-ledger?view=review&date=${day}`} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 font-semibold text-[var(--tape-focus)] hover:bg-[var(--tape-chip)]"><ScrollText className="h-3.5 w-3.5" />{currentMemoryReviewLabel}</Link>}
          <ReviewBacklogMenu items={reviewQueue.data || []} day={day} />
          <details className="relative">
            <summary className="flex cursor-pointer list-none items-center gap-1 rounded-md px-2 py-1 font-semibold text-gray-700 hover:bg-[var(--tape-chip)] dark:text-gray-200"><MoreHorizontal className="h-4 w-4" /> Day tools</summary>
            <div className="absolute right-0 z-30 mt-1 w-48 space-y-1 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-1.5 shadow-lg">
              <button type="button" onClick={() => setShowManualMemories(value => !value)} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-[var(--tape-chip)]"><Bookmark className="h-4 w-4" />{showManualMemories ? 'Hide' : 'Show'} manual memories</button>
              <button type="button" onClick={() => setShowRaw(value => !value)} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-[var(--tape-chip)]"><ScanLineIcon />{showRaw ? 'Hide' : 'Show'} raw capture</button>
              <button type="button" onClick={() => analyze.mutate(status?.state === 'failed')} disabled={analyze.isPending} className="flex w-full items-center gap-2 rounded px-2 py-2 text-left hover:bg-[var(--tape-chip)] disabled:opacity-40"><RefreshCw className="h-4 w-4" />{status?.state === 'failed' ? 'Retry analysis' : episodes.length ? 'Reanalyze day' : 'Analyze day'}</button>
            </div>
          </details>
        </div>
      </section>

      {progressMessage && episodes.length > 0 && <div className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] px-3 py-2.5 text-sm text-gray-600 dark:text-gray-300">{progressMessage}</div>}
      {status?.state === 'failed' && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2.5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <AlertTriangle className="h-4 w-4" /><span className="min-w-0 flex-1">Analysis failed. {status.error}</span><Button size="sm" variant="danger" onClick={() => analyze.mutate(true)}>Retry</Button>
        </div>
      )}
      {timeline.isError && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2.5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <AlertTriangle className="h-4 w-4" />
          <span className="min-w-0 flex-1">Could not load this day. {(timeline.error as Error).message}</span>
          <Button size="sm" variant="danger" onClick={() => timeline.refetch()}>Retry</Button>
        </div>
      )}
      <CoverageInspector coverage={coverage} />
      {timeline.data?.review && episodes.length > 0 && (
        <EpisodeReviewCheckpoint
          day={day}
          review={timeline.data.review}
          episodeCount={episodes.length}
          eligibleCount={memoryEligibleCount}
          referenceOnlyCount={referenceOnlyCount}
          unreconciledCount={unreconciled.length}
          consolidation={timeline.data.consolidation}
          finalizing={finalizeEpisodes.isPending}
          error={finalizeEpisodes.error as Error | null}
          onReviewGrouping={reviewGrouping}
          onFinish={() => finalizeEpisodes.mutate()}
        />
      )}
      {showManualMemories && (manualMemories.isLoading ? <div className="rounded-lg border border-[var(--tape-line)] p-4 text-sm text-gray-500">Loading manual memories…</div> : <ManualMemories items={manualMemories.data || []} />)}

      {labeling && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-[var(--tape-focus)] bg-[var(--tape-selected)] p-3 text-sm">
          <span className="text-gray-700 dark:text-gray-200">{selected.size ? `${selected.size} selected` : 'Select two or more episodes to group, or correct one below.'}</span>
          <Button size="sm" disabled={selected.size < 2 || mutating} onClick={() => group.mutate([...selected])} icon={<Combine className="h-4 w-4" />}>Group selected</Button>
          {!!selected.size && <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>}
          {!!mutationError && <p className="w-full text-xs text-red-700 dark:text-red-300">{(mutationError as { message?: string }).message || 'That edit was rejected.'}</p>}
        </div>
      )}

      {timeline.data?.review_projection && episodes.length ? (
        <DayReviewBoard
          day={day}
          timezone={timezone}
          projection={timeline.data.review_projection}
          episodes={episodes}
          coverage={coverage}
          initialProposal={timeline.data.consolidation}
          labeling={labeling}
          onToggleEditing={() => { setLabeling(value => !value); setSelected(new Set()) }}
          onSelectGroup={episodeIds => setSelected(new Set(episodeIds))}
          renderEpisode={episode => (
            <div key={episode.episode_id}>
              <EpisodeCard episode={episode} nested={episode.activity_mode === 'background' || !!episode.parent_episode_id} />
              <EpisodeLabelBar
                episode={episode}
                selected={selected.has(episode.episode_id)}
                busy={mutating}
                nested={episode.activity_mode === 'background' || !!episode.parent_episode_id}
                onToggleSelected={() => setSelected(current => {
                  const next = new Set(current)
                  next.has(episode.episode_id) ? next.delete(episode.episode_id) : next.add(episode.episode_id)
                  return next
                })}
                onAdjust={changes => adjust.mutate({ episodeId: episode.episode_id, changes })}
                onSplit={at => split.mutate({ episodeId: episode.episode_id, at })}
                onDelete={() => remove.mutate(episode.episode_id)}
              />
            </div>
          )}
        />
      ) : !timeline.isLoading && !timeline.isError ? (
        <EmptyDayHandoff
          items={reviewQueue.data || []}
          title={processing
            ? day === today ? 'Today’s episodes are still processing.' : 'This day’s episodes are still processing.'
            : status?.state === 'awaiting_evidence'
            ? day === today ? 'Nothing captured today.' : 'Nothing was captured for this day.'
            : status?.state === 'complete'
              ? 'Analysis found no episodes for this day.'
              : status?.state === 'failed'
                ? 'This day’s analysis needs attention.'
              : day === today ? 'Today has no processed episodes yet.' : 'This day has no processed episodes yet.'}
          description={processing
            ? `${progressMessage || 'Analysis is in progress.'} Continue reviewing an earlier episode day while it finishes.`
            : status?.state === 'awaiting_evidence'
            ? 'There is no capture evidence to turn into episodes. Continue with the review trail whenever you are ready.'
            : status?.state === 'complete'
              ? 'The analysis completed without producing a semantic episode. You can run it again or continue the review trail.'
              : status?.state === 'failed'
                ? 'Use Retry above for this day, or continue reviewing an earlier episode day.'
              : 'Start processing this day, or resume the oldest review action that needs you.'}
          canAnalyze={!status || status.state === 'complete'}
          analyzing={analyze.isPending}
          analyzeLabel={status?.state === 'complete' ? 'Reanalyze this day' : 'Analyze this day'}
          onAnalyze={() => analyze.mutate(status?.state === 'complete')}
        />
      ) : null}

      {showRaw && (
        <section className="rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-4">
          <h3 className="font-medium text-gray-900 dark:text-gray-100">Raw capture diagnostics</h3>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Transport and observation rows used to build evidence, not the semantic timeline.</p>
          {raw.isLoading && <p className="mt-3 text-sm text-gray-500">Loading raw capture…</p>}
          {raw.data && <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">{raw.data.length} raw items · {raw.data.filter(item => item.kind === 'audio').length} audio chunks · {raw.data.filter(item => item.kind !== 'audio').length} visual/context items</p>}
        </section>
      )}
    </div>
  )
}

function ScanLineIcon() {
  return <span className="inline-flex h-4 w-4 items-center justify-center text-[10px] font-bold" aria-hidden="true">|||</span>
}
