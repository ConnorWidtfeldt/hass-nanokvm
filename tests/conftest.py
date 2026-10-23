"""Shared sanitized NanoKVM fixtures."""

import asyncio
from collections.abc import AsyncIterator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion
from yarl import URL

from custom_components.nanokvm.api.models import Account, Event, Info, Versions


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Replace the device boundary without mocking Home Assistant."""
    fixture = load_json_object_fixture("device.json")

    async def events(ws: object | None = None) -> AsyncIterator[Event]:
        await asyncio.Event().wait()
        yield

    with (
        patch("custom_components.nanokvm.NanoKvmClient", autospec=True) as constructor,
        patch(
            "custom_components.nanokvm.config_flow.NanoKvmClient",
            new=constructor,
        ),
    ):
        client = constructor.return_value
        client.url = URL("http://192.0.2.10")
        client.get_info.return_value = Info.from_dict(fixture["info"])
        client.get_hardware.return_value = "PCIE"
        client.get_account.return_value = Account(username="test", role="admin")
        client.get_status.return_value = fixture["status"]
        client.get_configuration.return_value = fixture["configuration"]
        client.get_versions.return_value = Versions(current="2.5.2", latest="2.5.2")
        client.events.side_effect = events
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a device entry with no real credentials."""
    return MockConfigEntry(
        domain="nanokvm",
        title="kvm-test",
        unique_id="fixture-device",
        data={
            "url": "http://192.0.2.10",
            "username": "test",
            "password": "fixture",
            "fingerprint": None,
            "macs": ["00:11:22:33:44:55"],
        },
    )


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Let config-flow tests finish without starting platforms."""
    with patch(
        "custom_components.nanokvm.async_setup_entry", return_value=True
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components during tests."""


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use Home Assistant's state and registry snapshot formatting."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Exercise entities that are disabled by default in production."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default", True
    ):
        yield
