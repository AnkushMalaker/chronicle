import { SelectHTMLAttributes, ReactNode, forwardRef } from 'react'
import clsx from 'clsx'

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  children: ReactNode
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { children, className, ...rest },
  ref
) {
  return (
    <select
      ref={ref}
      className={clsx(
        'w-full cursor-pointer rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900',
        'focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500',
        'disabled:cursor-not-allowed disabled:opacity-60',
        'dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100',
        className
      )}
      {...rest}
    >
      {children}
    </select>
  )
})
