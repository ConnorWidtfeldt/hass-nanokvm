"""Coordinate NanoKVM state, configuration, and update checks."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import logging
from typing import Any, Literal, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AuthenticationError,
    NanoKvmClient,
    NanoKvmError,
    NanoKvmPermissionError,
)
from .api.models import (
    CaptureStatus,
    Info,
    KeyboardLeds,
    Values,
    Versions,
    VideoClients,
)
from .const import CONFIG_INTERVAL, DOMAIN, STATUS_INTERVAL, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

EVENT_FIELDS = {
    "num_lock",
    "caps_lock",
    "scroll_lock",
    "mixed_video",
    "capture_direct",
    "capture_h264",
    "capture_mjpeg",
}
STATUS_FIELDS = {
    "power",
    "hdd",
    "hdmi",
    "hdmi_signal",
    "hdmi_idle_timeout",
    "num_lock",
    "caps_lock",
    "scroll_lock",
}
CONFIG_FIELDS = {
    "jiggler",
    "jiggler_mode",
    "oled_sleep",
}

type NanoKvmConfigEntry = ConfigEntry[NanoKvmData]


@dataclass
class NanoKvmData:
    """Resources owned by a config entry."""

    client: NanoKvmClient
    info: Info
    hardware: str
    admin: bool
    state: NanoKvmCoordinator
    configuration: NanoKvmCoordinator
    firmware: NanoKvmUpdateCoordinator

    @callback
    def async_update_version(self, version: str) -> None:
        """Keep observed application versions consistent with device metadata."""
        self.info = replace(self.info, application=version)
        registry = dr.async_get(self.state.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, self.info.device_key), self.state.config_entry.entry_id
        ):
            registry.async_update_device(device.id, sw_version=version)

    async def async_command[T](
        self, command: Callable[[], Awaitable[T]], *, refresh: bool = True
    ) -> T:
        """Translate command failures without exposing device response contents."""
        try:
            result = await command()
        except AuthenticationError as err:
            self.state.config_entry.async_start_reauth(self.state.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except NanoKvmPermissionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="permission_denied"
            ) from err
        except NanoKvmError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err
        if refresh:
            await self.state.async_request_refresh()
            if self.admin:
                await self.configuration.async_request_refresh()
        return result


class NanoKvmCoordinator(DataUpdateCoordinator[Values]):
    """Poll enabled consumers and merge device notifications."""

    config_entry: NanoKvmConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanoKvmConfigEntry,
        client: NanoKvmClient,
        kind: Literal["state", "configuration"],
    ) -> None:
        """Initialize a polling group."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"NanoKVM {kind}",
            update_interval=STATUS_INTERVAL if kind == "state" else CONFIG_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.kind = kind
        self._event_task: asyncio.Task[None] | None = None

    @callback
    @override
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        """Subscribe only while an enabled entity consumes runtime events."""
        remove = super().async_add_listener(update_callback, context)
        self._async_update_subscription()

        @callback
        def unsubscribe() -> None:
            remove()
            self._async_update_subscription()

        return unsubscribe

    @callback
    def _async_update_subscription(self) -> None:
        needed = self.kind == "state" and bool(
            EVENT_FIELDS.intersection(self.async_contexts())
        )
        if needed and self._event_task is None:
            self._event_task = self.config_entry.async_create_background_task(
                self.hass, self.async_listen(), "NanoKVM events"
            )
        elif not needed and self._event_task is not None:
            self._event_task.cancel()
            self._event_task = None

    @override
    async def _async_update_data(self) -> Values:
        fields = set(self.async_contexts()) or (
            STATUS_FIELDS if self.kind == "state" else CONFIG_FIELDS
        )
        if self.kind == "state" and not fields.intersection(
            STATUS_FIELDS | {"input_resolution"}
        ):
            fields.add("power")
        try:
            values = await (
                self.client.get_status(fields)
                if self.kind == "state"
                else self.client.get_configuration(fields)
            )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except NanoKvmError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="state_update_failed"
            ) from err
        return {**(self.data or {}), **values}

    async def async_listen(self) -> None:
        """Reconnect notifications without replaying input or control commands."""
        delay = 1
        while True:
            try:
                ws = await self.client.open_websocket()
                try:
                    await self.async_request_refresh()
                    async for event in self.client.events(ws):
                        delay = 1
                        match event:
                            case KeyboardLeds():
                                values = self.client.led_values(event)
                            case VideoClients():
                                values = {
                                    "direct_viewers": event.direct,
                                    "webrtc_viewers": event.webrtc,
                                    "mixed_video": event.mixed,
                                }
                            case CaptureStatus():
                                values = {f"capture_{event.mode}": event.ok}
                        self.data = {**self.data, **values}
                        self.async_update_listeners()
                finally:
                    await self.client.close_websocket(ws)
            except AuthenticationError:
                self.config_entry.async_start_reauth(self.hass)
                return
            except NanoKvmError:
                self.data = {
                    **self.data,
                    "direct_viewers": None,
                    "webrtc_viewers": None,
                    "mixed_video": None,
                    "capture_direct": None,
                    "capture_h264": None,
                    "capture_mjpeg": None,
                }
                self.async_update_listeners()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


class NanoKvmUpdateCoordinator(DataUpdateCoordinator[Versions]):
    """Keep Internet-dependent update checks separate from local controls."""

    config_entry: NanoKvmConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: NanoKvmConfigEntry, client: NanoKvmClient
    ) -> None:
        """Initialize update checks."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="NanoKVM updates",
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.installing = False

    @override
    async def _async_update_data(self) -> Versions:
        try:
            versions = await self.client.get_versions()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except NanoKvmError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="update_check_failed"
            ) from err
        self.config_entry.runtime_data.async_update_version(versions.current)
        return versions
