# Chronicle ScreenPipe companion

This service keeps ScreenPipe as the local capture store while forwarding compact,
event-driven observations and, only when enabled, completed audio chunks to Chronicle.
Long activities remain one observation with incremental novel-text samples. Screen
pixels and OCR are retrieved only for bounded jobs requested by Chronicle. See the full
[capture-node architecture](../../docs/screenpipe.md).

## Pair and run

1. Create a pairing code from Chronicle's **Timeline → Sources** panel.
2. Pair this device:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe pair \
     --backend https://kraken.example \
     --code PAIRING_CODE
   ```

3. Start ScreenPipe with Chronicle's privacy-oriented defaults. The example records
   both the default microphone and system output while leaving transcription to
   Chronicle:

   ```bash
   screenpipe record --audio-transcription-engine disabled \
     --use-system-default-audio true --use-all-monitors true \
     --use-pii-removal true --disable-keyboard-capture \
     --disable-clipboard-capture --capture-scroll true \
     --prioritize-input-latency --pause-on-drm-content \
     --screenpipe-aec-enabled \
     --disable-telemetry \
     --video-quality balanced --retention-days 90 \
     --retention-mode media --api-auth true
   ```

   Set `SCREENPIPE_API_KEY` for both ScreenPipe and the pairing command so the
   companion can authenticate bounded local OCR queries.

   For system audio without the microphone, replace
   `--use-system-default-audio true` with
   `--use-system-default-audio false --audio-device "DEVICE (output)"`. Discover the
   exact platform device names with `screenpipe audio list --output json`. Use
   `--disable-audio` only for screen-only capture.

   Pair with `--forward-audio none|output|input|both` to independently control which
   locally recorded sources are uploaded. The guided setup asks for both the local
   capture mode and forwarding mode.

   Leave ScreenPipe's meeting detector **enabled** (no
   `--disable-meeting-detector`): on macOS and Windows it persists meetings —
   with titles — into its own database, and the companion mirrors those rows
   into Chronicle and tags forwarded audio chunks with the meeting interval,
   so the backend bounds the conversation on the real call instead of fixed
   time windows. Because the rows are persisted, bounds survive companion
   downtime and backfill retroactively. Use ScreenPipe's
   `ignored_meeting_apps` to exclude specific apps. On Linux, where ScreenPipe
   has no meeting sensor, the companion detects calls itself from the PipeWire
   graph (a known meeting app or browser holding a *running* microphone
   stream, browsers attributed through the current observation's URL) —
   live-only, so intervals only cover time the companion was running. Disable
   all of it at pairing time with `--no-meeting-detection`.

4. Run the companion:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe run
   ```

   After verification, install it as a separate user service:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe install-service
   ```

ScreenPipe itself can be installed independently with `screenpipe service install --record-args "..."`, using the recording arguments above.

Configuration is stored with mode `0600` under `~/.config/chronicle-screenpipe`;
checkpoints and crash-resumable observation state live under
`~/.local/state/chronicle-screenpipe`.
