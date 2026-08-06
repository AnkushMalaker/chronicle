import * as React from 'react'
/** Lazy expander with an icon, title and right-aligned `meta` summary (e.g. "17.6% low-confidence"). Collapsed by default. */
export interface CollapsibleSectionProps {
  icon?: React.ReactNode
  title: React.ReactNode
  meta?: React.ReactNode
  defaultOpen?: boolean
  children?: React.ReactNode
  style?: React.CSSProperties
}
export function CollapsibleSection(props: CollapsibleSectionProps): JSX.Element
