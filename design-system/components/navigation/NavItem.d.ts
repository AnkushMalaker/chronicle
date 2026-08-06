import * as React from 'react'
/** Sidebar navigation row. `active` fills blue; optional count `badge` (e.g. unacked errors). */
export interface NavItemProps extends React.AnchorHTMLAttributes<HTMLAnchorElement> {
  icon?: React.ReactNode
  label: React.ReactNode
  active?: boolean
  badge?: React.ReactNode
}
export function NavItem(props: NavItemProps): JSX.Element
