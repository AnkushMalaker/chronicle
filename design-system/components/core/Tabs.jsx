import React from 'react'

export function Tabs({ tabs, value, onChange, variant = 'pill', style }) {
  if (variant === 'underline') {
    return React.createElement('div', { style: { display: 'flex', gap: '2px', borderBottom: '1px solid var(--border)', ...style } },
      tabs.map((t) => {
        const on = t.value === value
        return React.createElement('button', {
          key: t.value, onClick: () => onChange && onChange(t.value),
          style: {
            display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '8px 16px', border: 'none',
            background: 'transparent', cursor: 'pointer', fontSize: 'var(--text-sm)', fontWeight: 500,
            color: on ? 'var(--accent-fg)' : 'var(--text-muted)',
            borderBottom: '2px solid ' + (on ? 'var(--accent)' : 'transparent'), marginBottom: '-1px',
          },
        }, t.icon, t.label)
      }))
  }
  return React.createElement('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', ...style } },
    tabs.map((t) => {
      const on = t.value === value
      return React.createElement('button', {
        key: t.value, onClick: () => onChange && onChange(t.value),
        style: {
          padding: 'var(--pad-btn)', border: 'none', borderRadius: 'var(--radius-lg)', cursor: 'pointer',
          fontSize: 'var(--text-sm)', fontWeight: 500,
          background: on ? 'var(--accent)' : 'var(--chip-bg)', color: on ? '#fff' : 'var(--chip-fg)',
        },
      }, t.label)
    }))
}
