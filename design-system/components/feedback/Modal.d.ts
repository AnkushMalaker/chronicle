import * as React from 'react'
/** Centered confirm/detail dialog with scrim. Provide `footer` buttons (e.g. Cancel + Delete audio). */
export interface ModalProps {
  open: boolean
  onClose?: () => void
  title: React.ReactNode
  icon?: React.ReactNode
  children?: React.ReactNode
  footer?: React.ReactNode
  style?: React.CSSProperties
}
export function Modal(props: ModalProps): JSX.Element | null
