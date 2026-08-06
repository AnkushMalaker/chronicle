window.CK = window.CK || {}
if (!CK.Icon) CK.Icon = ({ n, size = 16, color, style }) => React.createElement('span', { style: { display: 'inline-flex', color, ...style }, ref: (e) => { if (e) { e.innerHTML = ''; const i = document.createElement('i'); i.setAttribute('data-lucide', n); i.setAttribute('width', size); i.setAttribute('height', size); e.appendChild(i); window.lucide && window.lucide.createIcons() } } })
const NS = window.Chronicle || Object.values(window).find(v => v && v.Button && v.HubCard) || {}

const CLIPS = [
  ['0.996', '22/7/2026, 1:36:18 am'], ['0.996', '22/7/2026, 1:35:10 am'],
  ['0.996', '22/7/2026, 1:34:33 am'], ['0.996', '22/7/2026, 1:34:24 am'],
]
function FauxAudio() {
  const I = CK.Icon
  return React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 8, background: 'var(--surface-page)', border: '1px solid var(--border)', borderRadius: 6, padding: '5px 10px', width: 220 } },
    React.createElement(I, { n: 'play', size: 14, color: 'var(--text-secondary)' }),
    React.createElement('div', { style: { flex: 1, height: 3, borderRadius: 2, background: 'var(--gray-600)', position: 'relative' } }, React.createElement('span', { style: { position: 'absolute', left: '8%', top: '50%', transform: 'translate(-50%,-50%)', width: 9, height: 9, borderRadius: '50%', background: 'var(--text-secondary)' } })),
    React.createElement('span', { style: { fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' } }, '0:00'),
    React.createElement(I, { n: 'volume-2', size: 14, color: 'var(--text-muted)' }))
}
function ClipRow({ score, when }) {
  const { Badge, Button, IconButton } = NS
  const I = CK.Icon
  return React.createElement('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, border: '1px solid var(--border)', borderRadius: 'var(--radius-md)', padding: '8px 12px' } },
    React.createElement('span', { style: { fontFamily: 'var(--font-mono)', fontSize: 14, fontWeight: 600, color: 'var(--text-primary)', width: 52 } }, score),
    React.createElement(Badge, { tone: 'neutral', icon: React.createElement(I, { n: 'arrow-right-left', size: 12 }) }, 'Move / Copy'),
    React.createElement('span', { style: { fontSize: 12, color: 'var(--text-muted)' } }, when),
    React.createElement('span', { style: { fontSize: 12, color: 'var(--text-faint)' } }, '3s · smart_turn'),
    React.createElement(FauxAudio, null),
    React.createElement('div', { style: { marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 } },
      React.createElement(Button, { variant: 'secondary', style: { background: 'var(--success-soft-bg)', color: 'var(--success-fg)' }, icon: React.createElement(I, { n: 'check', size: 14 }) }, 'Wake'),
      React.createElement(Button, { variant: 'secondary', style: { background: 'var(--danger-soft-bg)', color: 'var(--danger-fg)' }, icon: React.createElement(I, { n: 'x', size: 14 }) }, 'Not'),
      React.createElement(IconButton, { label: 'Delete clip', danger: true }, React.createElement(I, { n: 'trash-2', size: 14 }))))
}
CK.WakeWordLab = function WakeWordLab({ onBack }) {
  const { Breadcrumb, Button, Badge, StatCard, Tabs, Card } = NS
  const I = CK.Icon
  const [bucket, setBucket] = React.useState('pending')
  return React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 18 } },
    React.createElement(Breadcrumb, { items: [{ label: 'Data Audit', onClick: onBack, icon: React.createElement(I, { n: 'arrow-left', size: 14 }) }, { label: 'Wake-Word Lab' }] }),
    React.createElement('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
      React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: 10 } }, React.createElement(I, { n: 'target', size: 24, color: 'var(--accent)' }), React.createElement('h1', { style: { margin: 0, fontSize: 'var(--text-2xl)', fontWeight: 700, color: 'var(--text-primary)' } }, 'Wake-Word Lab')),
      React.createElement(Button, { variant: 'secondary', icon: React.createElement(I, { n: 'refresh-cw', size: 16 }) }, 'Refresh')),
    React.createElement('p', { style: { margin: 0, fontSize: 13, lineHeight: 1.55, color: 'var(--text-muted)', maxWidth: 1000 } }, "Close the training loop, per wake word: review false positives the model fired on, and capture clips of yourself saying that word (false negatives). Each section below is one wake word."),
    React.createElement(Card, null,
      React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 14, fontWeight: 600 } }, React.createElement(I, { n: 'radio', size: 16 }), 'Active streams'),
      React.createElement('p', { style: { margin: 0, fontSize: 13, color: 'var(--text-muted)' } }, 'No live streams. Start a recording (Live Record) and it will appear here.')),
    React.createElement('section', { style: { border: '1px solid var(--border)', borderRadius: 'var(--radius-xl)' } },
      React.createElement('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, borderBottom: '1px solid var(--border)', padding: '12px 16px' } },
        React.createElement('h2', { style: { margin: 0, fontSize: 'var(--text-lg)', fontWeight: 700, color: 'var(--text-primary)' } }, '"hey hermes"'),
        React.createElement(Badge, { tone: 'mono' }, 'hey_hermes_f.onnx'),
        React.createElement(Badge, { tone: 'success', icon: React.createElement(I, { n: 'shield-check', size: 14 }) }, 'verifier on'),
        React.createElement(Badge, { tone: 'neutral', icon: React.createElement(I, { n: 'eye', size: 14 }) }, 'collect-only off'),
        React.createElement('span', { style: { fontSize: 12, color: 'var(--text-muted)' } }, 'thr 0.9 · patience 2'),
        React.createElement('div', { style: { marginLeft: 'auto', display: 'flex', gap: 8 } },
          React.createElement(Button, { variant: 'secondary', icon: React.createElement(I, { n: 'copy-x', size: 14 }) }, 'Remove duplicates'),
          React.createElement(Button, { variant: 'primary', disabled: true, icon: React.createElement(I, { n: 'target', size: 14 }) }, 'I\'ll say "hey hermes" now'))),
      React.createElement('div', { style: { padding: 16 } },
        React.createElement('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 16 } },
          React.createElement(StatCard, { value: 4, label: 'Pending', tone: 'amber' }),
          React.createElement(StatCard, { value: 196, label: 'Positives', tone: 'green' }),
          React.createElement(StatCard, { value: 75, label: 'Negatives', tone: 'red' }),
          React.createElement(StatCard, { value: 2, label: 'False negatives', tone: 'blue' })),
        React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 } },
          React.createElement(Tabs, { variant: 'pill', value: bucket, onChange: setBucket, tabs: [{ value: 'pending', label: 'Pending review' }, { value: 'positive', label: 'Positives (wake)' }, { value: 'negative', label: 'Negatives (not wake)' }] }),
          React.createElement('span', { style: { marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: 'var(--text-muted)' } }, React.createElement(I, { n: 'help-circle', size: 16 }), 'How to label')),
        bucket === 'pending'
          ? React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 } }, CLIPS.map((c, i) => React.createElement(ClipRow, { key: i, score: c[0], when: c[1] })))
          : React.createElement('p', { style: { padding: '24px 0', textAlign: 'center', fontSize: 13, color: 'var(--text-faint)' } }, 'Switch back to Pending review to see captured clips.'))))
}
