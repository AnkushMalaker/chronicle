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
     --disable-meeting-detector --disable-telemetry \
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
