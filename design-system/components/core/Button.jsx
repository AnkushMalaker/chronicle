import React from 'react'

const PAD = { sm: '6px 12px', md: '7px 14px' }
const VAR = {
  primary:   { background: 'var(--accent)',   color: '#fff' },
  secondary: { background: 'var(--chip-bg)',  color: 'var(--chip-fg)' },
  danger:    { background: 'var(--danger)',   color: '#fff' },
  ghost:     { background: 'transparent',     color: 'var(--text-secondary)' },
}
const HOVER = {
  primary: 'var(--accent-hover)', secondary: 'var(--gray-600)',
  danger: 'var(--red-700, #b91c1c)', ghost: 'var(--surface-sunken)',
}
export function Button({ variant = 'secondary', size = 'sm', icon, children, disabled, style, ...rest }) {
  const [hover, setHover] = React.useState(false)
  const base = VAR[variant] || VAR.secondary
  return React.createElement('button', {
    disabled, onMouseEnter: () => setHover(true), onMouseLeave: () => setHover(false), ...rest,
    style: {
      display: 'inline-flex', alignItems: 'center', gap: '6px', border: 'none',
      fontFamily: 'var(--font-sans)', fontSize: 'var(--text-sm)', fontWeight: 500, lineHeight: 1,
      borderRadius: 'var(--radius-lg)', padding: PAD[size], cursor: disabled ? 'not-allowed' : 'pointer',
      opacity: disabled ? 0.4 : 1, transition: 'background var(--dur) var(--ease)',
      ...base, background: !disabled && hover ? HOVER[variant] : base.background, ...style,
    },
  }, icon, children != null && React.createElement('span', null, children))
}
