import { HTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Filled surface with soft elevation (header/sidebar/main panel). Default is a bordered, transparent tile. */
  raised?: boolean
  /** Apply the standard inner padding (p-4). Set false for flush content (e.g. tables). Default true. */
  padded?: boolean
  children: ReactNode
}

/**
 * Chronicle Espresso surface. `raised` gives the elevated card look
 * (bg-surface-raised + shadow); otherwise it's a bordered tile on the page.
 */
export function Card({ raised, padded = true, children, className, ...rest }: CardProps) {
  return (
    <div
      className={clsx(
        'rounded-lg border border-gray-200 dark:border-gray-700',
        raised && 'bg-white shadow-sm dark:bg-gray-800',
        padded && 'p-4',
        className
      )}
      {...rest}
    >
      {children}
    </div>
  )
}
