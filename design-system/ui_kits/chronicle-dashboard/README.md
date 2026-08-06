# Chronicle Dashboard — UI kit

Recreation of the Chronicle admin dashboard, focused on the **Data Audit** hub and the
**Wake-Word Lab** sub-view it opens into (the "move Wake-Word Lab into Data Audit" design).

- `index.html` — interactive shell. Loads React + Lucide + the DS bundle, then mounts `App`.
  Click the **Wake-Word Lab** hub tile to enter the lab; the breadcrumb returns to the hub.
- `App.jsx` — header / sidebar / footer shell + hub↔lab routing.
- `Sidebar.jsx` — the 15-item nav (Data Audit active; Wake-Word Lab folded in, not a separate row).
- `DataAuditHub.jsx` — hub header, 4 launcher tiles, confidence expander, curated enrollment, speaker search.
- `WakeWordLab.jsx` — breadcrumb, per-word section, stat tiles, bucket tabs, clip rows.

Screens compose the primitives in `/components` — they do not re-implement them.
All colour/spacing/type comes from the tokens in `/styles.css`.
