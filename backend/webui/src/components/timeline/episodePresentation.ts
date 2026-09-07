import { TimelineEpisode } from '../../services/api'

export function isMediaKind(kind: string) {
  return kind.toLowerCase().split(/[^a-z0-9]+/).includes('media')
}

/** The backend applies evidence eligibility and the person's memory policy together. */
export function isSemanticMemoryEligible(episode: Pick<TimelineEpisode, 'memory_eligible'>) {
  return episode.memory_eligible
}

const MEDIA_ACTIVITY_PREFIX = /^(?:watching|viewing|listening\s+to|playing|discussing|showing|media(?:\s+(?:about|with))?)\s+/i

export function episodeDisplayTitle(episode: Pick<TimelineEpisode, 'kind' | 'title'>) {
  if (!isMediaKind(episode.kind)) return episode.title
  const subject = episode.title.replace(MEDIA_ACTIVITY_PREFIX, '').replace(/^[ .:-]+|[ .:-]+$/g, '')
  if (!subject) return 'Media'
  const normalized = /^[A-Z][a-z]/.test(subject)
    ? subject.charAt(0).toLowerCase() + subject.slice(1)
    : subject
  return `Media: ${normalized}`
}
