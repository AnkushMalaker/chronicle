# Hermes Agent Plugin

Routes voice commands that start with (or contain) the keyword **`hermes`** to an
external [Hermes agent](https://github.com/) over its OpenAI-compatible HTTP API.

When you say something like *"Hermes, what's on my calendar tomorrow?"*, the
router strips the `hermes` keyword and forwards the rest of the utterance to your
Hermes server's `POST /v1/chat/completions` endpoint. The agent's reply is
returned as the plugin result — Chronicle logs it and records it in the plugin
event log.

## How it works

1. Triggered on `transcript.streaming` via the `keyword_anywhere` condition
   (`keywords: [hermes]` in `config/plugins.yml`).
2. The router strips `hermes` and passes the remaining text as `command`.
3. The plugin POSTs `{model, messages:[system, user]}` to `<api_url>/v1/chat/completions`.
4. Conversation continuity: each request sends `X-Hermes-Session-Id: <conversation_id>`
   so all utterances in one Chronicle conversation share one Hermes session.
5. The reply (`choices[0].message.content`) becomes the `PluginResult.message`.

## Configuration

Edit in the dashboard under **Plugins → Form → Hermes Agent**, or directly:

- `config.yml`
  - `api_url` — your Hermes server base URL (Tailscale MagicDNS name or LAN IP +
    port, e.g. `http://hermes-rpi.your-tailnet.ts.net:8642`). A trailing `/v1`
    is optional.
  - `model` — model name advertised by the server (default `hermes`).
  - `timeout` — request timeout in seconds (default `120`).
  - `system_prompt` — system prompt sent with each request.
- `.env`
  - `HERMES_API_KEY` — optional bearer token; leave empty for an
    unauthenticated server.

Use **Test Connection** in the UI to validate reachability and auth — it calls
`GET /v1/models` (cheap, no agent invocation).

## Notes

- The backend runs in Docker; `localhost` will **not** reach a Hermes server on
  another machine. Use the RPi's Tailscale name or LAN IP.
- This plugin and the Home Assistant plugin must not share the same keyword.
  Home Assistant is disabled by default here; if you re-enable it, give it a
  different keyword.
