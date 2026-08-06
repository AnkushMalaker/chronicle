import React from 'react'

export function Breadcrumb({ items, style }) {
  const out = []
  items.forEach((it, i) => {
    const last = i === items.length - 1
    out.push(React.createElement(last ? 'span' : 'a', {
      key: 'i' + i, onClick: it.onClick, href: it.href,
      style: { display: 'inline-flex', alignItems: 'center', gap: '5px', color: last ? 'var(--text-secondary)' : 'var(--accent-fg)', cursor: last ? 'default' : 'pointer', textDecoration: 'none' },
    }, it.icon, it.label))
    if (!last) out.push(React.createElement('span', { key: 's' + i, style: { color: 'var(--gray-600)' } }, '›'))
  })
  return React.createElement('nav', { style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: 'var(--text-sm)', color: 'var(--text-muted)', ...style } }, out)
}
