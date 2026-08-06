import * as React from 'react'
export interface TabItem { value: string; label: React.ReactNode; icon?: React.ReactNode }
/** Segmented control. `pill` (filled, for content buckets) or `underline` (for top-level view switches like Conversations / Archived). */
export interface TabsProps { tabs: TabItem[]; value: string; onChange?: (v: string) => void; variant?: 'pill' | 'underline'; style?: React.CSSProperties }
export function Tabs(props: TabsProps): JSX.Element
