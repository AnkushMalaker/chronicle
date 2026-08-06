import React from 'react'

export function Card({ raised, padding = 'var(--pad-card)', children, style, ...rest }) {
  return React.createElement('div', {
    ...rest,
    style: {
      background: raised ? 'var(--surface-raised)' : 'transparent',
      border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)',
      padding, ...style,
    },
  }, children)
}
