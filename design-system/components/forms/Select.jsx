import React from 'react'

export function Select({ children, style, ...rest }) {
  return React.createElement('select', {
    ...rest,
    style: {
      background: 'var(--surface-sunken)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)',
      padding: '8px 12px', fontSize: 'var(--text-sm)', fontFamily: 'var(--font-sans)',
      color: 'var(--text-primary)', cursor: 'pointer', ...style,
    },
  }, children)
}
