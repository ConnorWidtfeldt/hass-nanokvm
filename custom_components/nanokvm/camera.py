"""Opt-in NanoKVM video preview."""

from typing import override

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import NanoKvmConfigEntry, NanoKvmCoordinator
from .entity import device_info

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the optional preview."""
    if entry.options.get("allow_preview", False):
        async_add_entities([NanoKvmCamera(entry)])


class NanoKvmCamera(CoordinatorEntity[NanoKvmCoordinator], Camera):
    """Read JPEG frames only after explicit encoder-sharing consent."""

    _attr_has_entity_name = True
    _attr_translation_key = "preview"
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: NanoKvmConfigEntry) -> None:
        """Initialize camera identity."""
        super().__init__(entry.runtime_data.state, context="power")
        Camera.__init__(self)
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_preview"
        self._attr_device_info = device_info(entry)

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return an unmodified source frame."""
        return await self.runtime.async_command(self.runtime.client.jpeg, refresh=False)
