import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CheckCircle2, ChevronDown, ChevronRight, Clock3, Layers3 } from 'lucide-react'
import { TimelineEpisode, timelineApi } from '../../services/api'

function durationLabel(startedAt: string, endedAt: string) {
  const seconds = Math.max(0, Math.round((Date.parse(endedAt) - Date.parse(startedAt)) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

function EpisodeThumbnail({ episode }: { episode: TimelineEpisode }) {
  const thumbnail = useQuery({
    queryKey: ['timeline-episode-thumbnail', episode.episode_id],
    queryFn: async () => (await timelineApi.getThumbnail(episode.episode_id)).data,
    enabled: episode.has_thumbnail,
    staleTime: Infinity,
  })
  const url = useMemo(() => thumbnail.data ? URL.createObjectURL(thumbnail.data) : null, [thumbnail.data])
  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])
  if (!url) return null
  return <img src={url} alt="Representative evidence for this episode" className="mt-4 max-h-72 w-auto max-w-full rounded-lg object-contain" />
}

export default function EpisodeCard({ episode, nested = false }: { episode: TimelineEpisode; nested?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const start = new Date(episode.started_at)
  const end = new Date(episode.ended_at)

  return (
    <article className={`rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-800 ${nested ? 'ml-6 border-l-4 border-l-gray-300 dark:border-l-gray-600' : ''}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <Clock3 className="h-3.5 w-3.5" />
            <time>{start.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>
            <span>–</span>
            <time>{end.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>
            <span>· {durationLabel(episode.started_at, episode.ended_at)}</span>
          </div>
          <h3 className="mt-1.5 text-lg font-semibold text-gray-900 dark:text-gray-100">
            <Link
              to={`/timeline/${episode.episode_id}`}
              className="rounded outline-none hover:text-blue-700 focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:text-blue-400"
            >
              {episode.title}
            </Link>
          </h3>
        </div>
        <div className="text-right text-xs text-gray-500 dark:text-gray-400">
          <div className="flex items-center justify-end gap-1.5">
            {/* `confirmed_at`, not `status`: only a person's edit sets the timestamp. */}
            {episode.confirmed_at && (
              <CheckCircle2 className="h-3.5 w-3.5 text-green-600 dark:text-green-400" aria-label="Confirmed" />
            )}
            {episode.kind.replace(/_/g, ' ')}
          </div>
          <div>{episode.activity_mode} · {Math.round(episode.confidence * 100)}% confidence</div>
        </div>
      </div>
      {episode.summary && <p className="mt-2 max-w-3xl text-sm leading-6 text-gray-600 dark:text-gray-300">{episode.summary}</p>}
      <EpisodeThumbnail episode={episode} />
      {!!episode.evidence.length && (
        <div className="mt-4 border-t border-gray-100 pt-3 dark:border-gray-700">
          <button
            type="button"
            onClick={() => setExpanded(value => !value)}
            aria-expanded={expanded}
            className="flex min-h-10 items-center gap-2 rounded-md px-1 text-sm font-medium text-gray-600 outline-none hover:text-gray-900 focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-300 dark:hover:text-gray-100"
          >
            {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            <Layers3 className="h-4 w-4" />
            {episode.evidence.length} evidence item{episode.evidence.length === 1 ? '' : 's'}
          </button>
          {expanded && (
            <div className="mt-2 space-y-3 pl-6">
              {episode.assertions.map((assertion, index) => (
                <div key={`${assertion.claim}-${index}`} className="text-sm text-gray-700 dark:text-gray-200">
                  {assertion.claim}
                  <span className="ml-2 text-xs text-gray-400">{assertion.role} · {Math.round(assertion.confidence * 100)}%</span>
                </div>
              ))}
              {episode.evidence.filter(item => item.excerpt).map(item => (
                <blockquote key={item.evidence_id} className="border-l-2 border-gray-200 pl-3 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
                  <div className="mb-1 text-xs">{item.kind} · {item.role}</div>
                  <p className="line-clamp-4">{item.excerpt}</p>
                </blockquote>
              ))}
            </div>
          )}
        </div>
      )}
    </article>
  )
}
