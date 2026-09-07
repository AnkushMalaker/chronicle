import { HTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

export type AlertTone = 'info' | 'success' | 'warning' | 'danger'

// `info` uses the muted info-blue (sky) ramp, kept distinct from the terracotta brand.
const TONE: Record<AlertTone, string> = {
  info: 'bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-300',
  success: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  warning: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  danger: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
}

export interface AlertProps extends HTMLAttributes<HTMLDivElement> {
  tone?: AlertTone
  icon?: ReactNode
  children: ReactNode
}

/** Inline status banner. Reserve tone for genuine signals. */
export function Alert({ tone = 'info', icon, children, className, ...rest }: AlertProps) {
  return (
    <div
      role="status"
      className={clsx(
        'flex items-center gap-2 rounded-lg px-4 py-2 text-sm',
        TONE[tone],
        className
      )}
      {...rest}
    >
      {icon}
      <span className="min-w-0 flex-1">{children}</span>
    </div>
  )
}
