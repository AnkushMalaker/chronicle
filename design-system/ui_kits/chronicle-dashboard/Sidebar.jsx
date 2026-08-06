window.CK = window.CK || {}
if (!CK.Icon) CK.Icon = ({ n, size = 16, color, style }) => React.createElement('span', { style: { display: 'inline-flex', color, ...style }, ref: (e) => { if (e) { e.innerHTML = ''; const i = document.createElement('i'); i.setAttribute('data-lucide', n); i.setAttribute('width', size); i.setAttribute('height', size); e.appendChild(i); window.lucide && window.lucide.createIcons() } } })
const NS = window.Chronicle || Object.values(window).find(v => v && v.Button && v.HubCard) || {}

const NAV = [
  ['radio','Live Record'],['message-circle','Chat'],['message-square','Conversations'],
  ['calendar-days','Timeline'],['scroll-text','Memory Ledger'],['users','User Management'],
  ['upload','Upload Audio'],['sparkles','Data Audit',true],['layers','Queue & Events'],
  ['puzzle','Plugins'],['zap','Training'],['network','Network'],['activity','System Status'],
  ['alert-triangle','System Errors'],['settings','Settings'],
]
CK.Sidebar = function Sidebar() {
  const { NavItem } = NS
  return React.createElement('nav', { style: { width: 256, flexShrink: 0, alignSelf: 'flex-start', background: 'var(--surface-raised)', border: '1px solid var(--border)', borderRadius: 10, padding: 14 } },
    React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 } },
      NAV.map(([ic, label, active]) => React.createElement(NavItem, { key: label, icon: React.createElement(CK.Icon, { n: ic, size: 20 }), label, active }))))
}
