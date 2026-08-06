import * as React from 'react'
/** Inline message banner: `info` (progress), `success`, `warning` (capped scan), `danger` (error). */
export interface AlertProps extends React.HTMLAttributes<HTMLDivElement> {
  tone?: 'info' | 'success' | 'warning' | 'danger'
  icon?: React.ReactNode
  children?: React.ReactNode
}
export function Alert(props: AlertProps): JSX.Element
