import { LabelHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

export interface LabelProps extends LabelHTMLAttributes<HTMLLabelElement> {
  children: ReactNode
}

/** Form field label. */
export function Label({ children, className, ...rest }: LabelProps) {
  return (
    <label
      className={clsx('block text-sm font-medium text-gray-700 dark:text-gray-300', className)}
      {...rest}
    >
      {children}
    </label>
  )
}
