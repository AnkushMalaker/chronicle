import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { timelineApi } from '../../services/api'

function PhotoGrid({ requestId, round }: { requestId: string; round: number }) {
  const image = useQuery({ queryKey: ['photo-exploration-grid', requestId, round], queryFn: async () => (await timelineApi.getPhotoExplorationGrid(requestId, round)).data, staleTime: Infinity })
  const url = useMemo(() => image.data ? URL.createObjectURL(image.data) : null, [image.data])
  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])
  if (image.isError) return <p role="alert">Could not load this photo grid.</p>
  return url ? <img src={url} alt={`Model input photo grid, round ${round}`} className="mt-2 h-auto w-full rounded border border-[var(--tape-line)]" /> : <p>Loading photo grid…</p>
}

export default function PhotoExplorationPanel({ requestId }: { requestId: string }) {
  const [open, setOpen] = useState(false)
  const details = useQuery({ queryKey: ['photo-exploration', requestId], queryFn: async () => (await timelineApi.getPhotoExploration(requestId)).data, enabled: open })
  return <details className="mt-3 border-t border-[var(--tape-line)] pt-2" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer font-medium">Photo coverage and model input grids</summary>
    {open && details.isError && <p role="alert">Could not load photo exploration details.</p>}
    {open && details.data && <div className="mt-2 space-y-3">
      <p>{details.data.coverage.inspected_count} of {details.data.coverage.inventory_count} photos inspected · {details.data.coverage.unseen_count} unseen · {details.data.coverage.round_count} rounds</p>
      <p className="text-xs opacity-75">Stopped: {details.data.coverage.stop_reason.replace(/_/g, ' ')}. Unseen photos remain available for future inspection.</p>
      {details.data.rounds.map(round => <div key={round.round}>
        <p className="text-xs font-semibold">Round {round.round} · {round.action} · {round.offered.length} photos</p>
        <p className="text-xs">{round.question}</p>
        <PhotoGrid requestId={requestId} round={round.round} />
      </div>)}
    </div>}
  </details>
}
