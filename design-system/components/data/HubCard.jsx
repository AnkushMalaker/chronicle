import React from 'react'

export function HubCard({ icon, title, description, stat, cta, active, onClick, style }) {
  const [hover, setHover] = React.useState(false)
  return React.createElement('div', {
    onClick, onMouseEnter: () => setHover(true), onMouseLeave: () => setHover(false),
    style: {
      position: 'relative', display: 'flex', flexDirection: 'column', gap: '10px', padding: '18px',
      borderRadius: 'var(--radius-lg)', cursor: onClick ? 'pointer' : 'default',
      background: active ? 'var(--accent-nav-bg)' : 'var(--surface-sunken)',
      border: '1px solid ' + (active ? 'var(--accent)' : hover && onClick ? 'var(--blue-500)' : 'var(--border)'),
      transition: 'border-color var(--dur) var(--ease)', ...style,
    },
  },
    React.createElement('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' } },
      React.createElement('span', { style: { color: active ? 'var(--accent-fg)' : 'var(--text-muted)' } }, icon),
      stat != null && React.createElement('span', { style: { fontSize: 'var(--text-2xl)', fontWeight: 700, color: 'var(--accent-fg)' } }, stat)),
    React.createElement('div', { style: { fontSize: 'var(--text-base)', fontWeight: 600, color: 'var(--text-primary)' } }, title),
    React.createElement('div', { style: { fontSize: 'var(--text-xs)', lineHeight: 1.5, color: 'var(--text-muted)' } }, description),
    cta && React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: '5px', fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--accent-fg)' } }, cta))
}
