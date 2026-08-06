import * as React from 'react'
/** Icon-only ghost button (e.g. row delete). Always give a `label` for a11y. */
export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Accessible label + tooltip. */
  label: string
  /** Turn the hover state red (destructive). */
  danger?: boolean
  children?: React.ReactNode
}
export function IconButton(props: IconButtonProps): JSX.Element
