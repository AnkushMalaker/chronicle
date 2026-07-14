# Connection Resilience & UX — Unified Plan

> **Implementation status (branch `fix/optimizations-2`): IMPLEMENTED.**
> Done: Phase 0 (backend pong), Track 1 (1A BLE device-forget, 1B central auth
> manager + SecureStore + logout/forget split, 1C persistent WS retry, 1D zombie
> ping/pong, 1E foreground/relaunch reconnect, 1F Connection Doctor probe ladder
> + Tailscale-aware messaging), Track 2 (QR JSON bundle parse, SM URL/token
> storage, `serviceManager.ts` proxy+direct client, `NetworkOverview` screen with
> start/stop/restart, webui QR bundle), Track 3 (wearable + HAVPE relay reconnect).
> App `tsc --noEmit` clean; all edited Python `py_compile`s.
> **Two deliberate deferrals:** (a) 1F "Settings home" *relocation* of auth/URL into
> a separate route + compact chip — pure navigation reorg, the diagnosis/QR value
> shipped in-place; (b) Track 2 SM **token** in the QR — the SM token is a
> server-side secret not exposed to the browser; minting it into a QR is a security
> decision left to the user. The QR carries `backendUrl` + `serviceManagerUrl`; the
> backend-proxy transport works token-free whenever the backend is up.

This is the **authoritative, consolidated** plan. It merges two prior efforts:

- `RECONNECT_FIXES_PLAN.md` — the **mechanical reconnection** layer (8 gaps: never
  permanently give up, never destroy saved state on one failure, detect zombie sockets,
  recover on foreground/relaunch). Spans mobile app + wearable client + HAVPE relay.
- `MOBILE_APP_CONNECTION_UX_PLAN.md` — the **auth + diagnosis + network-control** layer
  (silent token refresh so "logged in means logged in", honest actionable failures /
  Connection Doctor, QR-first config, service-manager control). App-only (+ one webui change).

Both originals are now superseded by this file and may be deleted once this is approved.

---

## Why a merge (the collision)

The two plans operate at different layers but **rewrite the same app code** — the
token-refresh / reconnect path in `app/src/hooks/useAudioStreamer.ts` (`attemptReLogin`,
`attemptReconnect`, `onclose`, NetInfo).

- The UX plan's **Phase 1 central auth manager** is the natural *home* for re-login.
- The reconnect plan's **A2/A3** call re-login *during* a reconnect.

If we land the mechanical reconnect first against today's inline `attemptReLogin`, Phase 1
then tears it out — pure churn. So the merged order is:

> **Build the central auth manager first**, then layer WS-reconnect on top of it. Anything
> that does *not* touch auth — BLE device-forget (A1), the backend `pong` reply, and the
> entire device-client track (B/C) — is independent and can proceed in parallel.

Guiding principles (carried from both plans):
- **Persistent retry** — never permanently give up; never delete saved state on a single
  failure; always keep a recovery path alive.
- **Set-once, then invisible** — configure backend URL + creds once (ideally by QR); if the
  app says you're connected, actions work; failures are honest and actionable.

---

## Tracks (what can run in parallel)

| Track | Content | Depends on | Parallelizable? |
|-------|---------|-----------|-----------------|
| **0 · Backend pong** | `websocket_controller.py` reply `{type:'pong'}` (2 sites) | nothing | ✅ anytime |
| **1 · App auth core** | Auth manager → mechanical reconnect (A1–A4) → Connection Doctor | Track 0 for A2 | ⛓ internally ordered |
| **2 · App network control** | QR bundle → SM client → Network Overview → remote start | Track 1 auth manager | after Track 1 |
| **3 · Device clients** | Wearable (B1/B2/B3) + HAVPE relay (C1/C2) reconnect | Track 0 (uses pong) optional | ✅ fully independent of app |

Track 3 has **no app dependency** and can be implemented start-to-finish alongside Track 1.

---

## Phase 0 — Backend `pong` reply  *(standalone, unblocks A2)*

**File:** `backends/advanced/src/advanced_omi_backend/controllers/websocket_controller.py`
(`:1719-1721` and the second ping site `:1772`)

Today the backend receives app-level `{type:'ping'}` and just logs it — no reply, so the
client can't tell a half-open socket from a healthy idle one.

**Fix:** at *both* ping sites, reply `{type:'pong'}` over the same socket (mirror how other
control replies are sent). No client behavior depends on it until A2 ships, so this is safe
to land immediately.

**Verify:** `cd tests && make test-quick` (WS control-message regression) + a manual
`wscat` ping to `/ws` confirming the `{type:"pong"}` reply.

---

## Track 1 — App connection core (ordered)

### 1A. BLE device-forget on a single launch-reconnect failure  *(independent — can land first)*
**File:** `app/src/hooks/useAutoReconnect.ts:183-198`
**Problem:** the launch auto-reconnect `catch` (`:189-192`) calls
`saveLastConnectedDeviceId(null)` + `setLastKnownDeviceId(null)` — one failed attempt
(device briefly out of range at startup) permanently forgets the device. The continuous
backoff retry loop (`:90-168`) only fires on a `connected→disconnected` transition, so it
never covers a launch attempt that never connected.

**Fix:**
- Extract the disconnect-retry machinery (`:103-167`) into a
  `scheduleRetry(deviceId, isQuickFailure)` callback (backoff refs, countdown timer,
  `connectToDevice` call).
- In the launch `catch`, **stop wiping the device**; call `scheduleRetry(lastKnownDeviceId, true)`.
  The device id survives; retries continue with capped backoff (10s→5min).
- `handleCancelAutoReconnect` (`:207-216`) and the explicit Disconnect button
  (`index.tsx:373-377,391-396`) remain the *only* paths that clear the saved device.

> Pure BLE, no auth dependency. This is the highest-value mechanical fix and can ship before
> the auth manager. (This was the in-flight edit; re-do it cleanly here.)

### 1B. Central auth / connection manager  *(foundation for everything below)*
Fixes UX issues A1–A3 (creds disappear on logout, forced relogin while "logged in",
plaintext password).

- **New** `app/src/contexts/AuthContext.tsx` (or `services/auth.ts`): single source of truth
  for `{ backendUrl, email, token, status }`.
- **Silent refresh everywhere:** lift `useAudioStreamer.ts::attemptReLogin` into the manager
  as a `fetchAuthed()` wrapper that retries once on 401/expiry using stored creds, then
  surfaces a real failure only if re-login fails. *All* calls (health, future overview) go
  through it. JWTs are 1h, so this is what makes "logged in" actually mean logged in.
- **Honest status:** "Logged in as X" reflects token validity / refreshing, not mere presence.
- **Split logout from forget:** `clearAuthData()` →
  - **Log out** = clear token only; keep email (+ optionally password) → re-login is one tap.
  - **Forget account** = clear everything (today's behavior, made explicit).
- **SecureStore:** move password (+ token) to `expo-secure-store`; keep email in AsyncStorage.

**Touchpoints:** `app/src/utils/storage.ts` (SecureStore + split clear fns),
`app/src/components/AuthSection.tsx` (consume manager; logout vs forget),
`app/src/hooks/useAudioStreamer.ts` (relocate `attemptReLogin`).

### 1C. Persistent WS retry — remove permanent give-up  *(A3; consumes 1B)*
**File:** `app/src/hooks/useAudioStreamer.ts:255-262`
**Problem:** after 10 attempts it sets `manuallyStoppedRef = true`, which also disables the
NetInfo recovery path (`:455`) and the AppState path (1E). It should keep trying.

**Fix:**
- Remove `manuallyStoppedRef.current = true` on exhaustion. Keep backoff **capped** at
  `MAX_RECONNECT_MS` (30s); attempts continue indefinitely while a session is intended.
- After N attempts, downgrade to a low-frequency keep-trying state and post
  "Connection lost — still retrying" **once** (guard with a ref) instead of giving up.
- Re-login on reconnect now goes through the **1B auth manager**, not inline.
- `manuallyStoppedRef` is set only by `stopStreaming()` (`:219`) — genuine user stop.

### 1D. Zombie WebSocket detection  *(A2; client side; backend done in Phase 0)*
**File:** `app/src/hooks/useAudioStreamer.ts:326-334` (+ `onmessage` `:348-370`)
**Problem:** the 25s heartbeat is fire-and-forget; a silently dead TCP keeps
`readyState === OPEN` so nothing reconnects until NetInfo happens to fire.

**Fix (client):** add `lastPongRef`; stamp it on `{type:'pong'}` in `ws.onmessage`; in the
heartbeat interval, if `now - lastPongRef > 2×HEARTBEAT_MS`, `ws.close()` (→ `onclose` →
existing `attemptReconnect`). Reset `lastPongRef` on `onopen`. (Backend reply = Phase 0.)

### 1E. App-lifecycle (foreground / relaunch) reconnect  *(A4)*
**Files:** `app/src/hooks/useAudioStreamer.ts` (WS) + `app/src/hooks/useAutoReconnect.ts` (BLE)
**Problem:** no `AppState` listener anywhere. On iOS the JS runtime suspends in background;
on resume nothing re-establishes the socket. (True iOS *background streaming* needs
background modes and is **out of scope** — we handle *resume on foreground* + warm relaunch.)

**Fix:**
- **WS:** `AppState` listener mirroring the NetInfo effect (`:452-465`): on `'active'`, if
  `currentUrlRef` set, not manually stopped, and `readyState !== OPEN` → `attemptReconnect()`.
- **BLE:** `AppState` listener: on `'active'`, if a `lastKnownDeviceId` exists and isn't
  connected, reset `triedAutoReconnectForCurrentId=false` so the launch-reconnect effect
  (`:171-205`) re-fires.
- `onDeviceConnect`'s existing audio-pipeline auto-restart (`index.tsx:87-97`) means BLE
  recovery transitively resumes streaming.

### 1F. Connection Doctor + Settings home  *(UX C1, C2, A4-UX; consumes 1B)*
**File:** `app/src/components/BackendStatus.tsx` (today: single `fetch(${baseUrl}/health)`)
**Problem:** any failure collapses to a bare "Network request failed" — no diagnosis, no
recovery path (this is the originating incident: backend reachable by browser/curl, app
just says "network failed").

**Fix:**
- Replace the single `/health` fetch with a **probe ladder** → classified, actionable
  messages: *offline* → "You're offline"; *tailnet unreachable* → **"Can't reach your
  tailnet — is Tailscale connected?"** [Open Tailscale] [Scan QR]; *backend down* →
  **"Backend is down"** [Start backend] (Track 2); *ok* → connected.
- **QR scan reachable in every state** (un-gate it). Manual URL typing = last resort.
- Move backend URL + auth into a dedicated **Settings / Connection** screen; collapse the
  main surface to a compact status chip when healthy. Set-once, not constantly present.
- (Optional) weak native VPN-interface hint (`utun*`/`tun*`) as a secondary signal — "a VPN
  is up", not "Tailscale specifically" (no official API exists; documented limitation).

---

## Track 2 — App network control (after Track 1 auth manager)

### 2A. Richer QR bundle  *(UX Phase 3; web + app)*
- `backends/advanced/webui/src/pages/System.tsx`: QR payload becomes JSON
  `{ backendUrl, serviceManagerUrl, smToken }` (SM token from the System page's existing SM
  config). Keep the copyable plain URL for fallback.
- `app/src/components/QRScanner.tsx` + `app/src/utils/urlConversion.ts`: parse **both** a
  plain URL (legacy) and the JSON bundle; persist SM URL + token (token in SecureStore).

### 2B. Service-manager client + Network Overview  *(UX Phase 4, fixes C3)*
**New** `app/src/services/serviceManager.ts` with **two auto-selected transports**:
- **via backend proxy** `/api/admin/services` (existing JWT) when backend is up — no SM token;
- **direct to SM** (`/node`, `/cluster`, `/services`) with stored token when backend is down;
- SM URL auto-derived from backend host `:8775`, confirmed via the **unauthed** `/health`.

**Network Overview screen:** status table of nodes/services (backend, ASR, speaker,
wakeword, …) from `/cluster` + `/services`, each with running/health.

> Architectural note: the SM solves *"backend down"*, **not** *"Tailscale down"* (reaching
> the SM still needs the tailnet — that case is handled by 1F's guidance).

### 2C. Remote start / restart  *(UX Phase 5, fixes C4)*
In the overview, a down backend shows **Start** → direct `POST /services/backend/start` to
the SM (the only path that works when the backend is down) → poll `GET /operations/{id}` →
reflect progress. Same pattern restarts any service.

---

## Track 3 — Device clients (independent of the app)

### 3A. Wearable client — backend-WS reconnect + mid-session JWT refresh  *(B1/B2)*
**Files:** `extras/local-wearable-client/backend_sender.py:212-296`, `main.py:245-256`
**Problem:** `stream_to_backend` opens the WS once; on WS error the wrapper just logs
(`main.py:255-256`) and the backend stays dead until the whole BLE session cycles. No
mid-session token refresh.

**Fix:**
- Wrap the `websockets.connect` block (`backend_sender.py:234-296`) in a capped
  exponential-backoff reconnect loop. On a WS drop while the audio generator is still
  producing, re-fetch the JWT (`get_jwt_token`, `:217` — this **is** the mid-session refresh,
  solving B2) and re-dial **without ending the BLE session**.
- Drain-vs-reconnect: the queue-backed generator (`main.py:246-251`) yields `None` only at
  true session end → treat `None` as "stop", WS exceptions as "reconnect". Drop audio during
  the gap (matches current outage behavior; document the choice).
- Reset backoff after a healthy connection duration (mirror app's `MIN_HEALTHY_DURATION`).

### 3B. (optional) headless `run()` BLE backoff parity  *(B3, low priority)*
**File:** `extras/local-wearable-client/main.py:584-621` — headless path rescans at a fixed
`scan_interval` with no backoff. The launchd default is the menu app (`menu_app.py:468-488`,
already has backoff), so add the same exponential backoff for parity only if cheap.

### 3C. HAVPE relay — backend-WS reconnect  *(C1)*
**File:** `extras/havpe-relay/relay_core.py:300-376` (`run_device_session`)
**Problem:** `websockets.connect` (`:301`) is opened once; any WS close tears down the
session (`:364-376`) and waits passively for the ESP32 to re-dial.
**Fix:** wrap WS-connect + task group (`:301-360`) in an inner capped-backoff retry loop,
keeping the device `reader`/`writer` alive across WS reconnects. Re-fetch the token
(`get_jwt_token`, `:282`) on each re-dial. Break only on device disconnect
(`IncompleteReadError`/reader EOF) or idle timeout — not on a WS blip.

### 3D. HAVPE relay — ESPHome API reconnect within a session  *(C2, lower priority)*
**Files:** `extras/havpe-relay/relay_core.py:304-321`, `device_controller.py:33-44`
**Problem:** one API connect timeout → audio-only for the entire session; no reconnect.
**Fix:** use `aioesphomeapi`'s `ReconnectLogic` (or a lightweight periodic re-`connect()`)
so button/dial/LED/speaker recover mid-session.

> Note: device→relay audio TCP is server-side (relay can't initiate); the firmware re-dials
> and the idle-timeout (`relay_core.py:45`) reaps zombies. No change needed there.

---

## Suggested implementation order

1. **Phase 0** backend `pong` — tiny, standalone, unblocks 1D.
2. **1A** BLE device-forget — highest mechanical value, no auth dependency.
3. **1B** central auth manager — foundation; removes daily auth pain (A1–A3 UX).
4. **1C → 1D → 1E** mechanical WS reconnect on top of the auth manager.
5. **1F** Connection Doctor + Settings home — fixes the originating "useless message" incident.
6. **2A → 2B → 2C** network-control story, each building on the last.
- **Track 3 (3A, 3C, then 3D/3B)** in parallel throughout — no app dependency.

Each item is independently shippable.

---

## Verification

**App mechanical (1A,1C,1D,1E):** dev client on a physical phone (BLE needs hardware):
- 1A: connect device, force-quit, walk out of range, relaunch in range → reconnects and is
  **not** forgotten if the first attempt fails (`lastKnownDeviceId` persists).
- 1C: backend down for >10 reconnect attempts, then back → client resumes without a manual
  restart (previously gave up permanently).
- 1D: start streaming, kill the backend's TCP path abruptly (drop wifi / `kill -STOP`) so no
  close frame → within ~50s client detects missed pongs, closes, reconnects on return.
- 1E: background the app (iOS), foreground → WS re-establishes; toggle BLE off/on while
  backgrounded then foreground → device reconnects.

**App auth/UX (1B,1F):** log out → email retained, one-tap re-login; let a token expire (1h
or shortened TTL) and hit a non-streaming action → silent refresh, no forced relogin; point
the app at an unreachable tailnet → Connection Doctor says *tailnet unreachable* with
actions, not "Network request failed".

**Network control (2A–2C):** scan the JSON QR → backend URL + SM URL + token persisted; take
the backend down → Overview shows it down with **Start** → SM starts it → status flips healthy.

**Device clients (3A,3C):** `cd extras/local-wearable-client && uv run python main.py run`
against a local backend; mid-stream `./restart.sh` → client re-dials and resumes without
dropping BLE; run >1h (or shorten token TTL) to confirm mid-session re-auth. HAVPE: ESP32
dialed in, restart backend mid-session → relay re-dials, ESP32 stays connected; kill/restore
the ESPHome API → button/LED recover (3D).

**Regression:** `cd tests && make test-quick` after the Phase 0 `pong` change.
