import { HTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

export type StatTone = 'neutral' | 'amber' | 'green' | 'red' | 'blue'

const TONE: Record<StatTone, string> = {
  neutral: 'text-gray-900 dark:text-gray-100',
  amber: 'text-amber-600 dark:text-amber-400',
  green: 'text-green-600 dark:text-green-400',
  red: 'text-red-600 dark:text-red-400',
  blue: 'text-blue-600 dark:text-blue-400',
}

export interface StatCardProps extends HTMLAttributes<HTMLDivElement> {
  value: ReactNode
  label: ReactNode
  tone?: StatTone
}

/** Centered metric tile: large tone-colored value over a muted label. */
export function StatCard({ value, label, tone = 'neutral', className, ...rest }: StatCardProps) {
  return (
    <div
      className={clsx(
        'rounded-lg border border-gray-200 p-3 text-center dark:border-gray-700',
        className
      )}
      {...rest}
    >
      <div className={clsx('text-2xl font-bold', TONE[tone])}>{value}</div>
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
    </div>
  )
}
