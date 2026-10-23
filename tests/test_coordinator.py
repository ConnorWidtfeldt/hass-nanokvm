"""Test polling, event state, and resource ownership through Home Assistant."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nanokvm.api import (
    AuthenticationError,
    NanoKvmClient,
    NanoKvmConnectionError,
)
from custom_components.nanokvm.api.models import (
    Account,
    CaptureStatus,
    Event,
    KeyboardLeds,
    VideoClients,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("entity_registry_enabled_by_default")


@pytest.mark.parametrize(
    ("event", "entity", "state"),
    [
        (
            KeyboardLeds(
                known=False, num_lock=False, caps_lock=False, scroll_lock=False
            ),
            "binary_sensor.kvm_test_caps_lock",
            "unknown",
        ),
        (
            CaptureStatus(
                ok=True,
                result=0,
                message="private",
                mode="mjpeg",
                severity="info",
                updated_at="2026-01-01T00:00:00Z",
            ),
            "binary_sensor.kvm_test_mjpeg_capture",
            "on",
        ),
        (
            VideoClients(
                generation="fixture", revision=1, direct=1, webrtc=1, mixed=True
            ),
            "binary_sensor.kvm_test_h_264_viewer_conflict",
            "on",
        ),
    ],
)
async def test_notifications(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    event: Event,
    entity: str,
    state: str,
) -> None:
    """Initial snapshots update state without creating synthetic event entities."""

    async def events(ws: object | None = None) -> AsyncIterator[Event]:
        yield event
        await asyncio.Event().wait()

    mock_client.events.side_effect = events
    mock_client.led_values.side_effect = NanoKvmClient.led_values
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == state
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    mock_client.close.assert_awaited_once()


async def test_poll_failure(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Failed local reads make state unavailable and successful polls recover it."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_status.side_effect = NanoKvmConnectionError()
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kvm_test_power").state == "unavailable"
    mock_client.get_status.side_effect = None
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kvm_test_power").state == "on"


@pytest.mark.parametrize("error", [AuthenticationError(), NanoKvmConnectionError()])
async def test_update_feed_failure(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    """The remote update feed does not determine local-device availability."""
    mock_client.get_versions.side_effect = error
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kvm_test_power").state == "on"
    assert hass.states.get("update.kvm_test_application").state == "unavailable"


async def test_standard_account(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A standard user can monitor power without probing administrator routes."""
    mock_client.get_account.return_value = Account(username="test", role="user")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kvm_test_power").state == "on"
    mock_client.get_configuration.assert_not_awaited()
    mock_client.get_versions.assert_not_awaited()


@pytest.mark.parametrize("error", [AuthenticationError(), NanoKvmConnectionError()])
async def test_event_failure(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    """Losing events clears event-only state without reviving unavailable polling."""

    async def events(ws: object | None = None) -> AsyncIterator[Event]:
        raise error
        yield

    mock_client.events.side_effect = events
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kvm_test_mjpeg_capture").state == "unknown"
    assert hass.states.get("binary_sensor.kvm_test_power").state == "on"


async def test_initial_poll_auth_failure(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A session revoked after login enters reauthentication and closes resources."""
    mock_client.get_status.side_effect = AuthenticationError()
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    mock_client.close.assert_awaited_once()


async def test_reconnect_refresh(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reconnecting reads fresh power state before consuming new snapshots."""
    mock_client.open_websocket.side_effect = [NanoKvmConnectionError(), MagicMock()]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_status.return_value = {
        **mock_client.get_status.return_value,
        "power": False,
    }
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert mock_client.open_websocket.await_count == 2
    assert hass.states.get("binary_sensor.kvm_test_power").state == "off"


async def test_cancel_subscription_during_refresh(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Removing the final consumer closes a socket before event iteration starts."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.nanokvm.PLATFORMS", []):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    state = mock_config_entry.runtime_data.state
    started = asyncio.Event()

    async def refresh() -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(state, "async_request_refresh", side_effect=refresh):
        remove = state.async_add_listener(lambda: None, "caps_lock")
        await started.wait()
        remove()
        await hass.async_block_till_done()
    mock_client.events.assert_not_called()
    mock_client.close_websocket.assert_awaited_once_with(
        mock_client.open_websocket.return_value
    )


@pytest.mark.parametrize("context", ["power_button", "capture_mjpeg"])
async def test_unpolled_consumers_check_availability(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    context: str,
) -> None:
    """Button-only and event-only consumers retain an HTTP reachability check."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.nanokvm.PLATFORMS", []):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    state = mock_config_entry.runtime_data.state
    remove = state.async_add_listener(lambda: None, context)
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_client.get_status.assert_awaited_with({context, "power"})
    mock_client.get_status.side_effect = NanoKvmConnectionError()
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not state.last_update_success
    mock_client.get_status.side_effect = None
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state.last_update_success
    remove()
