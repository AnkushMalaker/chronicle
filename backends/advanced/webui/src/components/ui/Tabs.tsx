import { ReactNode } from 'react'
import clsx from 'clsx'

export interface TabItem<T extends string = string> {
  value: T
  label: ReactNode
  icon?: ReactNode
}

export interface TabsProps<T extends string = string> {
  tabs: TabItem<T>[]
  value: T
  onChange?: (value: T) => void
  variant?: 'pill' | 'underline'
  className?: string
}

/** Segmented navigation. `pill` = filled terracotta active tab; `underline` = accent-underlined active tab. */
export function Tabs<T extends string = string>({
  tabs,
  value,
  onChange,
  variant = 'pill',
  className,
}: TabsProps<T>) {
  if (variant === 'underline') {
    return (
      <div
        role="tablist"
        className={clsx('flex gap-0.5 border-b border-gray-200 dark:border-gray-700', className)}
      >
        {tabs.map((t) => {
          const on = t.value === value
          return (
            <button
              key={t.value}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => onChange?.(t.value)}
              className={clsx(
                '-mb-px inline-flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm font-medium transition-colors',
                on
                  ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
              )}
            >
              {t.icon}
              {t.label}
            </button>
          )
        })}
      </div>
    )
  }
  return (
    <div role="tablist" className={clsx('flex flex-wrap gap-2', className)}>
      {tabs.map((t) => {
        const on = t.value === value
        return (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange?.(t.value)}
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-lg px-3.5 py-[7px] text-sm font-medium transition-colors',
              on
                ? 'bg-blue-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600'
            )}
          >
            {t.icon}
            {t.label}
          </button>
        )
      })}
    </div>
  )
}
