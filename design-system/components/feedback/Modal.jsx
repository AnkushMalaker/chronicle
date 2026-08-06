import React from 'react'

export function Modal({ open, onClose, title, icon, children, footer, style }) {
  if (!open) return null
  return React.createElement('div', {
    onClick: onClose,
    style: { position: 'fixed', inset: 0, zIndex: 50, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,.5)', padding: '16px' },
  },
    React.createElement('div', {
      onClick: (e) => e.stopPropagation(),
      style: { width: '100%', maxWidth: '448px', background: 'var(--surface-raised)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xl)', boxShadow: 'var(--shadow-lg)', padding: '20px', display: 'flex', flexDirection: 'column', gap: '16px', ...style },
    },
      React.createElement('div', { style: { display: 'flex', alignItems: 'flex-start', gap: '8px' } }, icon,
        React.createElement('h3', { style: { margin: 0, fontSize: 'var(--text-base)', fontWeight: 600, color: 'var(--text-primary)' } }, title)),
      React.createElement('div', { style: { fontSize: 'var(--text-sm)', color: 'var(--text-muted)', lineHeight: 1.5 } }, children),
      footer && React.createElement('div', { style: { display: 'flex', justifyContent: 'flex-end', gap: '8px' } }, footer)))
}
