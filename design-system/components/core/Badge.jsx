import React from 'react'

const TONE = {
  neutral: { background: 'var(--chip-bg)',        color: 'var(--chip-fg)' },
  success: { background: 'var(--success-soft-bg)', color: 'var(--success-fg)' },
  danger:  { background: 'var(--danger-soft-bg)',  color: 'var(--danger-fg)' },
  warning: { background: 'var(--warning-soft-bg)', color: 'var(--warning-fg)' },
  info:    { background: 'var(--info-soft-bg)',    color: 'var(--info-fg)' },
  suggest: { background: 'var(--suggest-soft-bg)', color: 'var(--suggest-fg)' },
  mono:    { background: 'var(--surface-sunken)',  color: 'var(--text-muted)' },
}
export function Badge({ tone = 'neutral', icon, children, style, ...rest }) {
  const t = TONE[tone] || TONE.neutral
  return React.createElement('span', {
    ...rest,
    style: {
      display: 'inline-flex', alignItems: 'center', gap: '4px', padding: '2px 8px',
      borderRadius: 'var(--radius-sm)', fontSize: 'var(--text-xs)', lineHeight: 1.4,
      fontFamily: tone === 'mono' ? 'var(--font-mono)' : 'var(--font-sans)', ...t, ...style,
    },
  }, icon, children)
}
