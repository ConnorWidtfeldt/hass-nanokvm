"""NanoKVM operating modes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast, override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import NanoKvmClient
from .coordinator import NanoKvmConfigEntry
from .entity import NanoKvmEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class NanoKvmSelectDescription(SelectEntityDescription):
    """A finite device setting."""

    set_value: Callable[[NanoKvmClient, str], Awaitable[None]]


SELECTS = (
    NanoKvmSelectDescription(
        key="jiggler_mode",
        translation_key="jiggler_mode",
        options=["relative", "absolute"],
        entity_category=EntityCategory.CONFIG,
        set_value=lambda client, value: client.set_jiggler(
            mode=cast(Literal["relative", "absolute"], value)
        ),
    ),
    NanoKvmSelectDescription(
        key="hid_mode",
        translation_key="hid_mode",
        options=["normal", "hid-only"],
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        set_value=lambda client, value: client.set_setting("hid_mode", value),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up administrator mode controls."""
    if entry.runtime_data.admin:
        async_add_entities(
            NanoKvmSelect(entry, description, configuration=True)
            for description in SELECTS
        )


class NanoKvmSelect(NanoKvmEntity, SelectEntity):
    """A mode confirmed by device readback."""

    entity_description: NanoKvmSelectDescription

    @property
    @override
    def current_option(self) -> str | None:
        """Return the active mode."""
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, str) else None

    @override
    async def async_select_option(self, option: str) -> None:
        """Change the mode."""
        await self.runtime.async_command(
            lambda: self.entity_description.set_value(self.runtime.client, option)
        )
