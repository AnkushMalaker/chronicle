import { InputHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label?: ReactNode
  /** Secondary, fainter helper text after the label. */
  hint?: ReactNode
}

/** Checkbox with an inline label; the terracotta accent colors the checked box. */
export function Checkbox({ label, hint, className, ...rest }: CheckboxProps) {
  return (
    <label
      className={clsx(
        'inline-flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300',
        rest.disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
      )}
    >
      <input
        type="checkbox"
        className={clsx('h-[15px] w-[15px] accent-blue-600', className)}
        {...rest}
      />
      {label != null && <span>{label}</span>}
      {hint != null && <span className="text-gray-400 dark:text-gray-500">{hint}</span>}
    </label>
  )
}
