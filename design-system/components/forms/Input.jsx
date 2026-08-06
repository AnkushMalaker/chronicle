import React from 'react'

export function Input({ style, ...rest }) {
  const [focus, setFocus] = React.useState(false)
  return React.createElement('input', {
    onFocus: () => setFocus(true), onBlur: () => setFocus(false), ...rest,
    style: {
      width: '100%', boxSizing: 'border-box', background: 'var(--surface-sunken)',
      border: '1px solid ' + (focus ? 'var(--accent)' : 'var(--border)'),
      borderRadius: 'var(--radius-lg)', padding: '10px 14px', fontSize: 'var(--text-sm)',
      fontFamily: 'var(--font-sans)', color: 'var(--text-primary)', outline: 'none',
      transition: 'border-color var(--dur) var(--ease)', ...style,
    },
  })
}
