"""Allowlisted NanoKVM diagnostics."""

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import NanoKvmConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NanoKvmConfigEntry
) -> dict[str, Any]:
    """Exclude identity, credentials, URLs, paths, and device-supplied messages."""
    data = entry.runtime_data
    return {
        "hardware": data.hardware,
        "application": data.info.application,
        "image": data.info.image,
        "admin": data.admin,
        "state_available": data.state.last_update_success,
        "configuration_available": data.configuration.last_update_success,
        "updates_available": data.firmware.last_update_success,
        "enabled_state_fields": sorted(data.state.async_contexts()),
        "enabled_configuration_fields": sorted(data.configuration.async_contexts()),
    }
