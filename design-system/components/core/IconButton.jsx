import React from 'react'

export function IconButton({ children, label, danger, style, ...rest }) {
  const [hover, setHover] = React.useState(false)
  return React.createElement('button', {
    'aria-label': label, title: label, onMouseEnter: () => setHover(true), onMouseLeave: () => setHover(false), ...rest,
    style: {
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center', padding: '5px',
      border: 'none', background: hover ? 'var(--surface-sunken)' : 'transparent',
      color: hover ? (danger ? 'var(--danger)' : 'var(--text-secondary)') : 'var(--text-muted)',
      borderRadius: 'var(--radius-md)', cursor: 'pointer', transition: 'all var(--dur) var(--ease)', ...style,
    },
  }, children)
}
