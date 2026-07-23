# Voice command routing — build status

Goal: say "hermes <anything>" → device commands go to Home Assistant (fast),
everything else goes to the Hermes agent. Routing decided by a sub-millisecond
local classifier, live in the plugin chain.

## ✅ DONE, DEPLOYED & VERIFIED LIVE

### Architecture (chain of responsibility, not hardcoded fallback)
Wake-word commands flow through a **priority-ordered plugin chain** (`config/plugins.yml`):
- **homeassistant** (priority 10) — asks its intent router "is this a home command?".
  If yes and it can act → handles, stops the chain. Otherwise **returns None (declines)**.
- **hermes** (priority 100) — catch-all, handles whatever earlier plugins declined.

The dispatcher sorts plugins by `priority` (added to `BasePlugin`). HA has **no
knowledge of Hermes** — no hardcoded cross-plugin call. A future drag-to-reorder
UI would just edit the `priority` values.

### intent-router microservice (`extras/intent-router/`)
- Separate lightweight image so ML deps stay OUT of the backend.
- Model2Vec `potion-base-32M` + logistic-regression head. ~0.5ms classify.
- Model pre-baked into the image (offline-safe, `HF_HUB_OFFLINE=1`).
- `POST /classify {text}` → `{route, p_home, label}`. `GET /health`.
- In `backends/advanced/docker-compose.yml` as service `intent-router` (port 8791).
- Retrain: `uv run --with model2vec --with scikit-learn --with numpy --with joblib
  python3 train.py` (here), then restart the service (code/artifact are mounted).

### HA cascade (inside the HA plugin, runs only when router says "home")
`/conversation` fast path → instant mood-keyword shortcut → LLM fuzzy translate.
Whole-home brightness/colour targets `hall` (=dining+living) + `study`.

### Classifier quality
- 17/18 real held-out phrases correct (lone miss: terse "movie time").
- "make it soothing"→home 0.93, "meaning of life"→other 0.03.

### Live verification (real plugin, in the workers container, real HA + service)
- "make it more soothing for my eyes" → HANDLED ha_mood → dim+warm, **586ms**
- "turn off hall lights"              → HANDLED ha_conversation
- "what is the meaning of life"       → DECLINED → Hermes catch-all
- Workers reach `http://intent-router:8791` by name; both plugins registered.

### Test harness
`plugins/homeassistant/intent_router/route_test "<phrase>"` — runs the live chain
(router service + HA + Hermes) and prints which handler won. Needs the service up.

## ⛔ NEEDS YOU
- **Speak into the HAVPE device** to verify the acoustic-wake → transcription inch
  (everything from transcript → routing → action is verified).
- **`GROQ_API_KEY`** (optional) — only affects the *LLM* fuzzy path; the mood
  shortcut already keeps common moods <2s. Set `ROUTER_HOME_THRESHOLD` to tune.
- **Evening scene / fans** — need HA-side setup (no fan entities exist yet).

## Durability note
Backend image was NOT rebuilt (no new backend deps — plugin code + config are
volume-mounted). Only the new `intent-router` image was built. A normal
`./start.sh` keeps everything; the service is in the compose file.
