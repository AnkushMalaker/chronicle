# friend-lite-sdk

Python SDK for OMI / Friend Lite BLE wearable devices — audio streaming, button events, device control, and transcription.

Derived from the [OMI Python SDK](https://github.com/BasedHardware/omi/tree/main/sdks/python) (MIT license, Based Hardware Contributors). See `NOTICE` for attribution.

## Installation

```bash
pip install friend-lite-sdk
```

With optional transcription support:

```bash
pip install "friend-lite-sdk[deepgram]"   # Deepgram cloud transcription
pip install "friend-lite-sdk[wyoming]"    # Local ASR via Wyoming protocol
pip install "friend-lite-sdk[deepgram,wyoming]"  # Both
```

## Features

- **BLE Audio Streaming** — Connect to OMI/Friend Lite devices and stream Opus-encoded audio
- **Button Events** — Subscribe to single tap, double tap, long press events
- **Haptic Control** — Trigger haptic feedback patterns on supported devices
- **WiFi Sync** — Configure and trigger WiFi-based audio sync
- **Storage Access** — Read stored audio from device storage
- **Neo1 Support** — Sleep/wake control for Neo1 devices
- **Transcription** — Built-in Deepgram and Wyoming ASR integration

## Quick Start

```python
import asyncio
from friend_lite import OmiConnection, ButtonState, parse_button_event

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
from friend_lite import print_devices

asyncio.run(print_devices())
```

## Links

- [Chronicle Project](https://github.com/SimpleOpenSoftware/chronicle)
- [Original OMI Project](https://github.com/BasedHardware/omi)
