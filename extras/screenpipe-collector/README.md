# Chronicle ScreenPipe companion

This service keeps ScreenPipe as the local capture store while forwarding completed audio chunks and compact application/window transitions to Chronicle. Screen pixels and OCR are retrieved only for bounded jobs requested by Chronicle.

## Pair and run

1. Create a pairing code from Chronicle's **Timeline → Sources** panel.
2. Pair this device:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe pair \
     --backend https://kraken.example \
     --code PAIRING_CODE
   ```

3. Start ScreenPipe with Chronicle's privacy-oriented defaults:

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

4. Run the companion:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe run
   ```

   After verification, install it as a separate user service:

   ```bash
   uv run --project extras/screenpipe-collector chronicle-screenpipe install-service
   ```

ScreenPipe itself can be installed independently with `screenpipe service install --record-args "..."`, using the recording arguments above.

Configuration is stored with mode `0600` under `~/.config/chronicle-screenpipe`; checkpoints live under `~/.local/state/chronicle-screenpipe`.
