# chronicle-wearable-sdk

Python SDK for OMI / Chronicle BLE wearable devices — audio streaming, button events, device control, and transcription.

Derived from the [OMI Python SDK](https://github.com/BasedHardware/omi/tree/main/sdks/python) (MIT license, Based Hardware Contributors). See `NOTICE` for attribution.

## Installation

The package lives in this repository and is consumed as a path dependency, so
nothing is fetched from PyPI:

```bash
uv add --editable extras/chronicle-wearable-sdk
```

With optional transcription support:

```bash
uv add --editable "extras/chronicle-wearable-sdk[deepgram]"   # Deepgram cloud transcription
uv add --editable "extras/chronicle-wearable-sdk[wyoming]"    # Local ASR via Wyoming protocol
uv add --editable "extras/chronicle-wearable-sdk[deepgram,wyoming]"  # Both
```

## Features

- **BLE Audio Streaming** — Connect to OMI/Chronicle devices and stream Opus-encoded audio
- **Button Events** — Subscribe to single tap, double tap, long press events
- **Haptic Control** — Trigger haptic feedback patterns on supported devices
- **WiFi Sync** — Configure and trigger WiFi-based audio sync
- **Storage Access** — Read stored audio from device storage
- **Neo1 Support** — Sleep/wake control for Neo1 devices
- **Transcription** — Built-in Deepgram and Wyoming ASR integration

## Quick Start

```python
import asyncio
from chronicle_wearable_sdk import OmiConnection, ButtonState, parse_button_event

async def main():
    async with OmiConnection("AA:BB:CC:DD:EE:FF") as conn:
        # Stream audio
        await conn.subscribe_audio(lambda _handle, data: print(len(data), "bytes"))

        # Listen for button events
        await conn.subscribe_button(
            lambda _handle, data: print("Button:", parse_button_event(data))
        )

        await conn.wait_until_disconnected()

asyncio.run(main())
```

## Device Discovery

```python
import asyncio
from chronicle_wearable_sdk import print_devices

asyncio.run(print_devices())
```

## Links

- [Chronicle Project](https://github.com/SimpleOpenSoftware/chronicle)
- [Original OMI Project](https://github.com/BasedHardware/omi)
