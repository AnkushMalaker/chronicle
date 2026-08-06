import * as React from 'react'
/** Bordered surface/panel. `raised` fills with the elevated surface colour; default is a bordered region on an already-raised parent. */
export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  raised?: boolean
  /** CSS padding. @default var(--pad-card) */
  padding?: string
  children?: React.ReactNode
}
export function Card(props: CardProps): JSX.Element
