import { CheckCircle2, GitMerge, Loader2, PackageOpen, Trash2, UserCheck, VolumeX } from 'lucide-react'
import { Button } from '../ui'

interface Props {
  total: number
  selectedCount: number
  mergeEligible: boolean
  // Conversations still lacking cached VAD analysis; null = not loaded yet.
  unanalyzedCount: number | null
  analyzing: boolean
  archiving: boolean
  // Pending speaker-triage decisions and how many conversations they span.
  triagePendingCount: number
  triageConversationCount: number
  applyingTriage: boolean
  onApplyTriage: () => void
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
  triagePendingCount,
  triageConversationCount,
  applyingTriage,
  onApplyTriage,
  onAnalyze,
  onMerge,
  onArchive,
  onExport,
}: Props) {
  const nothingToAnalyze = unanalyzedCount === 0 && !analyzing
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="text-sm text-gray-500 dark:text-gray-400">
        {total} match{total === 1 ? '' : 'es'} · {selectedCount} selected
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {triagePendingCount > 0 && (
          <Button
            variant="primary"
            size="md"
            onClick={onApplyTriage}
            disabled={applyingTriage}
            title="Apply all speaker-triage decisions: relabel transcripts, enroll voiceprints, reprocess memory"
            icon={
              applyingTriage ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <UserCheck className="h-4 w-4" />
              )
            }
          >
            {applyingTriage
              ? 'Applying…'
              : `Apply triage (${triagePendingCount} across ${triageConversationCount})`}
          </Button>
        )}
        <Button
          variant="secondary"
          size="md"
          onClick={onAnalyze}
          disabled={analyzing || nothingToAnalyze}
          title={
            nothingToAnalyze
              ? 'All conversations already have cached VAD analysis'
              : 'Run VAD over conversations without cached analysis'
          }
          icon={
            analyzing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : nothingToAnalyze ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <VolumeX className="h-4 w-4" />
            )
          }
        >
          {analyzing
            ? 'Analyzing…'
            : nothingToAnalyze
              ? 'Audio analyzed'
              : unanalyzedCount != null
                ? `Analyze audio (${unanalyzedCount})`
                : 'Analyze audio'}
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onExport}
          title="Export speech-cropped clips + transcripts for annotation"
          icon={<PackageOpen className="h-4 w-4" />}
        >
          Export…
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onMerge}
          disabled={!mergeEligible}
          title={
            mergeEligible
              ? 'Merge the selected adjacent conversations'
              : 'Select 2+ conversations from the same device to merge'
          }
          icon={<GitMerge className="h-4 w-4" />}
        >
          Merge selected
        </Button>
        <button
          onClick={onArchive}
          disabled={selectedCount === 0 || archiving}
          className="flex items-center space-x-2 px-4 py-2 rounded-lg text-sm font-medium border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {archiving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
          <span>Archive selected</span>
        </button>
      </div>
    </div>
  )
}
