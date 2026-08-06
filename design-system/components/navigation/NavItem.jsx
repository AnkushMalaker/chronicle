import React from 'react'

export function NavItem({ icon, label, active, badge, style, ...rest }) {
  const [hover, setHover] = React.useState(false)
  const bg = active ? 'var(--accent)' : hover ? 'var(--surface-sunken)' : 'transparent'
  const color = active ? '#fff' : 'var(--text-secondary)'
  return React.createElement('a', {
    onMouseEnter: () => setHover(true), onMouseLeave: () => setHover(false), ...rest,
    style: {
      display: 'flex', alignItems: 'center', gap: '12px', padding: '8px 12px',
      borderRadius: 'var(--radius-md)', fontSize: 'var(--text-sm)', fontWeight: active ? 500 : 400,
      background: bg, color, textDecoration: 'none', cursor: 'pointer', ...style,
    },
  }, icon, React.createElement('span', null, label),
    badge != null && React.createElement('span', {
      style: { marginLeft: 'auto', minWidth: '20px', textAlign: 'center', borderRadius: 'var(--radius-full)', background: 'var(--danger)', color: '#fff', fontSize: 'var(--text-xs)', fontWeight: 600, padding: '2px 6px' },
    }, badge))
}
