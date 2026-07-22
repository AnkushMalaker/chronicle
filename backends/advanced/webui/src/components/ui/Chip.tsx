import { ReactNode } from 'react'

/**
 * Muted pill for non-actionable metadata: versions, counts, provenance, kinds,
 * operation history, provider names. Metadata should look like metadata — never
 * give it an accent color. For a genuine state signal (error/warning/success),
 * use StateBadge instead.
 */
export function MetadataChip({
  children,
  title,
  className = '',
}: {
  children: ReactNode
  title?: string
  className?: string
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-500 dark:bg-gray-700/60 dark:text-gray-400 ${className}`}
    >
      {children}
    </span>
  )
}

export type StateTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'suggest' | 'mono'

// `info` uses the muted info-blue (sky) ramp, kept distinct from the terracotta brand.
const TONE: Record<StateTone, string> = {
  neutral: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  info: 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300',
  success: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  warning: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  danger: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  suggest: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
  mono: 'bg-gray-50 font-mono text-gray-500 dark:bg-gray-900 dark:text-gray-400',
}

/**
 * Badge for a genuine state signal (error, warning, success, active). Reserve
 * color for state — do not use it for descriptive metadata (use MetadataChip).
 */
export function StateBadge({
  tone = 'neutral',
  children,
  title,
  className = '',
}: {
  tone?: StateTone
  children: ReactNode
  title?: string
  className?: string
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${TONE[tone]} ${className}`}
    >
      {children}
    </span>
  )
}
