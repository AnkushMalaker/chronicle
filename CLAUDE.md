# CLAUDE.md

@AGENTS.md

## Verifying against a running backend

**First work out which machine you are on — this file is checked in, so it reads the
same on every one of them.** Run `hostname`:

| hostname | what it is | how to verify |
|---|---|---|
| `rainbow` | Ankush's dev workstation (CachyOS). Usually **no** Chronicle stack running. | Reach Kraken over the network, as below. |
| `Kraken` | The live deployment (Ubuntu 24.04 under WSL2). The stack is running **locally**. | Check it locally — `podman ps`, `curl localhost:8000/health`. Do **not** go looking for a remote Kraken; you are it. |

Anything below about reaching Kraken applies when you are *not* on Kraken. Do not
describe a machine as macOS unless `uname -s` actually says `Darwin`.

If there is no Chronicle stack on the machine you are on, that does not mean you
cannot verify your work. The live deployment runs on **`kraken`** and is reachable
from `rainbow` without any tunnel:

- `https://kraken.parrot-census.ts.net` (Tailnet, preferred)
- `https://192.168.0.110/` over LAN — self-signed cert, so use `curl -k`. Caddy on
  `:443` fronts the **vite dev server**, and the backend is on `:8000`.

Before reporting something as "not verified because nothing is running locally",
check Kraken first. See AGENTS.md → "Live Deployment Location" for the full
resolution order.

Note that Kraken runs in WSL2, so **two** Tailscale peers answer to the hostname
`Kraken` — the Linux guest (which runs Chronicle) and the Windows host. When
resolving it from `tailscale status --json`, take the one whose `OS` is `linux`.

Kraken runs *deployed* code from `~/workspaces/friend-lite` on that host, so it will
not have uncommitted local changes. To exercise new backend code end-to-end, stand up
the pieces you need locally (a `podman run` Mongo plus the FastAPI app is usually
enough) rather than deploying to Kraken — deploying touches a live system and needs
Ankush's say-so first.
