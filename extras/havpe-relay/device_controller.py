"""
ESPHome API client for HAVPE device I/O.

Manages the ESPHome native API connection (port 6053) for:
  - Subscribing to text_sensor state changes (button presses, dial rotation)
  - Controlling LED ring colors
  - Playing audio via media_player

This is separate from the TCP connection (port 8989) used for mic streaming.
"""

import asyncio
import logging

import aioesphomeapi

logger = logging.getLogger(__name__)


class DeviceController:
    """ESPHome API client for HAVPE device I/O."""

    def __init__(self):
        self._client: aioesphomeapi.APIClient | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._playback_state: str | None = None
        self._playback_waiters: list[tuple[str, asyncio.Future[None]]] = []
        self._entity_keys: dict[str, int] = {}  # object_id -> key
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def supports_voice_protocol_v1(self) -> bool:
        """True only when firmware exposes media output and physical state."""
        return (
            self._connected
            and "_media_player" in self._entity_keys
            and "playback_state" in self._entity_keys
        )

    async def connect(self, device_ip: str, port: int = 6053) -> bool:
        """Connect to device, discover entities, subscribe to states.

        Returns True on success, False on failure (relay continues without API).
        """
        try:
            self._entity_keys.clear()
            self._client = aioesphomeapi.APIClient(
                address=device_ip,
                port=port,
                password="",
            )
            # on_stop fires when the API connection drops; flip _connected so the
            # session's keepalive task notices and re-runs connect() (3D).
            await self._client.connect(login=True, on_stop=self._on_api_disconnect)

            device_info = await self._client.device_info()
            logger.info(
                "ESPHome API connected: %s (%s)",
                device_info.friendly_name or device_info.name,
                device_info.mac_address,
            )

            # Discover entities — map by object_id AND by type (fallback)
            entities, _ = await self._client.list_entities_services()
            light_key = None
            media_player_key = None

            for entity in entities:
                oid = entity.object_id
                self._entity_keys[oid] = entity.key
                logger.debug(
                    "Entity: %s (key=%d, type=%s)",
                    oid,
                    entity.key,
                    type(entity).__name__,
                )

                # Type-based discovery for light and media_player
                if isinstance(entity, aioesphomeapi.LightInfo) and light_key is None:
                    light_key = entity.key
                elif (
                    isinstance(entity, aioesphomeapi.MediaPlayerInfo)
                    and media_player_key is None
                ):
                    media_player_key = entity.key
                elif (
                    isinstance(entity, aioesphomeapi.NumberInfo)
                    and oid == "led_hold_duration"
                ):
                    self._entity_keys["_led_hold_duration"] = entity.key
                    logger.info("LED hold duration entity found (key=%d)", entity.key)

            # Store type-discovered keys under canonical names
            if light_key is not None:
                self._entity_keys["_light"] = light_key
                logger.info("Light entity found (key=%d)", light_key)
            else:
                logger.warning("No light entity found on device")

            if media_player_key is not None:
                self._entity_keys["_media_player"] = media_player_key
                logger.info("Media player entity found (key=%d)", media_player_key)
            else:
                logger.warning("No media_player entity found on device")

            # Check text_sensors
            for name in ["button_action", "dial_action", "playback_state"]:
                if name in self._entity_keys:
                    logger.info(
                        "Text sensor '%s' found (key=%d)", name, self._entity_keys[name]
                    )
                else:
                    logger.warning("Text sensor '%s' not found", name)

            # Subscribe to state changes
            self._client.subscribe_states(self._on_state_change)
            self._connected = True
            return True

        except Exception as e:
            logger.warning(
                "ESPHome API connection failed: %s (relay continues audio-only)", e
            )
            self._connected = False
            return False

    async def _on_api_disconnect(self, expected_disconnect: bool) -> None:
        """aioesphomeapi on_stop callback: the API connection dropped.

        Mark ourselves disconnected so the session's keepalive task re-runs the
        full connect()/discovery. We don't reconnect here to avoid racing an
        intentional disconnect() during teardown.
        """
        self._connected = False
        if not expected_disconnect:
            logger.warning("ESPHome API connection lost — will attempt reconnect")

    def _on_state_change(self, state) -> None:
        """Sync callback from aioesphomeapi. Enqueues button/dial events."""
        if not isinstance(state, aioesphomeapi.TextSensorState):
            return

        key = state.key
        value = state.state
        if not value:
            return

        # Match key to our known text_sensors
        if key == self._entity_keys.get("playback_state"):
            if value not in {"started", "stopped"}:
                logger.warning("Unknown firmware playback state: %s", value)
                return
            self._publish_playback_state(value)
            logger.info("Physical playback state: %s", value)
        elif key == self._entity_keys.get("button_action"):
            event = {"type": "button-event", "state": value}
            self._event_queue.put_nowait(event)
            logger.info("Button event: %s", value)
        elif key == self._entity_keys.get("dial_action"):
            event = {"type": "dial-event", "direction": value}
            self._event_queue.put_nowait(event)
            logger.info("Dial event: %s", value)

    async def get_event(self) -> dict:
        """Await next device event from queue."""
        return await self._event_queue.get()

    async def set_led(
        self,
        r: float,
        g: float,
        b: float,
        brightness: float = 0.3,
        duration: float = 5.0,
    ) -> None:
        """Set LED ring color. Values 0.0-1.0. Duration in seconds. No-op if not connected."""
        key = self._entity_keys.get("_light")
        if not self._connected or not self._client or key is None:
            logger.debug("set_led skipped: not connected or no light entity")
            return

        try:
            # Set hold duration first (processed before light command on device)
            dur_key = self._entity_keys.get("_led_hold_duration")
            if dur_key is not None:
                self._client.number_command(key=dur_key, state=duration)

            self._client.light_command(
                key=key,
                state=True,
                rgb=(r, g, b),
                brightness=brightness,
            )
            logger.info(
                "LED set: rgb=(%.1f, %.1f, %.1f) br=%.1f dur=%.1fs",
                r,
                g,
                b,
                brightness,
                duration,
            )
        except Exception as e:
            logger.warning("LED command failed: %s", e)

    async def set_led_effect(
        self,
        effect: str,
        r: float = 0.0,
        g: float = 0.0,
        b: float = 0.0,
        brightness: float = 0.4,
        duration: float = 5.0,
    ) -> None:
        """Activate a named addressable LED effect on the ring.

        ``effect`` is one of the firmware effect names (e.g. "Listening For
        Command", "Thinking", "Replying", "Error"); the rgb values set the base
        colour the effect tints with. ``effect="None"`` stops any running effect.
        ``duration`` arms the firmware's status-LED override hold so the ring
        reverts to the connectivity colour afterwards. No-op if not connected.
        """
        key = self._entity_keys.get("_light")
        if not self._connected or not self._client or key is None:
            logger.debug("set_led_effect skipped: not connected or no light entity")
            return

        try:
            # Set hold duration first (processed before the light command on device)
            dur_key = self._entity_keys.get("_led_hold_duration")
            if dur_key is not None:
                self._client.number_command(key=dur_key, state=duration)

            self._client.light_command(
                key=key,
                state=True,
                rgb=(r, g, b),
                brightness=brightness,
                effect=effect,
            )
            logger.info(
                "LED effect: %s rgb=(%.1f, %.1f, %.1f) br=%.1f dur=%.1fs",
                effect,
                r,
                g,
                b,
                brightness,
                duration,
            )
        except Exception as e:
            logger.warning("LED effect command failed: %s", e)

    async def play_audio(self, url: str, announcement: bool = True) -> None:
        """Play audio URL via the firmware media player."""
        key = self._entity_keys.get("_media_player")
        if not self._connected or not self._client or key is None:
            raise RuntimeError("HAVPE media player is unavailable")

        try:
            self._client.media_player_command(
                key=key,
                media_url=url,
                announcement=announcement,
            )
            logger.info("Play audio: %s (announcement=%s)", url, announcement)
        except Exception as e:
            raise RuntimeError("HAVPE play command failed") from e

    async def stop_audio(self) -> None:
        """Stop the current response; firmware later confirms ``stopped``."""
        key = self._entity_keys.get("_media_player")
        if not self._connected or not self._client or key is None:
            raise RuntimeError("HAVPE media player is unavailable")
        self._client.media_player_command(
            key=key,
            command=aioesphomeapi.MediaPlayerCommand.STOP,
        )

    def clear_playback_states(self) -> None:
        """Drop state notifications left over from an earlier response."""
        self._playback_state = None
        for _expected, waiter in self._playback_waiters:
            if not waiter.done():
                waiter.cancel()
        self._playback_waiters.clear()

    def _publish_playback_state(self, observed: str) -> None:
        """Broadcast one physical state to every matching response waiter."""
        self._playback_state = observed
        remaining = []
        for expected, waiter in self._playback_waiters:
            if waiter.done():
                continue
            if observed == expected:
                waiter.set_result(None)
            else:
                remaining.append((expected, waiter))
        self._playback_waiters = remaining

    async def wait_playback_state(self, expected: str, timeout: float) -> None:
        """Wait until firmware reports the requested physical player state."""
        if expected not in {"started", "stopped"}:
            raise ValueError("invalid HAVPE playback state")
        if self._playback_state == expected:
            return
        waiter = asyncio.get_running_loop().create_future()
        entry = (expected, waiter)
        self._playback_waiters.append(entry)
        try:
            await asyncio.wait_for(waiter, timeout=timeout)
        finally:
            if entry in self._playback_waiters:
                self._playback_waiters.remove(entry)

    async def disconnect(self) -> None:
        """Disconnect from device."""
        self._connected = False
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        logger.info("ESPHome API disconnected")
