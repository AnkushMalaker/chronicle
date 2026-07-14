import { useQuery } from '@tanstack/react-query'
import { systemEventsApi, type SystemEventsFilter } from '../services/api'

// Admin system-event ledger (newest first), server-filtered + paginated.
export function useSystemEvents(filter: SystemEventsFilter = {}, enabled = true) {
  return useQuery({
    queryKey: ['system-events', filter],
    queryFn: () => systemEventsApi.list(filter).then(r => r.data),
    enabled,
  })
}

// Counts by severity/category/source over a window — drives the summary strip
// and the nav unacked-error badge.
export function useSystemEventsSummary(windowHours = 24, enabled = true) {
  return useQuery({
    queryKey: ['system-events-summary', windowHours],
    queryFn: () => systemEventsApi.summary(windowHours).then(r => r.data),
    enabled,
  })
}
