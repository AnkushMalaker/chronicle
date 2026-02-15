import { useMemo } from 'react'

interface Speaker {
  speaker_id: string
  name: string
}

/**
 * Shared sorting logic for speaker lists.
 * Returns speakers split into { recent, rest } where:
 *  - recent: speakers from recentSpeakers list, in recency order (most recent first)
 *  - rest: remaining speakers sorted alphabetically
 * Both groups are filtered by the search query (case-insensitive contains).
 */
export function useSortedSpeakers(
  enrolledSpeakers: Speaker[],
  searchQuery: string,
  recentSpeakers: string[] = [],
) {
  return useMemo(() => {
    let speakers = [...enrolledSpeakers]

    if (searchQuery) {
      const q = searchQuery.toLowerCase()
      speakers = speakers.filter(s => s.name.toLowerCase().includes(q))
    }

    const recentSet = new Set(recentSpeakers)
    const recent: Speaker[] = []
    const rest: Speaker[] = []

    for (const speaker of speakers) {
      if (recentSet.has(speaker.name)) {
        recent.push(speaker)
      } else {
        rest.push(speaker)
      }
    }

    recent.sort((a, b) => recentSpeakers.indexOf(a.name) - recentSpeakers.indexOf(b.name))
    rest.sort((a, b) => a.name.localeCompare(b.name))

    return { recent, rest }
  }, [enrolledSpeakers, searchQuery, recentSpeakers])
}
