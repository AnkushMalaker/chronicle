import { CheckCircle2, GitMerge, Loader2, PackageOpen, Trash2, VolumeX } from 'lucide-react'

interface Props {
  total: number
  selectedCount: number
  mergeEligible: boolean
  // Conversations still lacking cached VAD analysis; null = not loaded yet.
  unanalyzedCount: number | null
  analyzing: boolean
  archiving: boolean
  onAnalyze: () => void
  onMerge: () => void
  onArchive: () => void
  onExport: () => void
}

export default function AuditToolbar({
  total,
  selectedCount,
  mergeEligible,
  unanalyzedCount,
  analyzing,
  archiving,
  onAnalyze,
  onMerge,
  onArchive,
  onExport,
}: Props) {
  const nothingToAnalyze = unanalyzedCount === 0 && !analyzing
  return (
    <div className="flex items-center justify-between">
      <div className="text-sm text-gray-500 dark:text-gray-400">
        {total} match{total === 1 ? '' : 'es'} · {selectedCount} selected
      </div>
      <div className="flex items-center space-x-2">
        <button
          onClick={onAnalyze}
          disabled={analyzing || nothingToAnalyze}
          className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          title={
            nothingToAnalyze
              ? 'All conversations already have cached VAD analysis'
              : 'Run VAD over conversations without cached analysis'
          }
        >
          {analyzing ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : nothingToAnalyze ? (
            <CheckCircle2 className="h-4 w-4" />
          ) : (
            <VolumeX className="h-4 w-4" />
          )}
          <span>
            {analyzing
              ? 'Analyzing…'
              : nothingToAnalyze
                ? 'Audio analyzed'
                : unanalyzedCount != null
                  ? `Analyze audio (${unanalyzedCount})`
                  : 'Analyze audio'}
          </span>
        </button>
        <button
          onClick={onExport}
          className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          title="Export speech-cropped clips + transcripts for annotation"
        >
          <PackageOpen className="h-4 w-4" />
          <span>Export…</span>
        </button>
        <button
          onClick={onMerge}
          disabled={!mergeEligible}
          className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          title={
            mergeEligible
              ? 'Merge the selected adjacent conversations'
              : 'Select 2+ conversations from the same device to merge'
          }
        >
          <GitMerge className="h-4 w-4" />
          <span>Merge selected</span>
        </button>
        <button
          onClick={onArchive}
          disabled={selectedCount === 0 || archiving}
          className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {archiving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
          <span>Archive selected</span>
        </button>
      </div>
    </div>
  )
}
