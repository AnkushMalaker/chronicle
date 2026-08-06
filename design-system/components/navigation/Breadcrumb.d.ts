import * as React from 'react'
export interface Crumb { label: React.ReactNode; href?: string; onClick?: () => void; icon?: React.ReactNode }
/** Path trail; last item is the current page (non-interactive). Used to return from a sub-view (Wake-Word Lab) to its hub (Data Audit). */
export interface BreadcrumbProps { items: Crumb[]; style?: React.CSSProperties }
export function Breadcrumb(props: BreadcrumbProps): JSX.Element
