import * as React from 'react'
/** Checkbox with inline label + optional faint hint (as used for "Also show auto-identified segments"). */
export interface CheckboxProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'style'> {
  label?: React.ReactNode
  hint?: React.ReactNode
  style?: React.CSSProperties
}
export function Checkbox(props: CheckboxProps): JSX.Element
