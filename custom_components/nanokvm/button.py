"""NanoKVM momentary controls."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import NanoKvmClient
from .coordinator import NanoKvmConfigEntry
from .entity import NanoKvmEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class NanoKvmButtonDescription(ButtonEntityDescription):
    """A one-shot device command."""

    press: Callable[[NanoKvmClient], Awaitable[object]]
    admin: bool = False
    pcie: bool = False


BUTTONS = (
    NanoKvmButtonDescription(
        key="power_button",
        translation_key="power_button",
        press=lambda client: client.press_button("power"),
    ),
    NanoKvmButtonDescription(
        key="reset_button",
        translation_key="reset_button",
        entity_registry_enabled_default=False,
        press=lambda client: client.press_button("reset"),
    ),
    NanoKvmButtonDescription(
        key="force_off",
        translation_key="force_off",
        entity_registry_enabled_default=False,
        press=lambda client: client.press_button("power", 5000),
    ),
    NanoKvmButtonDescription(
        key="reboot",
        translation_key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        admin=True,
        press=lambda client: client.reboot(),
    ),
    NanoKvmButtonDescription(
        key="reset_hid",
        translation_key="reset_hid",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        admin=True,
        press=lambda client: client.reset_hid(),
    ),
    NanoKvmButtonDescription(
        key="reset_hdmi",
        translation_key="reset_hdmi",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        pcie=True,
        press=lambda client: client.reset_hdmi(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanoKvmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up momentary controls."""
    async_add_entities(
        NanoKvmButton(entry, description)
        for description in BUTTONS
        if (not description.admin or entry.runtime_data.admin)
        and (not description.pcie or entry.runtime_data.hardware == "PCIE")
    )


class NanoKvmButton(NanoKvmEntity, ButtonEntity):
    """An ATX pulse or device maintenance command."""

    entity_description: NanoKvmButtonDescription

    @override
    async def async_press(self) -> None:
        """Send the command once."""
        await self.runtime.async_command(
            lambda: self.entity_description.press(self.runtime.client)
        )
