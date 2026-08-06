import React from 'react'

export function CollapsibleSection({ icon, title, meta, defaultOpen = false, children, style }) {
  const [open, setOpen] = React.useState(defaultOpen)
  return React.createElement('div', { style: { border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', ...style } },
    React.createElement('button', {
      onClick: () => setOpen(!open),
      style: { width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '11px 16px', background: 'transparent', border: 'none', cursor: 'pointer', textAlign: 'left' },
    },
      React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        React.createElement('span', { style: { color: 'var(--text-faint)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform var(--dur) var(--ease)', display: 'inline-flex' } }, '›'),
        icon, React.createElement('span', { style: { fontSize: 'var(--text-sm)', fontWeight: 500, color: 'var(--text-primary)' } }, title)),
      meta && React.createElement('span', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-muted)' } }, meta)),
    open && React.createElement('div', { style: { padding: '0 16px 16px', borderTop: '1px solid var(--border)', paddingTop: '12px' } }, children))
}
