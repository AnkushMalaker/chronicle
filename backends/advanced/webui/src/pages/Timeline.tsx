import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, Copy, Link2, Monitor, RefreshCw } from 'lucide-react'
import EpisodeCard from '../components/timeline/EpisodeCard'
import { deviceInputApi, timelineApi } from '../services/api'
import { timeAgo } from '../utils/timeAgo'
import { Button, Card, IconButton } from '../components/ui'

function dayBounds(day: string) {
  const start = new Date(`${day}T00:00:00`)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  return [start.toISOString(), end.toISOString()] as const
}

function sourceStatusLabel(status: string) {
  return status.charAt(0).toUpperCase() + status.slice(1)
}

function analysisMessage(state?: string, retryAfter?: string | null) {
  if (state === 'pending' || state === 'preparing') return 'Timeline analysis is queued.'
  if (state === 'running') return 'Reading the day’s evidence and forming episodes.'
  if (state === 'validating') return 'Checking episode boundaries and evidence citations.'
  if (state === 'quota_deferred') return `Analysis is waiting for Codex capacity${retryAfter ? ` until ${new Date(retryAfter).toLocaleString()}` : ''}.`
  if (state === 'awaiting_evidence') return 'No usable evidence has arrived for this day yet.'
  return null
}

export default function Timeline() {
  const queryClient = useQueryClient()
  const [day, setDay] = useState(() => {
    const parts = new Intl.DateTimeFormat('en-CA').formatToParts(new Date())
    const value = Object.fromEntries(parts.map(part => [part.type, part.value]))
    return `${value.year}-${value.month}-${value.day}`
  })
  const [showRaw, setShowRaw] = useState(false)
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const [start, end] = useMemo(() => dayBounds(day), [day])

  const timeline = useQuery({
    queryKey: ['semantic-timeline', day, timezone],
    queryFn: async () => (await timelineApi.getDay(day, timezone)).data,
    refetchInterval: query => {
      const state = query.state.data?.analysis?.state
      return state && !['complete', 'failed', 'awaiting_evidence'].includes(state) ? 10_000 : false
    },
  })
  const raw = useQuery({
    queryKey: ['raw-device-timeline', day],
    queryFn: async () => (await deviceInputApi.getTimeline(start, end)).data.items,
    enabled: showRaw,
  })
  const sources = useQuery({
    queryKey: ['device-input-sources'],
    queryFn: async () => (await deviceInputApi.getSources()).data.sources,
    refetchInterval: 30_000,
  })
  const pairing = useMutation({
    mutationFn: async () => (await deviceInputApi.createPairingCode()).data,
  })
  const analyze = useMutation({
    mutationFn: (force: boolean) => timelineApi.analyze(day, timezone, force),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['semantic-timeline', day, timezone] }),
  })

  useEffect(() => {
    timelineApi.setTimezone(timezone).catch(() => undefined)
  }, [timezone])

  const episodes = timeline.data?.episodes || []
  const status = timeline.data?.analysis
  const progressMessage = analysisMessage(status?.state, status?.retry_after)

  return (
    <div className="space-y-8">
      <header className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold text-gray-900 dark:text-gray-100">
            <CalendarDays className="h-6 w-6 text-blue-600" /> Timeline
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">A semantic account of the day, grounded in capture evidence.</p>
        </div>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300">
          Date
          <input
            type="date"
            value={day}
            onChange={event => setDay(event.target.value)}
            className="min-h-10 rounded-md border border-gray-300 bg-white px-3 py-2 text-sm font-medium normal-case tracking-normal text-gray-900 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
          />
        </label>
      </header>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">Sources</h2>
          <Button variant="secondary" size="md" onClick={() => pairing.mutate()} icon={<Link2 className="h-4 w-4" />}>Pair ScreenPipe</Button>
        </div>
        {pairing.data && (
          <div className="mb-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-gray-700 dark:border-blue-800 dark:bg-blue-950/30 dark:text-gray-200">
            Pairing code <code className="mx-1 font-mono font-bold">{pairing.data.code}</code> expires {new Date(pairing.data.expires_at).toLocaleTimeString()}.
            <IconButton label="Copy pairing code" onClick={() => navigator.clipboard.writeText(pairing.data!.code)} className="ml-2"><Copy className="h-4 w-4" /></IconButton>
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {(sources.data || []).map(source => (
            <Card key={source.source_id} className="flex gap-3">
              <Monitor className="h-5 w-5 text-gray-500 dark:text-gray-400" />
              <div className="min-w-0">
                <div className="truncate font-medium text-gray-900 dark:text-gray-100">{source.name}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400">{source.provider} · {source.platform}</div>
                <div className={`mt-1 text-xs ${source.status === 'online' ? 'text-green-600 dark:text-green-400' : source.status === 'error' ? 'text-red-600 dark:text-red-400' : 'text-gray-500 dark:text-gray-400'}`}>
                  {sourceStatusLabel(source.status)}{source.last_seen_at ? ` · ${timeAgo(source.last_seen_at)}` : ''}
                </div>
              </div>
            </Card>
          ))}
          {!sources.isLoading && !sources.data?.length && <div className="rounded-lg border border-dashed border-gray-300 p-4 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">No capture sources paired.</div>}
        </div>
      </section>

      <section>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <h2 className="font-semibold text-gray-900 dark:text-gray-100">Day</h2>
            {timeline.isFetching && <RefreshCw className="h-4 w-4 animate-spin text-gray-400" />}
            {timeline.data?.coverage?.window_count != null && <span className="text-xs text-gray-500">{timeline.data.coverage.window_count} evidence windows</span>}
          </div>
          <div className="flex gap-2">
            <Button variant="secondary" size="sm" onClick={() => setShowRaw(value => !value)}>{showRaw ? 'Hide raw capture' : 'Raw capture'}</Button>
            <Button size="sm" onClick={() => analyze.mutate(status?.state === 'failed')} disabled={analyze.isPending || ['pending', 'preparing', 'running', 'validating'].includes(status?.state || '')}>
              {status?.state === 'failed' ? 'Retry analysis' : episodes.length ? 'Reanalyze day' : 'Analyze day'}
            </Button>
          </div>
        </div>

        {progressMessage && <div className="mb-4 rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">{progressMessage}</div>}
        {status?.state === 'failed' && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">Analysis failed. {status.error}</div>}

        <div className="space-y-4">
          {episodes.map(episode => <EpisodeCard key={episode.episode_id} episode={episode} nested={episode.activity_mode === 'background' || !!episode.parent_episode_id} />)}
          {!timeline.isLoading && !episodes.length && !progressMessage && status?.state !== 'failed' && (
            <div className="rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
              {status?.state === 'awaiting_evidence' ? 'Nothing captured for this day.' : 'This day has not been analyzed yet.'}
            </div>
          )}
        </div>

        {showRaw && (
          <div className="mt-6 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
            <h3 className="font-medium text-gray-900 dark:text-gray-100">Raw capture diagnostics</h3>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Transport and observation rows used to build evidence. This is diagnostic data, not the semantic timeline.</p>
            {raw.isLoading && <p className="mt-3 text-sm text-gray-500">Loading raw capture…</p>}
            {raw.data && <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">{raw.data.length} raw items · {raw.data.filter(item => item.kind === 'audio').length} audio chunks · {raw.data.filter(item => item.kind !== 'audio').length} visual/context items</p>}
          </div>
        )}
      </section>
    </div>
  )
}
