window.CK = window.CK || {}
if (!CK.Icon) CK.Icon = ({ n, size = 16, color, style }) => React.createElement('span', { style: { display: 'inline-flex', color, ...style }, ref: (e) => { if (e) { e.innerHTML = ''; const i = document.createElement('i'); i.setAttribute('data-lucide', n); i.setAttribute('width', size); i.setAttribute('height', size); e.appendChild(i); window.lucide && window.lucide.createIcons() } } })

CK.App = function App() {
  const I = CK.Icon
  const [view, setView] = React.useState('hub')
  return React.createElement('div', { style: { minHeight: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--surface-page)', color: 'var(--text-primary)', fontFamily: 'var(--font-sans)' } },
    React.createElement('header', { style: { background: 'var(--surface-raised)', borderBottom: '1px solid var(--border)', height: 64, flexShrink: 0, display: 'flex', alignItems: 'center' } },
      React.createElement('div', { style: { maxWidth: 1600, width: '100%', margin: '0 auto', padding: '0 32px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
        React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 14 } }, React.createElement(I, { n: 'music', size: 32, color: 'var(--accent)' }), React.createElement('span', { style: { fontSize: 19, fontWeight: 600 } }, 'Chronicle Dashboard')),
        React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 18, color: 'var(--text-muted)', fontSize: 13 } },
          React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: 6 } }, React.createElement('span', { style: { width: 8, height: 8, borderRadius: '50%', background: 'var(--success)' } }), 'Live'),
          React.createElement(I, { n: 'sun', size: 20 }),
          React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)' } }, React.createElement(I, { n: 'shield', size: 16, color: 'var(--accent)' }), 'admin@example.com'),
          React.createElement('span', { style: { display: 'flex', alignItems: 'center', gap: 6 } }, React.createElement(I, { n: 'log-out', size: 20 }), 'Logout')))),
    React.createElement('div', { style: { flex: 1, maxWidth: 1600, width: '100%', margin: '0 auto', padding: 32, display: 'flex', gap: 32 } },
      React.createElement(CK.Sidebar, null),
      React.createElement('main', { style: { flex: 1, minWidth: 0, background: 'var(--surface-raised)', border: '1px solid var(--border)', borderRadius: 10, padding: 28 } },
        view === 'hub'
          ? React.createElement(CK.DataAuditHub, { onOpenLab: () => setView('lab') })
          : React.createElement(CK.WakeWordLab, { onBack: () => setView('hub') }))),
    React.createElement('footer', { style: { background: 'var(--surface-raised)', borderTop: '1px solid var(--border)', padding: 18 } },
      React.createElement('div', { style: { textAlign: 'center', fontSize: 13, color: 'var(--text-faint)' } }, 'Chronicle — understand everything, everywhere, all at once.')))
}
