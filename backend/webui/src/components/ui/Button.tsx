import { ButtonHTMLAttributes, ReactNode, forwardRef } from 'react'
import clsx from 'clsx'

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'
export type ButtonSize = 'sm' | 'md'

const VARIANT: Record<ButtonVariant, string> = {
  primary: 'bg-blue-600 text-white hover:bg-blue-700 disabled:hover:bg-blue-600',
  secondary:
    'bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600',
  danger: 'bg-red-600 text-white hover:bg-red-700 disabled:hover:bg-red-600',
  ghost:
    'bg-transparent text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800',
}

const SIZE: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2 text-sm',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Leading icon element (e.g. a lucide-react icon). */
  icon?: ReactNode
}

/**
 * Chronicle Espresso primary control. `variant` maps to the design-system button
 * families (primary = terracotta accent, secondary = neutral chip, danger, ghost).
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'sm', icon, children, className, type = 'button', ...rest },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      className={clsx(
        'inline-flex items-center justify-center gap-1.5 rounded-lg font-medium leading-none',
        'transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50',
        'disabled:cursor-not-allowed disabled:opacity-40',
        VARIANT[variant],
        SIZE[size],
        className
      )}
      {...rest}
    >
      {icon}
      {children != null && <span>{children}</span>}
    </button>
  )
})
