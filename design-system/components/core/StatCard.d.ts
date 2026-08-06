import * as React from 'react'
/** Compact metric tile: big coloured number over a muted label. Grid these 2–4 across. */
export interface StatCardProps extends React.HTMLAttributes<HTMLDivElement> {
  value: React.ReactNode
  label: string
  /** Number colour. @default 'neutral' */
  tone?: 'amber' | 'green' | 'red' | 'blue' | 'neutral'
}
export function StatCard(props: StatCardProps): JSX.Element
