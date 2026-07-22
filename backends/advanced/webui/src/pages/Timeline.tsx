import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Activity, AppWindow, CalendarDays, Copy, Image, Link2, Monitor, RefreshCw } from 'lucide-react'
import { deviceInputApi, DeviceInputItem } from '../services/api'

function dayBounds(day: string) {
  const start = new Date(`${day}T00:00:00`)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  return [start.toISOString(), end.toISOString()] as const
}

function ItemIcon({ item }: { item: DeviceInputItem }) {
  if (item.kind === 'immich_memory') return <Image className="w-5 h-5" />
  if (item.kind === 'activity' || item.kind === 'screen_context') return <AppWindow className="w-5 h-5" />
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
  return <img src={url} alt="Screen captured during this activity" loading="lazy" className="mt-3 w-full max-h-64 object-cover rounded-md bg-gray-100 dark:bg-gray-800" />
}

const AUDIO_SESSION_GAP_MS = 90_000
const AUDIO_SESSION_MAX_MS = 30 * 60_000

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
  const [start, end] = useMemo(() => dayBounds(day), [day])
  const timeline = useQuery({
    queryKey: ['device-timeline', day],
    queryFn: async () => (await deviceInputApi.getTimeline(start, end)).data.items,
    refetchInterval: 10_000,
  })
  const sources = useQuery({ queryKey: ['device-input-sources'], queryFn: async () => (await deviceInputApi.getSources()).data.sources, refetchInterval: 30_000 })
  const pairing = useMutation({ mutationFn: async () => (await deviceInputApi.createPairingCode()).data })
  const visibleItems = useMemo(() => groupTimelineAudio(timeline.data || []), [timeline.data])

  return (
    <div className="space-y-8">
      <header className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2"><CalendarDays className="w-6 h-6 text-blue-600" />Day Ribbon</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Conversations, desktop activity and salient photo memories.</p>
        </div>
        <input type="date" value={day} onChange={event => setDay(event.target.value)} className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2" />
      </header>

      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">Sources</h2>
          <button onClick={() => pairing.mutate()} className="inline-flex items-center gap-2 text-sm px-3 py-2 rounded-md border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700"><Link2 className="w-4 h-4" />Pair ScreenPipe</button>
        </div>
        {pairing.data && (
          <div className="mb-3 rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/30 p-3 text-sm">
            Pairing code <code className="font-mono font-bold mx-1">{pairing.data.code}</code> expires {new Date(pairing.data.expires_at).toLocaleTimeString()}.
            <button onClick={() => navigator.clipboard.writeText(pairing.data!.code)} className="ml-2"><Copy className="inline w-4 h-4" /></button>
          </div>
        )}
        <div className="grid sm:grid-cols-2 xl:grid-cols-3 gap-3">
          {(sources.data || []).map(source => (
            <div key={source.source_id} className="rounded-lg border border-gray-200 dark:border-gray-700 p-4 flex gap-3">
              <Monitor className="w-5 h-5 text-gray-500" />
              <div className="min-w-0"><div className="font-medium truncate">{source.name}</div><div className="text-xs text-gray-500">{source.provider} · {source.platform}</div><div className={`text-xs mt-1 ${source.status === 'online' ? 'text-green-600' : source.status === 'error' ? 'text-red-600' : 'text-gray-500'}`}>{source.status}{source.last_seen_at ? ` · ${new Date(source.last_seen_at).toLocaleTimeString()}` : ''}</div></div>
            </div>
          ))}
          {!sources.isLoading && !sources.data?.length && <div className="text-sm text-gray-500 border border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-4">No capture sources paired.</div>}
        </div>
      </section>

      <section>
        <div className="flex items-center gap-2 mb-4"><h2 className="font-semibold">Activity</h2>{timeline.isFetching && <RefreshCw className="w-4 h-4 animate-spin text-gray-400" />}</div>
        <div className="relative border-l-2 border-blue-100 dark:border-blue-900 ml-3 space-y-4">
          {visibleItems.map(item => (
            <article key={item.id} className="relative ml-6 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
              <span className="absolute -left-[2.3rem] top-4 rounded-full bg-blue-600 text-white p-1.5"><ItemIcon item={item} /></span>
              <div className="text-xs text-gray-500">{new Date(item.captured_at).toLocaleTimeString()} {item.ended_at && `– ${new Date(item.ended_at).toLocaleTimeString()}`}</div>
              <h3 className="font-medium mt-1">{item.kind === 'audio' ? 'Audio capture' : item.metadata.app_name || item.metadata.window_name || (item.kind === 'activity' ? 'Screen change' : item.kind)}</h3>
              {item.kind === 'audio' && <p className="text-sm text-gray-500">{formatDuration(item)} · {item.metadata.chunk_count} chunks{item.metadata.directions?.length ? ` · ${item.metadata.directions.join(' + ')}` : ''}</p>}
              {item.metadata.window_name && item.metadata.window_name !== item.metadata.app_name && <p className="text-sm text-gray-500 truncate">{item.metadata.window_name}</p>}
              {item.kind === 'activity' && item.metadata.text && <p className="text-sm text-gray-600 dark:text-gray-300 mt-2 line-clamp-3">{item.metadata.text}</p>}
              <TimelineThumbnail item={item} />
            </article>
          ))}
          {!timeline.isLoading && !visibleItems.length && <div className="ml-6 text-sm text-gray-500 border border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6">Nothing captured for this day.</div>}
        </div>
      </section>
    </div>
  )
}
