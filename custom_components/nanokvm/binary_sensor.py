"""NanoKVM power, signal, and keyboard status."""

from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NanoKvmConfigEntry
from .entity import NanoKvmEntity

PARALLEL_UPDATES = 0

SENSORS = (
    BinarySensorEntityDescription(
        key="power", translation_key="power", device_class=BinarySensorDeviceClass.POWER
    ),
    BinarySensorEntityDescription(
        key="hdd",
        translation_key="hdd",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="hdmi_signal",
        translation_key="hdmi_signal",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    *(
        BinarySensorEntityDescription(
            key=key, translation_key=key, entity_category=EntityCategory.DIAGNOSTIC
        )
        for key in ("num_lock", "caps_lock", "scroll_lock")
    ),
    *(
        BinarySensorEntityDescription(
            key=key,
            translation_key=key,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )
        for key in ("mixed_video", "capture_direct", "capture_h264", "capture_mjpeg")
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create only sensors supported by the hardware."""
    async_add_entities(
        NanoKvmBinarySensor(entry, description)
        for description in SENSORS
        if (description.key != "hdd" or entry.runtime_data.hardware == "Alpha")
        and (description.key != "hdmi_signal" or entry.runtime_data.hardware == "PCIE")
    )
    if entry.runtime_data.admin:
        async_add_entities(
            NanoKvmBinarySensor(
                entry,
                BinarySensorEntityDescription(
                    key=key,
                    translation_key=key,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                ),
                configuration=True,
            )
            for key in ("usb_network", "usb_disk", "mcp")
        )


class NanoKvmBinarySensor(NanoKvmEntity, BinarySensorEntity):
    """A reported boolean, preserving unknown state."""

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the observed value."""
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, bool) else None
