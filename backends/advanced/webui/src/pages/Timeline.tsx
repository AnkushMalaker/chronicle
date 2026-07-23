import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Activity, AppWindow, ArrowDownUp, CalendarDays, Copy, Image, Link2, Monitor, RefreshCw } from 'lucide-react'
import { deviceInputApi, DeviceInputItem } from '../services/api'
import { Button, Card, IconButton } from '../components/ui'
import { timeAgo } from '../utils/timeAgo'

function dayBounds(day: string) {
  const start = new Date(`${day}T00:00:00`)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  return [start.toISOString(), end.toISOString()] as const
}

function ItemIcon({ item }: { item: DeviceInputItem }) {
  if (item.kind === 'immich_memory') return <Image className="w-5 h-5" />
  if (item.kind === 'activity' || item.kind === 'observation' || item.kind === 'screen_context') return <AppWindow className="w-5 h-5" />
  return <Activity className="w-5 h-5" />
}

function TimelineThumbnail({ item }: { item: DeviceInputItem }) {
  useQuery({
    queryKey: ['device-input-thumbnail-request', item.id],
    queryFn: async () => (await deviceInputApi.requestThumbnail(item.id)).data,
    enabled: item.kind === 'activity' && item.metadata.thumbnail_available !== true,
    staleTime: Infinity,
    retry: false,
  })
  const thumbnail = useQuery({
    queryKey: ['device-input-thumbnail', item.id],
    queryFn: async () => (await deviceInputApi.getThumbnail(item.id)).data,
    enabled: item.metadata.thumbnail_available === true,
    staleTime: Infinity,
  })
  const url = useMemo(
    () => thumbnail.data ? URL.createObjectURL(thumbnail.data) : null,
    [thumbnail.data],
  )
  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])
  if (!url) return null
  return <img src={url} alt="Screen captured during this activity" loading="lazy" className="mt-3 max-h-64 max-w-full w-auto rounded-md object-contain" />
}

const AUDIO_SESSION_GAP_MS = 90_000
const AUDIO_SESSION_MAX_MS = 30 * 60_000
const TIMELINE_ORDER_KEY = 'chronicle_timeline_order'

type TimelineOrder = 'newest' | 'oldest'

const TIME_RIBBON_SEGMENTS = {
  oldest: [
    'from-purple-600 to-amber-600',
    'from-amber-600 to-cyan-600',
    'from-cyan-600 to-green-600',
    'from-green-600 to-orange-600',
    'from-orange-600 to-purple-600',
  ],
  newest: [
    'from-purple-600 to-orange-600',
    'from-orange-600 to-green-600',
    'from-green-600 to-cyan-600',
    'from-cyan-600 to-amber-600',
    'from-amber-600 to-purple-600',
  ],
} satisfies Record<TimelineOrder, string[]>

function timeOfDayClass(timestamp: string) {
  const hour = new Date(timestamp).getHours()
  if (hour < 6) return 'bg-purple-700'
  if (hour < 10) return 'bg-amber-700'
  if (hour < 14) return 'bg-cyan-700'
  if (hour < 18) return 'bg-green-700'
  if (hour < 21) return 'bg-orange-700'
  return 'bg-purple-700'
}

export function groupTimelineAudio(items: DeviceInputItem[]): DeviceInputItem[] {
  const visible = items.filter(item => item.kind !== 'audio')
  const bySource = new Map<string, DeviceInputItem[]>()
  for (const item of items) {
    if (item.kind !== 'audio') continue
    bySource.set(item.source_id, [...(bySource.get(item.source_id) || []), item])
  }

  for (const [sourceId, sourceItems] of bySource) {
    const ordered = sourceItems.sort((a, b) => Date.parse(a.captured_at) - Date.parse(b.captured_at))
    let session: DeviceInputItem | null = null
    for (const item of ordered) {
      const itemStart = Date.parse(item.captured_at)
      const itemEnd = Date.parse(item.ended_at || item.captured_at)
      const sessionStart = session ? Date.parse(session.captured_at) : 0
      const sessionEnd = session ? Date.parse(session.ended_at || session.captured_at) : 0
      if (!session || itemStart - sessionEnd > AUDIO_SESSION_GAP_MS || itemStart - sessionStart >= AUDIO_SESSION_MAX_MS) {
        session = {
          ...item,
          id: `audio-session:${sourceId}:${item.id}`,
          metadata: {
            chunk_count: 1,
            directions: item.metadata.direction ? [item.metadata.direction] : [],
          },
        }
        visible.push(session)
        continue
      }
      session.ended_at = new Date(Math.max(sessionEnd, itemEnd)).toISOString()
      session.metadata.chunk_count += 1
      const direction = item.metadata.direction
      if (direction && !session.metadata.directions.includes(direction)) {
        session.metadata.directions.push(direction)
      }
    }
  }
  return visible.sort((a, b) => Date.parse(a.captured_at) - Date.parse(b.captured_at))
}

function formatDuration(item: DeviceInputItem) {
  const seconds = Math.max(0, Math.round((Date.parse(item.ended_at || item.captured_at) - Date.parse(item.captured_at)) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

export default function Timeline() {
  const [day, setDay] = useState(() => new Date().toISOString().slice(0, 10))
  const [order, setOrder] = useState<TimelineOrder>(() => {
    return localStorage.getItem(TIMELINE_ORDER_KEY) === 'oldest' ? 'oldest' : 'newest'
  })
  const [start, end] = useMemo(() => dayBounds(day), [day])
  const timeline = useQuery({
    queryKey: ['device-timeline', day],
    queryFn: async () => (await deviceInputApi.getTimeline(start, end)).data.items,
    refetchInterval: 10_000,
  })
  const sources = useQuery({ queryKey: ['device-input-sources'], queryFn: async () => (await deviceInputApi.getSources()).data.sources, refetchInterval: 30_000 })
  const pairing = useMutation({ mutationFn: async () => (await deviceInputApi.createPairingCode()).data })
  const visibleItems = useMemo(() => groupTimelineAudio(timeline.data || []), [timeline.data])
  const orderedItems = useMemo(
    () => order === 'newest' ? [...visibleItems].reverse() : visibleItems,
    [order, visibleItems],
  )

  useEffect(() => {
    localStorage.setItem(TIMELINE_ORDER_KEY, order)
  }, [order])

  return (
    <div className="space-y-8">
      <header className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2"><CalendarDays className="w-6 h-6 text-blue-600" />Day Ribbon</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Conversations, desktop activity and salient photo memories.</p>
        </div>
        <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2">
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-blue-700 dark:text-blue-300">
            Date
            <input
              type="date"
              value={day}
              onChange={event => setDay(event.target.value)}
              className="min-h-10 rounded-md border border-blue-300 bg-blue-50 px-3 py-2 text-sm font-semibold normal-case tracking-normal text-blue-950 outline-none transition-colors [color-scheme:light] focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 dark:border-blue-700 dark:bg-blue-950/40 dark:text-blue-100 dark:[color-scheme:dark]"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-300">
            Order
            <span className="relative">
              <ArrowDownUp className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500 dark:text-gray-400" />
              <select
                aria-label="Timeline order"
                value={order}
                onChange={event => setOrder(event.target.value as TimelineOrder)}
                className="min-h-10 w-full appearance-none rounded-md border border-gray-300 bg-white py-2 pl-9 pr-8 text-sm font-medium normal-case tracking-normal text-gray-900 outline-none transition-colors focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
              >
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
              </select>
              <span aria-hidden="true" className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-gray-500">▾</span>
            </span>
          </label>
        </div>
      </header>

      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">Sources</h2>
          <Button variant="secondary" size="md" onClick={() => pairing.mutate()} icon={<Link2 className="w-4 h-4" />}>Pair ScreenPipe</Button>
        </div>
        {pairing.data && (
          <div className="mb-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-gray-700 dark:border-blue-800 dark:bg-blue-950/30 dark:text-gray-200">
            Pairing code <code className="font-mono font-bold mx-1">{pairing.data.code}</code> expires {new Date(pairing.data.expires_at).toLocaleTimeString()}.
            <IconButton label="Copy pairing code" onClick={() => navigator.clipboard.writeText(pairing.data!.code)} className="ml-2"><Copy className="w-4 h-4" /></IconButton>
          </div>
        )}
        <div className="grid sm:grid-cols-2 xl:grid-cols-3 gap-3">
          {(sources.data || []).map(source => (
            <Card key={source.source_id} className="flex gap-3">
              <Monitor className="h-5 w-5 text-gray-500 dark:text-gray-400" />
              <div className="min-w-0">
                <div className="truncate font-medium text-gray-900 dark:text-gray-100">{source.name}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400">{source.provider} · {source.platform}</div>
                <div className={`mt-1 text-xs ${source.status === 'online' ? 'text-green-600 dark:text-green-400' : source.status === 'error' ? 'text-red-600 dark:text-red-400' : 'text-gray-500 dark:text-gray-400'}`}>{source.status}{source.last_seen_at ? ` · ${timeAgo(source.last_seen_at)}` : ''}</div>
              </div>
            </Card>
          ))}
          {!sources.isLoading && !sources.data?.length && <div className="rounded-lg border border-dashed border-gray-300 p-4 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">No capture sources paired.</div>}
        </div>
      </section>

      <section>
        <div className="mb-4 flex items-center gap-2"><h2 className="font-semibold text-gray-900 dark:text-gray-100">Activity</h2>{timeline.isFetching && <RefreshCw className="w-4 h-4 animate-spin text-gray-400" />}</div>
        <div className="relative ml-3 space-y-4">
          {!!orderedItems.length && (
            <span
              aria-hidden="true"
              className="absolute bottom-0 left-0 top-0 flex w-1.5 flex-col overflow-hidden rounded-full ring-1 ring-black/10 dark:ring-white/15"
            >
              {TIME_RIBBON_SEGMENTS[order].map((segment, index) => (
                <span
                  key={`${order}-${index}`}
                  className={`min-h-0 flex-1 bg-gradient-to-b ${segment}`}
                />
              ))}
            </span>
          )}
          {orderedItems.map(item => (
            <article
              key={item.id}
              data-captured-at={item.captured_at}
              className="relative ml-7 rounded-lg border border-gray-200 p-4 dark:border-gray-700"
            >
              <span
                className={`absolute -left-[2.6rem] top-4 rounded-full p-1.5 text-white ring-2 ring-white dark:ring-gray-800 ${timeOfDayClass(item.captured_at)}`}
              >
                <ItemIcon item={item} />
              </span>
              <div className="text-xs text-gray-500 dark:text-gray-400">
                {new Date(item.captured_at).toLocaleTimeString()} {item.ended_at && `– ${new Date(item.ended_at).toLocaleTimeString()}`}
                {Date.now() - Date.parse(item.ended_at || item.captured_at) < 86_400_000 && <span className="ml-2 text-gray-400">· {timeAgo(item.ended_at || item.captured_at)}</span>}
              </div>
              <h3 className="mt-1 font-medium text-gray-900 dark:text-gray-100">{item.kind === 'audio' ? 'Audio capture' : item.metadata.app_name || item.metadata.window_name || (item.kind === 'activity' ? 'Screen change' : item.kind === 'observation' ? 'Screen observation' : item.kind)}</h3>
              {item.kind === 'audio' && <p className="text-sm text-gray-500 dark:text-gray-400">{formatDuration(item)} · {item.metadata.chunk_count} chunks{item.metadata.directions?.length ? ` · ${item.metadata.directions.join(' + ')}` : ''}</p>}
              {item.kind === 'observation' && <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{item.lifecycle === 'open' ? 'Open' : formatDuration(item)} · {item.samples?.length || 0} context sample{item.samples?.length === 1 ? '' : 's'}{item.curation ? ` · ${item.curation}` : ''}{item.related_conversation_ids?.length ? ` · ${item.related_conversation_ids.length} audio link${item.related_conversation_ids.length === 1 ? '' : 's'}` : ''}</p>}
              {item.metadata.window_name && item.metadata.window_name !== item.metadata.app_name && <p className="truncate text-sm text-gray-500 dark:text-gray-400">{item.metadata.window_name}</p>}
              {item.kind === 'activity' && item.metadata.text && <p className="text-sm text-gray-600 dark:text-gray-300 mt-2 line-clamp-3">{item.metadata.text}</p>}
              {item.kind === 'observation' && item.samples?.length ? <p className="mt-2 line-clamp-3 text-sm text-gray-600 dark:text-gray-300">{item.samples[item.samples.length - 1].text}</p> : null}
              {item.kind === 'observation' && item.vault_paths?.length ? <p className="mt-2 text-xs text-green-600 dark:text-green-400">Vault: {item.vault_paths.join(', ')}</p> : null}
              <TimelineThumbnail item={item} />
            </article>
          ))}
          {!timeline.isLoading && !orderedItems.length && <div className="ml-6 rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">Nothing captured for this day.</div>}
        </div>
      </section>
    </div>
  )
}
