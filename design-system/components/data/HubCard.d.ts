import * as React from 'react'
/**
 * Launcher tile for the Data Audit hub. Optional big `stat`, `active` (blue) state,
 * and `cta` row ("Open lab →"). Clickable when `onClick` is set.
 * @startingPoint section="Data" subtitle="Hub launcher tile" viewport="280x180"
 */
export interface HubCardProps {
  icon?: React.ReactNode
  title: React.ReactNode
  description?: React.ReactNode
  stat?: React.ReactNode
  cta?: React.ReactNode
  active?: boolean
  onClick?: () => void
  style?: React.CSSProperties
}
export function HubCard(props: HubCardProps): JSX.Element
