# friend-lite-sdk

Python SDK for OMI / Friend Lite BLE devices — audio streaming, button events, and transcription.

Derived from the [OMI Python SDK](https://github.com/BasedHardware/omi/tree/main/sdks/python) (MIT license, Based Hardware Contributors). See `NOTICE` for attribution.

## Installation

```bash
pip install -e extras/friend-lite-sdk
```

With optional transcription support:

```bash
pip install -e "extras/friend-lite-sdk[deepgram,wyoming]"
```

## Usage

```python
import asyncio
from friend_lite import OmiConnection, ButtonState, parse_button_event

async def main():
    async with OmiConnection("AA:BB:CC:DD:EE:FF") as conn:
        await conn.subscribe_audio(lambda _handle, data: print(len(data), "bytes"))
        await conn.wait_until_disconnected()

asyncio.run(main())
```
