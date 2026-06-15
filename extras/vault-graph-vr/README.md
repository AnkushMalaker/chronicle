# Vault Graph VR

Explore an Obsidian-style memory **vault** as a 3D force-directed graph on a Meta
Quest (3 / 3S), in the browser via WebXR. Nodes are notes, edges are `[[wikilinks]]`
(and local `[text](note.md)` links). Point a controller at a node or link and pull
the trigger to **fly to that note** and read its contents — smooth or snap, configurable.

This is a WebXR app (runs in the Quest Browser): no app store, no sideloading, no
Unity. It's intended to grow into a viewer for the Chronicle memory vault.

> Status: foundation. Build, typecheck and dev-serve are verified. The in-headset
> interaction (locomotion, fly-to, note panel) is implemented but not yet tested on a
> physical device — see "Test in the headset" below.

## Stack

- [React Three Fiber](https://r3f.docs.pmnd.rs/) (v8) + [`@react-three/xr`](https://pmndrs.github.io/xr/) (v6) for WebXR
- [`r3f-forcegraph`](https://github.com/vasturiano/r3f-forcegraph) for the force-directed layout/render
- `three-spritetext` for in-world node labels
- Vite + TypeScript

This repo uses Node via nvm (`~/.nvm/versions/node/v22.14.0`). System `node` (v18) has
no npm; activate nvm first:

```bash
export PATH="$HOME/.nvm/versions/node/v22.14.0/bin:$PATH"
```

## 1. Generate graph data from a vault

```bash
npm install

# Sample vault (committed) — good first run:
npm run graph -- sample-vault --out public/graph.json

# Your real Chronicle memory vault (a folder of .md notes):
npm run graph -- /path/to/conversation_docs/<user-id> --out public/graph.json
```

`build-graph.mjs` options:

| Flag | Default | Meaning |
|------|---------|---------|
| `--mode vault\|tree` | `vault` | `vault` = notes + `[[links]]`; `tree` = plain folder parent→child |
| `--out <file>` | `public/graph.json` | output path (the app fetches `/graph.json`) |
| `--max-content <n>` | `8000` | chars of each note embedded for the in-VR reader |
| `--no-unresolved` | off | drop placeholder nodes for links with no target file |

Note bodies are embedded into `graph.json` so the headset needs no access to your
filesystem — it's a self-contained snapshot. `graph.json` is gitignored.

## 2. Run

```bash
npm run dev        # http://localhost:5180
```

On a desktop browser you'll see the graph and a control panel (transition mode,
glide duration, view distance). WebXR needs a headset to actually enter VR.

## 3. Test in the headset (Quest 3 / 3S)

WebXR requires a secure context. `localhost` counts, so the no-cert path is `adb reverse`:

1. Enable Developer Mode on the Quest (Meta Horizon app) and plug in USB.
2. On the PC: `adb reverse tcp:5180 tcp:5180`
3. In the Quest Browser open `http://localhost:5180`, click **Enter VR**.

(LAN alternative: `npm run host`, browse to `https://<pc-ip>:5180` — but that needs an
HTTPS cert, which `adb reverse` avoids.)

## Controls

- **Left stick** — fly (forward/back/strafe), relative to where you're looking
- **Right stick** — up/down (vertical), left/right (turn)
- **Point at a node or link + trigger** — fly to that note; its contents appear on a
  floating panel beside it
- Transition **smooth** vs **snap**, glide duration and view distance are set in the
  desktop overlay (in-VR settings panel is a TODO)

## Roadmap / next steps

- In-VR settings panel (so smooth/snap + speed are adjustable inside the headset)
- Grab-to-pan and two-handed scale of the whole graph
- Live data: fetch from the backend memory API instead of a static `graph.json`
- Markdown rendering (currently plain text) and follow-links-from-within-the-panel
- Filter/search; collapse by folder; highlight a node's neighborhood
