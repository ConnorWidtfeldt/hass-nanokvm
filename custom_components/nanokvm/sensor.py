"""NanoKVM runtime and media diagnostics."""

from typing import override

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NanoKvmConfigEntry
from .entity import NanoKvmEntity

PARALLEL_UPDATES = 0
STATE_KEYS = ("input_resolution",)
CONFIG_KEYS = (
    "mounted_image",
    "tailscale",
    "control_mode",
    "picoclaw",
    "download_status",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up optional diagnostics."""
    keys = (*STATE_KEYS, *CONFIG_KEYS) if entry.runtime_data.admin else STATE_KEYS
    async_add_entities(
        NanoKvmSensor(
            entry,
            SensorEntityDescription(
                key=key,
                translation_key=key,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
            ),
            configuration=key in CONFIG_KEYS,
        )
        for key in keys
    )


class NanoKvmSensor(NanoKvmEntity, SensorEntity):
    """A reported runtime value."""

    @property
    @override
    def native_value(self) -> str | int | float | None:
        """Return the current value."""
        return self.coordinator.data.get(self.entity_description.key)
