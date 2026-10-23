"""NanoKVM display timeouts."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import NanoKvmClient
from .coordinator import NanoKvmConfigEntry
from .entity import NanoKvmEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class NanoKvmNumberDescription(NumberEntityDescription):
    """A bounded integer device setting."""

    set_value: Callable[[NanoKvmClient, int], Awaitable[None]]


NUMBERS = (
    NanoKvmNumberDescription(
        key="oled_sleep",
        translation_key="oled_sleep",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=3600,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        set_value=lambda client, value: client.set_setting("oled_sleep", value),
    ),
    NanoKvmNumberDescription(
        key="hdmi_idle_timeout",
        translation_key="hdmi_idle_timeout",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=10080,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        set_value=lambda client, value: client.set_setting("hdmi_idle_timeout", value),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up hardware-supported timeouts."""
    if entry.runtime_data.admin:
        async_add_entities(
            NanoKvmNumber(
                entry, description, configuration=description.key == "oled_sleep"
            )
            for description in NUMBERS
            if (
                description.key != "oled_sleep"
                or entry.runtime_data.configuration.data.get("oled_sleep") is not None
            )
            and (
                description.key != "hdmi_idle_timeout"
                or entry.runtime_data.hardware == "PCIE"
            )
        )


class NanoKvmNumber(NanoKvmEntity, NumberEntity):
    """A timeout read from the device."""

    entity_description: NanoKvmNumberDescription

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current timeout."""
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, int) else None

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write and refresh the timeout."""
        await self.runtime.async_command(
            lambda: self.entity_description.set_value(self.runtime.client, int(value))
        )
