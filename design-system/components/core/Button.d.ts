import * as React from 'react'
/**
 * Primary action control. Blue `primary` for the one main action per area;
 * `secondary` (neutral chip-grey) for toolbar/secondary; `danger` for destructive;
 * `ghost` for low-emphasis. Pass a Lucide icon node via `icon`.
 * @startingPoint section="Core" subtitle="Button variants & sizes" viewport="700x120"
 */
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** @default 'secondary' */
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  /** @default 'sm' */
  size?: 'sm' | 'md'
  /** Leading icon node. */
  icon?: React.ReactNode
}
export function Button(props: ButtonProps): JSX.Element
