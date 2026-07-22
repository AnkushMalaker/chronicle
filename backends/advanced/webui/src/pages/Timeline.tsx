import { useMemo, useState } from 'react'
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
          {(timeline.data || []).map(item => (
            <article key={item.id} className="relative ml-6 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
              <span className="absolute -left-[2.3rem] top-4 rounded-full bg-blue-600 text-white p-1.5"><ItemIcon item={item} /></span>
              <div className="text-xs text-gray-500">{new Date(item.captured_at).toLocaleTimeString()} {item.ended_at && `– ${new Date(item.ended_at).toLocaleTimeString()}`}</div>
              <h3 className="font-medium mt-1">{item.metadata.app_name || item.metadata.window_name || item.metadata.text || item.kind}</h3>
              {item.metadata.window_name && item.metadata.window_name !== item.metadata.app_name && <p className="text-sm text-gray-500 truncate">{item.metadata.window_name}</p>}
            </article>
          ))}
          {!timeline.isLoading && !timeline.data?.length && <div className="ml-6 text-sm text-gray-500 border border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6">Nothing captured for this day.</div>}
        </div>
      </section>
    </div>
  )
}
