import { ReactNode, useEffect } from 'react'
import clsx from 'clsx'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title?: ReactNode
  icon?: ReactNode
  /** Footer actions row (right-aligned). */
  footer?: ReactNode
  children: ReactNode
  /** Tailwind max-width class for the panel. Default `max-w-md`. */
  maxWidthClassName?: string
  className?: string
  /** Close when the user presses Escape. Default true; set false for data-entry forms or in-flight operations. */
  closeOnEscape?: boolean
  /** Close when the user clicks the backdrop. Default true. */
  closeOnBackdrop?: boolean
}

/** Centered dialog over a scrim. Closes on overlay click or Escape (both opt-out-able). */
export function Modal({
  open,
  onClose,
  title,
  icon,
  footer,
  children,
  maxWidthClassName = 'max-w-md',
  className,
  closeOnEscape = true,
  closeOnBackdrop = true,
}: ModalProps) {
  useEffect(() => {
    if (!open || !closeOnEscape) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose, closeOnEscape])

  if (!open) return null

  return (
    <div
      onClick={closeOnBackdrop ? onClose : undefined}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        className={clsx(
          'flex w-full flex-col gap-4 rounded-xl border border-gray-200 bg-white p-5 shadow-lg',
          'dark:border-gray-700 dark:bg-gray-800',
          maxWidthClassName,
          className
        )}
      >
        {title != null && (
          <div className="flex items-start gap-2">
            {icon}
            <h3 className="m-0 text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
          </div>
        )}
        <div className="text-sm text-gray-700 dark:text-gray-300">{children}</div>
        {footer != null && <div className="flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  )
}
