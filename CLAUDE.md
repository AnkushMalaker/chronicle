# CLAUDE.md

@AGENTS.md

## Verifying against a running backend

There is usually **no** Chronicle stack on this workstation — but that does not mean
you cannot verify your work. The live deployment runs on **`kraken`** and is reachable
from here without any tunnel:

- `https://kraken.parrot-census.ts.net` (Tailnet, preferred)
- `https://192.168.0.110/` over LAN — self-signed cert, so use `curl -k`. Caddy on
  `:443` fronts the **vite dev server**, and the backend is on `:8000`.

Before reporting something as "not verified because nothing is running locally",
check Kraken first. See AGENTS.md → "Live Deployment Location" for the full
resolution order.

Kraken runs *deployed* code from `~/workspaces/friend-lite` on that host, so it will
not have uncommitted local changes. To exercise new backend code end-to-end, stand up
the pieces you need locally (a `podman run` Mongo plus the FastAPI app is usually
enough) rather than deploying to Kraken — deploying touches a live system and needs
Ankush's say-so first.
