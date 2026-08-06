import * as React from 'react'
/**
 * Status chip / tag. Soft translucent fills: `success` (verifier on), `warning`
 * (collect-only), `info` (missed), `suggest` (also-fired), `danger`, `neutral`.
 * `mono` renders a code-like tag (model filenames) in the mono face.
 */
export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** @default 'neutral' */
  tone?: 'neutral' | 'success' | 'danger' | 'warning' | 'info' | 'suggest' | 'mono'
  /** Leading icon node. */
  icon?: React.ReactNode
  children?: React.ReactNode
}
export function Badge(props: BadgeProps): JSX.Element
