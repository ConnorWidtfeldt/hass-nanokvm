"""Test NanoKVM lifecycle, entities, and command boundaries."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanokvm import async_unload_entry
from custom_components.nanokvm.api import AuthenticationError, NanoKvmConnectionError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir


async def test_setup_unload(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Load actual platforms and release owned streams."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.kvm_test_power").state == "on"
    assert hass.states.get("binary_sensor.kvm_test_caps_lock").state == "unknown"
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    mock_client.close.assert_awaited_once()


async def test_firmware_repair(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A firmware downgrade raises a repair which clears after a supported upgrade."""
    info = mock_client.get_info.return_value
    mock_client.get_info.return_value = replace(info, application="2.5.1")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert (
        issue_registry.async_get_issue("nanokvm", mock_config_entry.entry_id)
        is not None
    )
    mock_client.get_info.return_value = info
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    assert issue_registry.async_get_issue("nanokvm", mock_config_entry.entry_id) is None


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (AuthenticationError(), ConfigEntryState.SETUP_ERROR),
        (NanoKvmConnectionError(), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_error(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    """Credentials and transient failures have different recovery behavior."""
    mock_client.authenticate.side_effect = error
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is state
    mock_client.close.assert_awaited_once()


async def test_press_once(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Dispatch an explicit command once and refresh observed state."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "nanokvm",
        "press_button",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "button": "power",
            "duration": 800,
        },
        blocking=True,
    )
    mock_client.press_button.assert_awaited_once_with("power", 800)
    assert mock_client.get_status.await_count >= 2


@pytest.mark.parametrize(
    ("hardware", "version", "identity"),
    [
        ("PRO", "2.5.2", "fixture-device"),
        ("PCIE", "2.5.1", "fixture-device"),
        ("PCIE", "invalid", "fixture-device"),
        ("PCIE", "2.5.2", "another-device"),
    ],
)
async def test_setup_identity_and_support(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    hardware: str,
    version: str,
    identity: str,
) -> None:
    """Reload cannot start controls after replacement or firmware downgrade."""
    mock_client.get_hardware.return_value = hardware
    mock_client.get_info.return_value = replace(
        mock_client.get_info.return_value, application=version, device_key=identity
    )
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    mock_client.get_status.assert_not_awaited()
    mock_client.close.assert_awaited_once()


async def test_failed_unload_keeps_client(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Keep the client usable when a platform cannot unload."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await async_unload_entry(hass, mock_config_entry)
    mock_client.close.assert_not_awaited()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    mock_client.close.assert_awaited_once()
