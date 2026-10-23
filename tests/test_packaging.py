"""Verify installation without Home Assistant Core's generated integration lists."""

import json
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import (
    async_get_dhcp,
    async_get_integration,
    async_get_zeroconf,
)


async def test_custom_discovery(hass: HomeAssistant) -> None:
    """Load the custom manifest and register both discovery mechanisms."""
    integration = await async_get_integration(hass, "nanokvm")
    assert not integration.is_built_in
    assert str(integration.version) == "0.1.0"
    zeroconf = await async_get_zeroconf(hass)
    assert {"domain": "nanokvm", "name": "kvm-*"} in zeroconf[
        "_workstation._tcp.local."
    ]
    assert {"domain": "nanokvm", "registered_devices": True} in await async_get_dhcp(
        hass
    )


def test_bundled_translations() -> None:
    """Ship English translations without depending on Core's translation generator."""
    component = Path(__file__).parent.parent / "custom_components" / "nanokvm"
    assert json.loads((component / "translations/en.json").read_text()) == json.loads(
        (component / "strings.json").read_text()
    )
