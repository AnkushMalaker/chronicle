# Live interaction modes

Interaction modes are bounded, plugin-owned voice workflows. They are operational
sessions, not semantic Conversations: a mode can begin before a Conversation exists,
span audio-session reconnects, and end without deciding whether any recorded evidence
forms an event. Reconciliation continues to make that semantic decision separately.

## Audio ingest, live segmentation, and modes

These are three independent choices:

- **Audio ingest** describes how evidence arrives: a live device stream, an upload,
  ScreenPipe, or an import.
- **Live segmentation** selects how a live stream becomes transcript intervals.
  `streaming_stt` emits low-latency finalized utterances; `windowed_batch` transcribes
  fixed windows with the batch provider.
- **Interaction mode** routes an accepted utterance into one stateful plugin workflow.
  It neither creates nor closes a Conversation.

The first implementation consumes finalized `streaming_stt` utterances and acoustic
wake detections. Therefore a bare `order Swiggy` activation followed by open-mic turns
requires `defaults.live_segmentation: streaming_stt`. Acoustic
`Hermes, order Swiggy` works independently of that switch; under `windowed_batch`,
subsequent turns also need an acoustic Hermes command until a dedicated turn segmenter
is added. Fixed 30-second transcript windows are deliberately not treated as shopping
turns: they can contain several speakers and the assistant's own audio.

## Runtime flow

```text
streaming final utterance ---------+
                                   |
acoustic Hermes command -----------+--> InteractionIngress
                                          |
                                          | Redis active slot: user + client
                                          | Redis stream: interaction:inputs
                                          v
                                  interaction-mode worker
                                          |
                                          v
                                  owning plugin callback
                                          |
                             state + deadline committed in Redis
                                          |
                                 SSE + device TTS reply
```

An `InteractionSession` has its own ID and stores its owner plugin, phase, full plugin
state, user, client, current audio session, idle deadline, and hard deadline. It never
stores a Conversation ID. The current limits are ten minutes idle and thirty minutes
total.

While a mode is active, it has first refusal over every accepted utterance for that
user/client. The utterance does not also reach the normal plugin chain. This prevents a
shopping command from accidentally activating Home Assistant, Hermes chat, or another
keyword plugin. Wake and streaming paths can observe the same speech, so a short
cross-source deduplication window admits it only once.

## Multiple plugins and modes

Plugins declare mode definitions in code and enable selected mode IDs through the
`modes` list in `config/plugins.yml`. Chronicle enforces:

- one owner per mode ID;
- no equal or prefix-overlapping activation phrases;
- one active mode per user/client; and
- explicit enablement of every declared mode.

Ordinary plugin priorities still order stateless event handling. They do not arbitrate
an active mode: exclusivity and activation-phrase validation make that decision before
the event chain.

## Swiggy Instamart order mode

`swiggy_order` activates on `order Swiggy`; a leading `Hermes` or `Hey Hermes` is
optional. Its state machine is:

```text
propose configured preferred saved address
        |
explicit "yes" (or choose another label, then confirm it)
        |
keep or clear an existing cart
        |
search / select / change quantities
        |
"complete order" -> fresh cart + address + payment review
        |
"confirm order"  -> checkout (separate utterance only)
        |
UPI scan-or-tap link -> bounded payment monitor -> end
```

`cancel order` closes the mode before checkout and leaves the server cart unchanged.
After checkout creates an order, Chronicle does not claim it can cancel that order.

The non-secret plugin setting `preferred_address_label` is matched against the complete
saved-address label, case-insensitively. This deployment sets it to `Home` for the
Bangalore home. Chronicle asks `Use Home for delivery?` and does not read or mutate the
cart until the user explicitly says yes. If that exact label is unavailable, it asks for
another saved label instead of silently selecting Swiggy's first returned address.
The latency-sensitive LLM classifies natural shopping language into search, product
selection, quantity, cart display, or a non-mutating final-review tool. It resolves
through `defaults.fast_llm`, and each primary/fallback attempt is bounded. Checkout is
never an LLM tool: the reviewed cart still requires the separate exact `confirm order`
command. Instamart's repeated read-only search samples run concurrently and are merged
afterward; cart writes and checkout remain serialized.

Each Swiggy MCP attempt is also bounded. Safe reads may retry within the configured
attempt cap; checkout and payment confirmation remain single-attempt because an
unknown transport outcome is not safe to replay.

Before checkout, Chronicle re-reads the cart and compares it with the reviewed cart.
A change or an expired review requires another `complete order`. Only UPI scan-or-tap
is selected; Chronicle never silently falls back to Cash. The opaque payment link is
sent through Hermes' direct notification action and also included in the interaction
SSE event. A bounded RQ job monitors payment and confirms once when the MCP contract
requires it.

Redis stream inputs can be reclaimed after a worker crash. Each cart mutation therefore
checkpoints the exact full-cart replacement before calling Swiggy; replay applies that
same payload instead of incrementing twice. Checkout checkpoints an at-most-once intent
before the non-idempotent call. If the process stops across that boundary, Chronicle
reports the outcome as unknown and asks the user to check Swiggy—it never resubmits the
checkout automatically.

## Enabling the mode

The plugin must be explicitly enabled on the deployment host:

1. Copy the existing standalone Swiggy MCP OAuth artifacts `tokens.json` and
   `client.json` into a private, persistent directory mounted in the Chronicle
   container (the default is `/app/data/integrations/swiggy`). Do not copy or commit
   their contents into configuration.
2. Set `SWIGGY_LINKED_USER_ID` to the one Chronicle user allowed to use those
   credentials. Set `SWIGGY_TOKEN_DIRECTORY` only when overriding the default.
3. Set `plugins.swiggy_instamart.enabled: true` and keep
   `modes: [swiggy_order]` in `config/plugins.yml`.
4. Set `preferred_address_label` in `plugins/swiggy_instamart/config.yml` to the exact
   saved Swiggy label that should be proposed first.
5. Rebuild the backend/worker image when adding the MCP Python dependency, then restart
   the stack so the conditional interaction and wake-dispatch workers are started.

The worker OAuth flow is intentionally non-interactive. Expired authorization produces
an explicit error; renew it from the standalone MCP workspace and replace the private
artifacts rather than opening a browser from a background worker.
