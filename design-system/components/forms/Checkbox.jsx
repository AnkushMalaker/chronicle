import React from 'react'

export function Checkbox({ label, hint, checked, onChange, style, ...rest }) {
  return React.createElement('label', {
    style: { display: 'inline-flex', alignItems: 'center', gap: '8px', fontSize: 'var(--text-sm)', color: 'var(--text-secondary)', cursor: 'pointer', ...style },
  },
    React.createElement('input', { type: 'checkbox', checked, onChange, style: { accentColor: 'var(--accent)', width: '15px', height: '15px' }, ...rest }),
    label && React.createElement('span', null, label),
    hint && React.createElement('span', { style: { color: 'var(--text-faint)' } }, hint))
}
