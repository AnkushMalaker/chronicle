import { useCallback, useState } from 'react'
import { ChevronDown, ChevronRight, Gauge } from 'lucide-react'
import { dataAuditApi, SpeakerConfidenceOverview } from '../../services/api'

/**
 * Strategic per-speaker confidence view for the Data Audit page.
 *
 * Reads stored identification confidence (no re-embedding) and surfaces:
 *  - the global confidence distribution (bimodal: noise hump near the
 *    threshold vs real-speaker hump higher up),
 *  - the marginal-match fraction (weak labels likely to be wrong),
 *  - per-speaker baselines so "noise magnets" (matches clustered at the floor)
 *    bubble to the top, and
 *  - a data-driven recommended similarity threshold.
 *
 * Lazy: fetches only when first expanded.
 */
export default function SpeakerConfidencePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<SpeakerConfidenceOverview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await dataAuditApi.getSpeakerConfidence()
      setData(res.data)
    } catch {
      setError('Failed to load speaker confidence stats')
    } finally {
      setLoading(false)
    }
  }, [])

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && !data && !loading) load()
  }

  const maxCount = data ? Math.max(1, ...data.histogram.counts) : 1
  const histEnd = data
    ? data.histogram.start + data.histogram.counts.length * data.histogram.bin_width
    : 1
  // Evenly-spaced x-axis ticks every 0.1 across [start, end]. The axis is
  // linear, so justify-between positions these to line up with their values.
  const xTicks: number[] = []
  if (data) {
    for (let t = data.histogram.start; t <= histEnd + 1e-9; t += 0.1) {
      xTicks.push(Number(t.toFixed(2)))
    }
  }

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg">
      <button
        onClick={toggle}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left"
      >
        <div className="flex items-center space-x-2">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <Gauge className="h-4 w-4 text-blue-600" />
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
            Speaker identification confidence
          </span>
        </div>
        {data && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {data.marginal_count}/{data.total_identified} (
            {(data.marginal_fraction * 100).toFixed(1)}%) low-confidence
          </span>
        )}
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-4 border-t border-gray-200 dark:border-gray-700 pt-3">
          {loading && <p className="text-sm text-gray-500">Computing…</p>}
          {error && <p className="text-sm text-red-600">{error}</p>}

          {data && (
            <>
              {/* Summary line */}
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
                <span className="text-gray-600 dark:text-gray-300">
                  Threshold in use: <b>{data.threshold.toFixed(2)}</b>
                </span>
                <span className="text-gray-600 dark:text-gray-300">
                  Recommended:{' '}
                  <b>
                    {data.recommended_threshold !== null
                      ? data.recommended_threshold.toFixed(2)
                      : '—'}
                  </b>
                </span>
                <span className="text-gray-600 dark:text-gray-300">
                  {data.total_identified} identifications across{' '}
                  {data.conversations_with_ids} conversations
                </span>
              </div>

              {/* Histogram */}
              <div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">
                  Confidence distribution (bin width {data.histogram.bin_width})
                </div>
                <div className="flex">
                  {/* Y-axis title */}
                  <div className="flex items-center pr-1">
                    <span
                      className="text-[10px] text-gray-400 whitespace-nowrap"
                      style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}
                    >
                      identifications
                    </span>
                  </div>
                  {/* Y-axis scale */}
                  <div className="flex flex-col items-end justify-between h-24 pr-1 text-[10px] text-gray-400 tabular-nums">
                    <span>{maxCount}</span>
                    <span>{Math.round(maxCount / 2)}</span>
                    <span>0</span>
                  </div>
                  {/* Bars + x-axis */}
                  <div className="flex-1">
                    <div className="flex items-end gap-0.5 h-24 border-l border-b border-gray-200 dark:border-gray-700">
                      {data.histogram.counts.map((c, i) => {
                        const lo = data.histogram.start + i * data.histogram.bin_width
                        const belowThr = lo + data.histogram.bin_width <= data.threshold + 1e-9
                        return (
                          <div
                            key={i}
                            className="flex-1 h-full flex flex-col justify-end items-center"
                            title={`[${lo.toFixed(2)}, ${(lo + data.histogram.bin_width).toFixed(2)}): ${c}`}
                          >
                            <div
                              className={
                                belowThr
                                  ? 'w-full bg-orange-400/70 dark:bg-orange-500/60'
                                  : 'w-full bg-blue-400/70 dark:bg-blue-500/60'
                              }
                              style={{ height: `${(c / maxCount) * 100}%` }}
                            />
                          </div>
                        )
                      })}
                    </div>
                    {/* X-axis ticks */}
                    <div className="flex justify-between text-[10px] text-gray-400 mt-0.5 tabular-nums">
                      {xTicks.map((t) => (
                        <span key={t}>{t.toFixed(2)}</span>
                      ))}
                    </div>
                    {/* X-axis title */}
                    <div className="text-[10px] text-gray-400 text-center mt-0.5">
                      similarity confidence
                    </div>
                  </div>
                </div>
                <div className="text-[10px] text-gray-400 mt-1">
                  orange = below current threshold (would become Unknown if reprocessed)
                </div>
              </div>

              {/* Survival */}
              <div className="text-xs text-gray-600 dark:text-gray-300">
                Survive if threshold raised:{' '}
                {data.survival.map((s) => (
                  <span key={s.threshold} className="mr-3">
                    @{s.threshold.toFixed(2)}:{' '}
                    <b>{((s.keep / Math.max(1, data.total_identified)) * 100).toFixed(0)}%</b>
                  </span>
                ))}
              </div>

              {/* Per-speaker table */}
              <div className="overflow-x-auto">
                <table className="min-w-full text-xs">
                  <thead className="text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <tr>
                      <th className="text-left py-1 pr-3">Speaker</th>
                      <th className="text-right px-2">segs</th>
                      <th className="text-right px-2">convs</th>
                      <th className="text-right px-2">median</th>
                      <th className="text-right px-2">mean</th>
                      <th className="text-right px-2">range</th>
                      <th className="text-right px-2">% marginal</th>
                      <th className="text-left pl-3">verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.speakers.map((s) => {
                      const magnet = s.marginal_pct >= 50
                      const weak = s.marginal_pct >= 25
                      return (
                        <tr
                          key={s.name}
                          className="border-b border-gray-100 dark:border-gray-800"
                        >
                          <td className="py-1 pr-3 font-medium text-gray-800 dark:text-gray-200">
                            {s.name}
                          </td>
                          <td className="text-right px-2">{s.nseg}</td>
                          <td className="text-right px-2">{s.nconv}</td>
                          <td className="text-right px-2">{s.median.toFixed(3)}</td>
                          <td className="text-right px-2 text-gray-500">{s.mean.toFixed(3)}</td>
                          <td className="text-right px-2 text-gray-500">
                            {s.min.toFixed(2)}–{s.max.toFixed(2)}
                          </td>
                          <td
                            className={`text-right px-2 font-medium ${
                              magnet
                                ? 'text-red-600 dark:text-red-400'
                                : weak
                                  ? 'text-orange-600 dark:text-orange-400'
                                  : 'text-gray-600 dark:text-gray-300'
                            }`}
                          >
                            {s.marginal_pct.toFixed(0)}%
                          </td>
                          <td className="pl-3">
                            {magnet ? (
                              <span className="text-red-600 dark:text-red-400">
                                noise magnet — re-enroll / raise threshold
                              </span>
                            ) : weak ? (
                              <span className="text-orange-600 dark:text-orange-400">borderline</span>
                            ) : (
                              <span className="text-green-600 dark:text-green-400">reliable</span>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
