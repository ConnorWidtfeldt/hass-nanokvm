"""NanoKVM application updates."""

import asyncio
from typing import Any, override

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AuthenticationError, NanoKvmConnectionError, NanoKvmError
from .const import DOMAIN
from .coordinator import NanoKvmConfigEntry, NanoKvmUpdateCoordinator
from .entity import device_info

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Expose updates for administrator accounts."""
    if entry.runtime_data.admin:
        await entry.runtime_data.firmware.async_refresh()
        async_add_entities([NanoKvmUpdate(entry)])


class NanoKvmUpdate(CoordinatorEntity[NanoKvmUpdateCoordinator], UpdateEntity):
    """Install an application release and verify its installed version."""

    _attr_has_entity_name = True
    _attr_translation_key = "application"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, entry: NanoKvmConfigEntry) -> None:
        """Initialize identity and update state."""
        super().__init__(entry.runtime_data.firmware)
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_application"
        self._attr_device_info = device_info(entry)

    @property
    @override
    def installed_version(self) -> str | None:
        """Return the version reported by the device."""
        return self.runtime.info.application

    @property
    @override
    def latest_version(self) -> str | None:
        """Return the version offered by the selected update channel."""
        return self.coordinator.data.latest if self.coordinator.data else None

    @property
    @override
    def in_progress(self) -> bool:
        """Report a running install without inventing percentage progress."""
        return self.coordinator.installing

    @override
    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Start once, then observe the device until the release is installed."""
        if self.coordinator.installing:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="update_unavailable"
            )
        self.coordinator.installing = True
        self.async_write_ha_state()
        try:
            await self.coordinator.async_refresh()
            target = self.latest_version
            previous = self.installed_version
            if (
                not self.coordinator.last_update_success
                or not target
                or target == previous
                or (version is not None and version != target)
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="update_unavailable"
                )
            try:
                await self.runtime.async_command(
                    self.runtime.client.update_application, refresh=False
                )
            except HomeAssistantError as err:
                if not isinstance(err.__cause__, NanoKvmConnectionError):
                    raise
            async with asyncio.timeout(300):
                while True:
                    await asyncio.sleep(5)
                    try:
                        info = await self.runtime.client.get_info()
                    except AuthenticationError as err:
                        self.coordinator.config_entry.async_start_reauth(self.hass)
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="invalid_auth"
                        ) from err
                    except NanoKvmError:
                        continue
                    # Firmware resolves the current release again when installation starts.
                    if info.application != previous:
                        self.runtime.async_update_version(info.application)
                        await self.coordinator.async_request_refresh()
                        return
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="update_timeout"
            ) from err
        finally:
            self.coordinator.installing = False
            self.async_write_ha_state()
