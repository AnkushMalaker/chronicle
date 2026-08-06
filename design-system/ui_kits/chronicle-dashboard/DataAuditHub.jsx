window.CK = window.CK || {}
if (!CK.Icon) CK.Icon = ({ n, size = 16, color, style }) => React.createElement('span', { style: { display: 'inline-flex', color, ...style }, ref: (e) => { if (e) { e.innerHTML = ''; const i = document.createElement('i'); i.setAttribute('data-lucide', n); i.setAttribute('width', size); i.setAttribute('height', size); e.appendChild(i); window.lucide && window.lucide.createIcons() } } })
const NS = window.Chronicle || Object.values(window).find(v => v && v.Button && v.HubCard) || {}

CK.DataAuditHub = function DataAuditHub({ onOpenLab }) {
  const { HubCard, CollapsibleSection, Card, Button, Checkbox, Input, Badge } = NS
  const I = CK.Icon
  return React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 22 } },
    React.createElement('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 12 } },
      React.createElement(I, { n: 'sparkles', size: 24, color: 'var(--accent)', style: { marginTop: 2 } }),
      React.createElement('div', null,
        React.createElement('h2', { style: { margin: 0, fontSize: 'var(--text-2xl)', fontWeight: 600, color: 'var(--text-primary)' } }, 'Data Audit'),
        React.createElement('p', { style: { margin: '5px 0 0', fontSize: 'var(--text-sm)', color: 'var(--text-muted)', maxWidth: 860 } }, 'Decide what the audio is — audit conversations, enroll speakers, classify background & role, and tune wake words. One home for all curation.'))),
    React.createElement('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16 } },
      React.createElement(HubCard, { icon: React.createElement(I, { n: 'sparkles', size: 20 }), title: 'Audit conversations', stat: '468', description: 'Find speech-free or mis-attributed audio; split, merge, archive.' }),
      React.createElement(HubCard, { icon: React.createElement(I, { n: 'mic', size: 20 }), title: 'Enroll speakers', description: 'Review the relabel queue and strengthen voiceprints — deliberate, gated.' }),
      React.createElement(HubCard, { icon: React.createElement(I, { n: 'radio', size: 20 }), title: 'Background & role', description: 'Content vs real people vs noise. Feeds background suppression.' }),
      React.createElement(HubCard, { icon: React.createElement(I, { n: 'target', size: 20 }), title: 'Wake-Word Lab', description: 'Review false positives & capture wake clips; per-word retrain loop.', cta: React.createElement(React.Fragment, null, 'Open lab ', React.createElement(I, { n: 'arrow-right', size: 14 })), active: true, onClick: onOpenLab })),
    React.createElement(CollapsibleSection, { icon: React.createElement(I, { n: 'gauge', size: 16, color: 'var(--accent)' }), title: 'Speaker identification confidence', meta: '2048/11625 (17.6%) low-confidence' },
      React.createElement('div', { style: { fontSize: 13, color: 'var(--text-muted)' } }, 'Threshold in use 0.35 · recommended 0.42 · 11625 identifications across 468 conversations')),
    React.createElement(Card, null,
      React.createElement('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 } },
        React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: 8 } }, React.createElement(I, { n: 'shield-check', size: 20, color: 'var(--success)' }), React.createElement('span', { style: { fontSize: 'var(--text-base)', fontWeight: 600 } }, 'Curated Speaker Enrollment')),
        React.createElement(Button, { variant: 'secondary', icon: React.createElement(I, { n: 'refresh-cw', size: 14 }) }, 'Refresh')),
      React.createElement('p', { style: { margin: '0 0 10px', fontSize: 12.5, lineHeight: 1.55, color: 'var(--text-muted)' } }, 'Only the segments you relabelled by hand are candidates, gated for quality. Clean clips are pre-selected; greyed clips are excluded with a reason.'),
      React.createElement(Checkbox, { label: 'Also show auto-identified segments', hint: '(off by default — never pre-ticked)' })),
    React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
      React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-lg)', fontWeight: 700 } }, React.createElement(I, { n: 'mic', size: 20, color: 'var(--accent)' }), 'Speaker enrollment'),
      React.createElement(Input, { placeholder: 'Search for a speaker to enhance…' })))
}
