"""Test application update confirmation and ambiguity handling."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanokvm.api import AuthenticationError, NanoKvmConnectionError
from custom_components.nanokvm.api.models import Versions
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr


@pytest.mark.parametrize("command_error", [None, NanoKvmConnectionError()])
async def test_install(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    command_error: Exception | None,
) -> None:
    """Confirm an installed version after success or an ambiguous request timeout."""
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.3")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.update_application.side_effect = command_error
    mock_client.get_info.return_value = replace(
        mock_client.get_info.return_value, application="2.5.3"
    )
    mock_client.get_versions.side_effect = [
        Versions(current="2.5.2", latest="2.5.3"),
        Versions(current="2.5.3", latest="2.5.3"),
    ]
    with patch("custom_components.nanokvm.update.asyncio", wraps=asyncio) as clock:
        clock.sleep = AsyncMock()
        await hass.services.async_call(
            "update",
            "install",
            {"entity_id": "update.kvm_test_application"},
            blocking=True,
        )
    mock_client.update_application.assert_awaited_once()
    state = hass.states.get("update.kvm_test_application")
    assert state.attributes["installed_version"] == "2.5.3"
    assert state.attributes["in_progress"] is False
    device = device_registry.async_get_device_by_identifier(
        ("nanokvm", "fixture-device"), mock_config_entry.entry_id
    )
    assert device.sw_version == "2.5.3"


async def test_update_timeout(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Never report completion when the installed version has not changed."""
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.3")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    with patch("custom_components.nanokvm.update.asyncio", wraps=asyncio) as clock:
        clock.timeout.side_effect = TimeoutError
        with pytest.raises(
            HomeAssistantError, match="installed version has not changed"
        ):
            await hass.services.async_call(
                "update",
                "install",
                {"entity_id": "update.kvm_test_application"},
                blocking=True,
            )
    mock_client.update_application.assert_awaited_once()
    assert (
        hass.states.get("update.kvm_test_application").attributes["installed_version"]
        == "2.5.2"
    )


@pytest.mark.parametrize("error_at", ["command", "observation"])
async def test_update_authentication(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    error_at: str,
) -> None:
    """Revoked sessions during installation require reauthentication."""
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.3")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    method = {
        "command": mock_client.update_application,
        "observation": mock_client.get_info,
    }[error_at]
    method.side_effect = AuthenticationError()
    with patch("custom_components.nanokvm.update.asyncio", wraps=asyncio) as clock:
        clock.sleep = AsyncMock()
        with pytest.raises(HomeAssistantError, match="session expired"):
            await hass.services.async_call(
                "update",
                "install",
                {"entity_id": "update.kvm_test_application"},
                blocking=True,
            )
    mock_client.update_application.assert_awaited_once()


async def test_reboot_observation(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Wait through reboot connection loss without repeating installation."""
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.3")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    installed = replace(mock_client.get_info.return_value, application="2.5.3")
    mock_client.get_info.side_effect = [NanoKvmConnectionError(), installed]
    mock_client.get_versions.side_effect = [
        Versions(current="2.5.2", latest="2.5.3"),
        Versions(current="2.5.3", latest="2.5.3"),
    ]
    with patch("custom_components.nanokvm.update.asyncio", wraps=asyncio) as clock:
        clock.sleep = AsyncMock()
        await hass.services.async_call(
            "update",
            "install",
            {"entity_id": "update.kvm_test_application"},
            blocking=True,
        )
    mock_client.update_application.assert_awaited_once()
    assert (
        hass.states.get("update.kvm_test_application").attributes["installed_version"]
        == "2.5.3"
    )


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_channel_refresh(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    service: str,
) -> None:
    """Both channel transitions immediately refresh the offered release."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.4")
    await hass.services.async_call(
        "switch",
        service,
        {"entity_id": "switch.kvm_test_preview_updates"},
        blocking=True,
    )
    assert (
        hass.states.get("update.kvm_test_application").attributes["latest_version"]
        == "2.5.4"
    )


@pytest.mark.parametrize("installed", ["2.5.4", "2.5.5"])
async def test_release_changes(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    installed: str,
) -> None:
    """Observe actual installation when the feed changes before or during updating."""
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.3")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_versions.side_effect = [
        Versions(current="2.5.2", latest="2.5.4"),
        Versions(current=installed, latest=installed),
    ]
    mock_client.get_info.return_value = replace(
        mock_client.get_info.return_value, application=installed
    )
    with patch("custom_components.nanokvm.update.asyncio", wraps=asyncio) as clock:
        clock.sleep = AsyncMock()
        await hass.services.async_call(
            "update",
            "install",
            {"entity_id": "update.kvm_test_application"},
            blocking=True,
        )
    mock_client.update_application.assert_awaited_once()
    assert (
        hass.states.get("update.kvm_test_application").attributes["installed_version"]
        == installed
    )


async def test_preflight_feed_failure(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Do not install using cached metadata when refreshing the release feed fails."""
    mock_client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.3")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_versions.side_effect = NanoKvmConnectionError()
    with pytest.raises(HomeAssistantError, match="update is unavailable"):
        await hass.services.async_call(
            "update",
            "install",
            {"entity_id": "update.kvm_test_application"},
            blocking=True,
        )
    mock_client.update_application.assert_not_awaited()
