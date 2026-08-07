import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  AudioLines,
  CheckCircle2,
  Clock3,
  ExternalLink,
  Image as ImageIcon,
  MonitorPlay,
  Pause,
  Pencil,
  Play,
  Users,
  VideoOff,
} from 'lucide-react'
import {
  TimelineEpisode,
  TimelineEvidenceRef,
  conversationsApi,
  speakerApi,
  timelineApi,
} from '../services/api'
import { useConversationDetail } from '../hooks/useConversations'
import { useGaplessPlayer } from '../hooks/useGaplessPlayer'
import { Range } from '../lib/gaplessPlayer'
import TranscriptEditor, { Segment } from '../components/transcript/TranscriptEditor'
import { Button, Card, StateBadge } from '../components/ui'

/** Absolute wall-clock seconds for a value the API may send as ISO text or epoch. */
function epochSeconds(value: string | number | undefined | null): number | null {
  if (value == null) return null
  if (typeof value === 'number') return value
  const normalized =
    value.endsWith('Z') || value.includes('+') || /T.*-\d\d:?\d\d$/.test(value)
      ? value
      : `${value}Z`
  const parsed = Date.parse(normalized)
  return Number.isNaN(parsed) ? null : parsed / 1000
}

function durationLabel(startedAt: string, endedAt: string) {
  const seconds = Math.max(0, Math.round((Date.parse(endedAt) - Date.parse(startedAt)) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function clockRange(startedAt: string, endedAt: string) {
  const options: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' }
  return `${new Date(startedAt).toLocaleTimeString([], options)} – ${new Date(endedAt).toLocaleTimeString([], options)}`
}

/**
 * A human-readable name for one piece of evidence.
 *
 * Never falls back to the raw evidence id: episodes published before evidence metadata
 * was persisted have none, and `observation:14696` tells a reader nothing. The excerpt
 * is assembled as `app · window · text …`, so its leading segments are the next best
 * identity when metadata is absent.
 */
function describeEvidence(ref: TimelineEvidenceRef): { label: string; detail: string } {
  const parts = (ref.excerpt ?? '')
    .split(' · ')
    .map(part => part.trim())
    .filter(Boolean)

  const app = typeof ref.metadata?.app_name === 'string' ? ref.metadata.app_name.trim() : ''
  const window =
    typeof ref.metadata?.window_name === 'string' ? ref.metadata.window_name.trim() : ''
  const named = [app, window].filter(Boolean)

  // The excerpt is assembled as `app · window · text …`, so whichever segments the label
  // already shows are dropped from the detail rather than repeated under it.
  const identity = named.length ? named : parts.slice(0, 2)
  const consumed = parts.findIndex(part => !identity.includes(part))
  const detail = (consumed === -1 ? [] : parts.slice(consumed)).join(' · ')

  return {
    label: identity.join(' — ') || ref.kind.replace(/_/g, ' '),
    detail,
  }
}

/** Only a person's edit sets `confirmed_at`; `status` defaulted to "confirmed" historically. */
function isHumanConfirmed(episode: Pick<TimelineEpisode, 'confirmed_at'>) {
  return !!episode.confirmed_at
}

function Section({
  icon,
  title,
  hint,
  children,
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline gap-2">
        <span className="text-gray-500 dark:text-gray-400">{icon}</span>
        <h2 className="font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
        {hint && <span className="text-xs text-gray-500 dark:text-gray-400">{hint}</span>}
      </div>
      {children}
    </section>
  )
}

/**
 * Play the episode straight through, across every recording it cites.
 *
 * An episode is one event, but continuous capture is stored as bounded compute spans,
 * so an hour-long standup is commonly three recordings. Listening to it a fragment at a
 * time is not listening to the standup. This assembles one program: each cited recording
 * in wall-clock order, clipped to the part that falls inside the episode, handed to the
 * gapless scheduler so the joins are inaudible.
 *
 * A recording with no resolvable position (missing timestamp, no audio) is skipped and
 * reported, because a silently shorter program reads as a complete one.
 */
function EpisodePlayback({
  episode,
  conversationIds,
}: {
  episode: TimelineEpisode
  conversationIds: string[]
}) {
  const player = useGaplessPlayer()
  const results = useQueries({
    queries: conversationIds.map(id => ({
      queryKey: ['conversation', id],
      queryFn: () => conversationsApi.getById(id).then(r => r.data.conversation),
    })),
  })

  const loading = results.some(r => r.isLoading)

  const { ranges, skipped } = useMemo(() => {
    const episodeStart = Date.parse(episode.started_at) / 1000
    const episodeEnd = Date.parse(episode.ended_at) / 1000
    const usable: Array<{ range: Range; at: number }> = []
    let missing = 0

    results.forEach((result, index) => {
      const conversation = result.data as
        | { created_at?: string | number; audio_total_duration?: number; audio_chunks_count?: number }
        | undefined
      const base = epochSeconds(conversation?.created_at)
      const duration = conversation?.audio_total_duration
      if (base == null || !duration || !conversation?.audio_chunks_count) {
        if (result.isSuccess) missing += 1
        return
      }
      // Clip the recording to the slice that lies inside the episode.
      const start = Math.max(0, episodeStart - base)
      const end = Math.min(duration, episodeEnd - base)
      if (!(end > start)) {
        missing += 1
        return
      }
      usable.push({ range: { cid: conversationIds[index], start, end }, at: base + start })
    })

    // Wall-clock order, so the program plays the event forwards regardless of the
    // order the agent happened to cite its evidence in.
    usable.sort((a, b) => a.at - b.at)
    return { ranges: usable.map(item => item.range), skipped: missing }
  }, [results, episode.started_at, episode.ended_at, conversationIds])

  const isThisEpisode = player.activeConversationId === episode.episode_id
  const active = isThisEpisode && (player.isPlaying || player.isPaused)

  if (loading) {
    return <Card className="text-sm text-gray-500 dark:text-gray-400">Loading audio…</Card>
  }
  if (ranges.length === 0) {
    return null
  }

  return (
    <Card className="flex flex-wrap items-center gap-3">
      <Button
        onClick={() => {
          // Deliberately not togglePlay: that rebuilds the program as one range over
          // the "conversation" it is given, which for an episode id is not a recording
          // at all — every audio fetch would 404. Pause/resume keep the program intact.
          if (!active) player.playProgram(episode.episode_id, ranges)
          else if (player.isPaused) player.resume()
          else player.pause()
        }}
        icon={
          isThisEpisode && player.isPlaying ? (
            <Pause className="h-4 w-4" />
          ) : (
            <Play className="h-4 w-4" />
          )
        }
      >
        {isThisEpisode && player.isPlaying ? 'Pause' : 'Play whole episode'}
      </Button>
      <span className="text-sm text-gray-600 dark:text-gray-300">
        {durationLabel(episode.started_at, episode.ended_at)} across{' '}
        {ranges.length === 1 ? '1 recording' : `${ranges.length} recordings`}
        {player.buffering && isThisEpisode && ' · buffering…'}
      </span>
      {skipped > 0 && (
        <span className="text-xs text-amber-600 dark:text-amber-400">
          {skipped} cited {skipped === 1 ? 'recording has' : 'recordings have'} no playable
          audio and {skipped === 1 ? 'is' : 'are'} not included.
        </span>
      )}
    </Card>
  )
}

/**
 * One cited recording, opened in place with the episode's slice foregrounded.
 *
 * The player and transcript editor are the same ones the recording page uses — an
 * episode is a view onto the artifact, so the interface travels with the evidence
 * rather than being rebuilt per episode kind.
 */
function EpisodeRecording({
  conversationId,
  episode,
  enrolledSpeakers,
}: {
  conversationId: string
  episode: TimelineEpisode
  enrolledSpeakers: Array<{ speaker_id: string; name: string }>
}) {
  const { data, isLoading, refetch } = useConversationDetail(conversationId)
  const conversation = data as
    | {
        conversation_id?: string
        title?: string
        created_at?: string | number
        segments?: Segment[]
        audio_chunks_count?: number
        audio_total_duration?: number
        speaker_recognition?: any
      }
    | undefined

  const focusWindow = useMemo(() => {
    const base = epochSeconds(conversation?.created_at)
    if (base == null) return undefined
    const start = Date.parse(episode.started_at) / 1000 - base
    const end = Date.parse(episode.ended_at) / 1000 - base
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return undefined
    return { start: Math.max(0, start), end }
  }, [conversation?.created_at, episode.started_at, episode.ended_at])

  if (isLoading) {
    return <Card className="text-sm text-gray-500 dark:text-gray-400">Loading recording…</Card>
  }
  if (!conversation?.conversation_id) {
    return (
      <Card className="text-sm text-gray-500 dark:text-gray-400">
        This episode cites recording <code className="font-mono">{conversationId}</code>, which is
        no longer available.
      </Card>
    )
  }

  const openHref = focusWindow
    ? `/recordings/${conversationId}?start=${Math.floor(focusWindow.start)}&end=${Math.ceil(focusWindow.end)}`
    : `/recordings/${conversationId}`

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0 text-sm font-medium text-gray-900 dark:text-gray-100">
          {conversation.title || 'Untitled recording'}
        </div>
        <Link
          to={openHref}
          state={{ from: `/timeline/${episode.episode_id}` }}
          className="inline-flex min-h-10 items-center gap-1.5 rounded-md px-2 text-sm font-medium text-blue-600 outline-none hover:text-blue-700 focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400"
        >
          Open full recording <ExternalLink className="h-3.5 w-3.5" />
        </Link>
      </div>
      {focusWindow && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Showing the whole recording with this episode's window highlighted.
        </p>
      )}
      <TranscriptEditor
        conversationId={conversation.conversation_id}
        segments={conversation.segments ?? []}
        duration={conversation.audio_total_duration}
        hasAudio={!!conversation.audio_chunks_count && conversation.audio_chunks_count > 0}
        showWaveform={false}
        focusWindow={focusWindow}
        enrolledSpeakers={enrolledSpeakers}
        speakerRecognition={conversation.speaker_recognition}
        onChanged={refetch}
      />
    </Card>
  )
}

function EpisodeThumbnail({ episode }: { episode: TimelineEpisode }) {
  const thumbnail = useQuery({
    queryKey: ['timeline-episode-thumbnail', episode.episode_id],
    queryFn: async () => (await timelineApi.getThumbnail(episode.episode_id)).data,
    enabled: episode.has_thumbnail,
    staleTime: Infinity,
  })
  const url = useMemo(
    () => (thumbnail.data ? URL.createObjectURL(thumbnail.data) : null),
    [thumbnail.data]
  )
  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])
  if (!url) return null
  return (
    <img
      src={url}
      alt="Representative evidence for this episode"
      className="max-h-96 w-auto max-w-full rounded-lg object-contain"
    />
  )
}

export default function EpisodeDetail() {
  const { episodeId } = useParams<{ episodeId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftSummary, setDraftSummary] = useState('')
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<
    Array<{ speaker_id: string; name: string }>
  >([])

  useEffect(() => {
    speakerApi.getEnrolledSpeakers()
      .then(res => setEnrolledSpeakers(res.data.speakers || []))
      .catch(() => {})
  }, [])

  const episodeQuery = useQuery({
    queryKey: ['timeline-episode', episodeId],
    queryFn: async () => (await timelineApi.getEpisode(episodeId!)).data,
    enabled: !!episodeId,
  })
  const episode = episodeQuery.data

  const save = useMutation({
    mutationFn: () =>
      timelineApi.updateEpisode(episodeId!, { title: draftTitle, summary: draftSummary }),
    onSuccess: response => {
      queryClient.setQueryData(['timeline-episode', episodeId], response.data)
      queryClient.invalidateQueries({ queryKey: ['semantic-timeline'] })
      setEditing(false)
    },
  })

  const evidenceByKind = useMemo(() => {
    const grouped = new Map<TimelineEvidenceRef['kind'], TimelineEvidenceRef[]>()
    for (const ref of episode?.evidence ?? []) {
      grouped.set(ref.kind, [...(grouped.get(ref.kind) ?? []), ref])
    }
    return grouped
  }, [episode?.evidence])

  // Audio and transcript evidence both point at a recording; one section per recording.
  const citedRecordingIds = useMemo(() => {
    const ids = (episode?.evidence ?? [])
      .filter(ref => ref.kind === 'audio_span' || ref.kind === 'transcript')
      .map(ref => ref.metadata?.conversation_id)
      .filter((id): id is string => typeof id === 'string' && !!id)
    return Array.from(new Set(ids))
  }, [episode?.evidence])

  // Playback covers the episode's whole span, so it uses every recording that
  // overlaps it rather than only the ones cited as evidence — a long call is often
  // cited through the single recording carrying its most quotable stretch.
  const playbackRecordingIds = useMemo(() => {
    const spanned = episode?.audio_recording_ids ?? []
    return spanned.length ? spanned : citedRecordingIds
  }, [episode?.audio_recording_ids, citedRecordingIds])

  if (episodeQuery.isLoading) {
    return <div className="text-sm text-gray-500 dark:text-gray-400">Loading episode…</div>
  }
  if (!episode) {
    return (
      <div className="space-y-4">
        <Button variant="secondary" onClick={() => navigate('/timeline')} icon={<ArrowLeft className="h-4 w-4" />}>
          Back to Timeline
        </Button>
        <div className="rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
          This episode no longer exists. Reanalyzing a day replaces its unconfirmed episodes.
        </div>
      </div>
    )
  }

  const observations = evidenceByKind.get('observation') ?? []
  const frames = evidenceByKind.get('frame') ?? []
  const photos = evidenceByKind.get('immich') ?? []
  const meetings = evidenceByKind.get('meeting') ?? []
  const gaps = evidenceByKind.get('capture_gap') ?? []

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => navigate('/timeline')}
          icon={<ArrowLeft className="h-4 w-4" />}
        >
          Back to Timeline
        </Button>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
          <span className="inline-flex items-center gap-1.5">
            <Clock3 className="h-3.5 w-3.5" />
            {clockRange(episode.started_at, episode.ended_at)}
          </span>
          <span>· {durationLabel(episode.started_at, episode.ended_at)}</span>
          <span>· {episode.kind.replace(/_/g, ' ')}</span>
          <span>· {episode.activity_mode}</span>
          <span>· {episode.salience}</span>
          <span>· {Math.round(episode.confidence * 100)}% confidence</span>
          {isHumanConfirmed(episode) && (
            <StateBadge tone="success" className="ml-1">
              <CheckCircle2 className="mr-1 h-3 w-3" /> Confirmed
            </StateBadge>
          )}
        </div>

        {editing ? (
          <div className="space-y-2">
            <input
              value={draftTitle}
              onChange={event => setDraftTitle(event.target.value)}
              aria-label="Episode title"
              className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-xl font-bold text-gray-900 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
            />
            <textarea
              value={draftSummary}
              onChange={event => setDraftSummary(event.target.value)}
              rows={3}
              aria-label="Episode summary"
              className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
            />
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending || !draftTitle.trim()}>
                {save.isPending ? 'Saving…' : 'Save'}
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                Saving confirms this episode, so reanalyzing the day keeps it.
              </span>
            </div>
            {save.isError && (
              <p className="text-sm text-red-600 dark:text-red-400">
                Could not save this episode. {(save.error as Error)?.message}
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">{episode.title}</h1>
              {episode.summary && (
                <p className="mt-1.5 max-w-3xl text-sm leading-6 text-gray-600 dark:text-gray-300">
                  {episode.summary}
                </p>
              )}
            </div>
            <Button
              variant="secondary"
              size="sm"
              icon={<Pencil className="h-4 w-4" />}
              onClick={() => {
                setDraftTitle(episode.title)
                setDraftSummary(episode.summary)
                setEditing(true)
              }}
            >
              Edit
            </Button>
          </div>
        )}

        {!!episode.entities.length && (
          <div className="flex flex-wrap items-center gap-2">
            <Users className="h-3.5 w-3.5 text-gray-400" />
            {episode.entities.map(entity => (
              <span
                key={entity}
                className="rounded-full bg-gray-100 px-2.5 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-200"
              >
                {entity}
              </span>
            ))}
          </div>
        )}
      </header>

      {episode.has_thumbnail && <EpisodeThumbnail episode={episode} />}

      {!!episode.assertions.length && (
        <Section icon={<CheckCircle2 className="h-4 w-4" />} title="What the evidence supports">
          <ul className="space-y-2">
            {episode.assertions.map((assertion, index) => (
              <li key={`${assertion.claim}-${index}`} className="text-sm text-gray-700 dark:text-gray-200">
                {assertion.claim}
                <span className="ml-2 text-xs text-gray-400">
                  {assertion.role} · {Math.round(assertion.confidence * 100)}%
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {!!meetings.length && (
        <Section icon={<Users className="h-4 w-4" />} title="Meeting">
          {meetings.map(ref => (
            <Card key={ref.evidence_id} className="text-sm text-gray-700 dark:text-gray-200">
              {ref.excerpt || 'A meeting was detected across this interval.'}
              {typeof ref.metadata?.detection_source === 'string' && (
                <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">
                  detected via {ref.metadata.detection_source}
                </span>
              )}
            </Card>
          ))}
        </Section>
      )}

      {!!citedRecordingIds.length && (
        <Section
          icon={<AudioLines className="h-4 w-4" />}
          title={citedRecordingIds.length === 1 ? 'Recording' : 'Recordings'}
          hint="Play the event straight through, or correct one recording at a time."
        >
          <div className="space-y-4">
            <EpisodePlayback episode={episode} conversationIds={playbackRecordingIds} />
            {citedRecordingIds.map(conversationId => (
              <EpisodeRecording
                key={conversationId}
                conversationId={conversationId}
                episode={episode}
                enrolledSpeakers={enrolledSpeakers}
              />
            ))}
          </div>
        </Section>
      )}

      {!!observations.length && (
        <Section icon={<MonitorPlay className="h-4 w-4" />} title="On screen">
          <ol className="space-y-1.5">
            {observations.map(ref => {
              const { label, detail } = describeEvidence(ref)
              return (
                <li key={ref.evidence_id} className="flex gap-3 text-sm">
                  <time className="w-16 flex-shrink-0 text-xs text-gray-500 dark:text-gray-400">
                    {new Date(ref.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                  </time>
                  <div className="min-w-0">
                    <div className="truncate text-gray-800 dark:text-gray-200">{label}</div>
                    {detail && (
                      <p className="line-clamp-2 text-xs text-gray-500 dark:text-gray-400">{detail}</p>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        </Section>
      )}

      {!!(frames.length || photos.length) && (
        <Section
          icon={<ImageIcon className="h-4 w-4" />}
          title="Images"
          hint="Full-resolution frames stay on the capture node."
        >
          <ul className="space-y-1.5">
            {[...frames, ...photos].map(ref => (
              <li key={ref.evidence_id} className="flex gap-3 text-sm">
                <time className="w-16 flex-shrink-0 text-xs text-gray-500 dark:text-gray-400">
                  {new Date(ref.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                </time>
                <span className="min-w-0 truncate text-gray-800 dark:text-gray-200">
                  {describeEvidence(ref).label}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {!!gaps.length && (
        <Section icon={<VideoOff className="h-4 w-4" />} title="Not captured">
          <div className="rounded-lg border border-dashed border-gray-300 p-4 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
            {gaps.length === 1 ? 'A stretch of this episode' : `${gaps.length} stretches of this episode`} has
            no capture at all. Missing capture is a different fact from silence.
          </div>
        </Section>
      )}

      {!episode.evidence.length && (
        <div className="rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
          This episode cites no evidence.
        </div>
      )}
    </div>
  )
}
