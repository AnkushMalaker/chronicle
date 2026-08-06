import React from 'react'

const TONE = {
  info:    { bg: 'var(--info-soft-bg)',    fg: 'var(--info-fg)' },
  success: { bg: 'var(--success-soft-bg)', fg: 'var(--success-fg)' },
  warning: { bg: 'var(--warning-soft-bg)', fg: 'var(--warning-fg)' },
  danger:  { bg: 'var(--danger-soft-bg)',  fg: 'var(--danger-fg)' },
}
export function Alert({ tone = 'info', icon, children, style, ...rest }) {
  const t = TONE[tone] || TONE.info
  return React.createElement('div', {
    role: 'status', ...rest,
    style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 16px', borderRadius: 'var(--radius-lg)', fontSize: 'var(--text-sm)', background: t.bg, color: t.fg, ...style },
  }, icon, React.createElement('span', null, children))
}
