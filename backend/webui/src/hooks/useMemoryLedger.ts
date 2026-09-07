import { useQuery } from '@tanstack/react-query'
import { memoryApi } from '../services/api'

interface LedgerOpts {
  limit?: number
  conversation_id?: string
  user_id?: string
}

// Memory vault change ledger (newest first).
export function useMemoryLedger(opts: LedgerOpts = {}) {
  return useQuery({
    queryKey: ['memory-ledger', opts],
    queryFn: () => memoryApi.getAudit(opts).then(r => r.data),
  })
}

// Before→after diff for one entry — only fetched when an entry id is provided
// (i.e. when the user expands a row), so the list stays light.
export function useMemoryAuditDiff(entryId: string | null) {
  return useQuery({
    queryKey: ['memory-audit-diff', entryId],
    queryFn: () => memoryApi.getAuditDiff(entryId as string).then(r => r.data),
    enabled: !!entryId,
    staleTime: 5 * 60_000,
  })
}
