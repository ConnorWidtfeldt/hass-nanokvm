"""Test entity state and public actions through Home Assistant."""

from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.camera import async_get_image
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")


@pytest.mark.parametrize(
    "platform",
    [
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
        Platform.SWITCH,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.SENSOR,
        Platform.UPDATE,
        Platform.CAMERA,
    ],
)
async def test_entities(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    platform: Platform,
) -> None:
    """Snapshot every platform's public state and registry metadata."""
    mock_client.get_configuration.return_value["oled_sleep"] = 60
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"allow_preview": True}
    )
    with (
        patch("custom_components.nanokvm.PLATFORMS", [platform]),
        patch(
            "homeassistant.components.camera._RND.getrandbits",
            return_value=12345,
        ),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        await snapshot_platform(
            hass, entity_registry, snapshot, mock_config_entry.entry_id
        )


@pytest.mark.parametrize(
    ("domain", "service", "entity", "fields", "method", "args", "kwargs"),
    [
        (
            "button",
            "press",
            "button.kvm_test_power_button",
            {},
            "press_button",
            ("power",),
            {},
        ),
        (
            "switch",
            "turn_on",
            "switch.kvm_test_mouse_jiggler",
            {},
            "set_jiggler",
            (),
            {"enabled": True},
        ),
        (
            "switch",
            "turn_off",
            "switch.kvm_test_hdmi_capture",
            {},
            "set_setting",
            ("hdmi", False),
            {},
        ),
        (
            "number",
            "set_value",
            "number.kvm_test_hdmi_idle_timeout",
            {"value": 30},
            "set_setting",
            ("hdmi_idle_timeout", 30),
            {},
        ),
        (
            "select",
            "select_option",
            "select.kvm_test_usb_input_mode",
            {"option": "hid-only"},
            "set_setting",
            ("hid_mode", "hid-only"),
            {},
        ),
    ],
)
async def test_entity_actions(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    domain: str,
    service: str,
    entity: str,
    fields: dict[str, object],
    method: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    """Entity actions dispatch once and use confirmed device state."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        domain, service, {"entity_id": entity, **fields}, blocking=True
    )
    getattr(mock_client, method).assert_awaited_once_with(*args, **kwargs)
    assert hass.states.get("switch.kvm_test_mouse_jiggler").state == "off"


async def test_preview(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Preview is explicit and does not write encoder settings or refresh configuration."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"allow_preview": True}
    )
    mock_client.jpeg.return_value = b"\xff\xd8fixture\xff\xd9"
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_configuration.reset_mock()
    image = await async_get_image(hass, "camera.kvm_test_preview")
    assert image.content == b"\xff\xd8fixture\xff\xd9"
    mock_client.configure_video.assert_not_awaited()
    mock_client.get_configuration.assert_not_awaited()


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Diagnostics omit all identity, credentials, paths, and input contents."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
        == snapshot
    )
