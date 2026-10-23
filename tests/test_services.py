"""Test explicit device actions and failure translation."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanokvm.api import (
    AuthenticationError,
    NanoKvmConnectionError,
    NanoKvmPermissionError,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component


@pytest.mark.parametrize(
    ("service", "fields", "method", "args", "kwargs"),
    [
        (
            "set_control_mode",
            {"mode": "off"},
            "set_setting",
            ("control_mode", "off"),
            {},
        ),
        ("send_text", {"text": "hello"}, "send_text", ("hello", ""), {}),
        ("send_keys", {"keys": [4]}, "send_keys", ([4], 0), {}),
        (
            "move_pointer",
            {"x": 10, "y": 20},
            "mouse",
            (10, 20),
            {"relative": False, "buttons": 0, "wheel": 0},
        ),
        (
            "set_usb_device",
            {"device": "disk", "enabled": False},
            "set_virtual_device",
            ("disk", False),
            {},
        ),
        ("mount_image", {}, "mount_image", ("",), {"cdrom": False}),
        (
            "configure_video",
            {"setting": "gop", "value": 30},
            "configure_video",
            ("gop", 30),
            {},
        ),
        ("set_frame_detection", {"enabled": True}, "set_frame_detection", (True,), {}),
        (
            "set_input_region",
            {"region": {"mode": "off"}},
            "set_input_region",
            ({"mode": "off"},),
            {},
        ),
        ("run_script", {"name": "test.sh"}, "run_script", ("test.sh",), {}),
        (
            "wake_on_lan",
            {"mac": "00:11:22:33:44:55"},
            "wake_on_lan",
            ("00:11:22:33:44:55",),
            {},
        ),
        (
            "control_extension",
            {"extension": "mcp", "action": "stop"},
            "control_extension",
            ("mcp", "stop"),
            {},
        ),
    ],
)
async def test_actions(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    service: str,
    fields: dict[str, object],
    method: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    """Each documented action maps to its specific client operation."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "nanokvm",
        service,
        {"config_entry_id": mock_config_entry.entry_id, **fields},
        blocking=True,
    )
    getattr(mock_client, method).assert_awaited_once_with(*args, **kwargs)


@pytest.mark.parametrize(
    ("error", "translation"),
    [
        (AuthenticationError(), "invalid_auth"),
        (NanoKvmPermissionError(), "permission_denied"),
        (NanoKvmConnectionError(), "command_failed"),
        (ValueError(), "invalid_parameter"),
    ],
)
async def test_action_errors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    error: Exception,
    translation: str,
) -> None:
    """Translate failures, including ambiguous timeouts, without replaying commands."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.press_button.side_effect = error
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "nanokvm",
            "press_button",
            {"config_entry_id": mock_config_entry.entry_id, "button": "reset"},
            blocking=True,
        )
    assert raised.value.translation_key == translation
    mock_client.press_button.assert_awaited_once()


async def test_unloaded(hass: HomeAssistant) -> None:
    """Actions remain registered and explain an unloaded target."""
    assert await async_setup_component(hass, "nanokvm", {})
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await hass.services.async_call(
            "nanokvm",
            "press_button",
            {"config_entry_id": "missing", "button": "power"},
            blocking=True,
        )
