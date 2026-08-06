import React from 'react'

const TONE = {
  amber: 'var(--warning-fg)', green: 'var(--success-fg)',
  red: 'var(--danger-fg)', blue: 'var(--accent-fg)', neutral: 'var(--text-primary)',
}
export function StatCard({ value, label, tone = 'neutral', style, ...rest }) {
  return React.createElement('div', {
    ...rest,
    style: { border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '12px', textAlign: 'center', ...style },
  },
    React.createElement('div', { style: { fontSize: 'var(--text-2xl)', fontWeight: 700, color: TONE[tone] || TONE.neutral } }, value),
    React.createElement('div', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-muted)' } }, label))
}
