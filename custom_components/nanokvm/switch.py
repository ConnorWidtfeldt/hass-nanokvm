"""NanoKVM readable toggles."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import NanoKvmClient
from .coordinator import NanoKvmConfigEntry
from .entity import NanoKvmEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class NanoKvmSwitchDescription(SwitchEntityDescription):
    """A setting with authoritative readback."""

    set_value: Callable[[NanoKvmClient, bool], Awaitable[None]]


SWITCHES = (
    NanoKvmSwitchDescription(
        key="jiggler",
        translation_key="jiggler",
        entity_category=EntityCategory.CONFIG,
        set_value=lambda client, value: client.set_jiggler(enabled=value),
    ),
    NanoKvmSwitchDescription(
        key="hdmi",
        translation_key="hdmi",
        entity_category=EntityCategory.CONFIG,
        set_value=lambda client, value: client.set_setting("hdmi", value),
    ),
    NanoKvmSwitchDescription(
        key="preview",
        translation_key="preview",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        set_value=lambda client, value: client.set_setting("preview", value),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up administrator settings."""
    if entry.runtime_data.admin:
        async_add_entities(
            NanoKvmSwitch(entry, description, configuration=description.key != "hdmi")
            for description in SWITCHES
            if description.key != "hdmi" or entry.runtime_data.hardware == "PCIE"
        )


class NanoKvmSwitch(NanoKvmEntity, SwitchEntity):
    """A setting controlled without optimistic state."""

    entity_description: NanoKvmSwitchDescription

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the device's setting."""
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, bool) else None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting."""
        await self.runtime.async_command(
            lambda: self.entity_description.set_value(self.runtime.client, True)
        )
        if self.entity_description.key == "preview":
            await self.runtime.firmware.async_refresh()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting."""
        await self.runtime.async_command(
            lambda: self.entity_description.set_value(self.runtime.client, False)
        )
        if self.entity_description.key == "preview":
            await self.runtime.firmware.async_refresh()
