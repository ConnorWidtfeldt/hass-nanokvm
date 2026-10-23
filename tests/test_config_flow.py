"""Exercise NanoKVM setup through Home Assistant."""

from dataclasses import replace
from ipaddress import ip_address
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanokvm.api import AuthenticationError, NanoKvmConnectionError
from custom_components.nanokvm.api.models import Info
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

DATA = {"url": "http://192.0.2.10", "username": "test", "password": "fixture"}
pytestmark = pytest.mark.usefixtures("mock_client", "mock_setup_entry")


async def test_user(hass: HomeAssistant) -> None:
    """Authenticate before creating a stable entry."""
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "fixture-device"
    assert result["title"] == "kvm-test"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AuthenticationError(), "invalid_auth"),
        (NanoKvmConnectionError(), "cannot_connect"),
    ],
)
async def test_recover(
    hass: HomeAssistant, mock_client: MagicMock, error: Exception, reason: str
) -> None:
    """A failed connection can recover in the same flow."""
    mock_client.authenticate.side_effect = error
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": reason}
    mock_client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "fixture-device"


async def test_duplicate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Manual setup cannot duplicate a discovered entry."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("hardware", "version", "reason"),
    [("PRO", "2.5.2", "unsupported_device"), ("PCIE", "2.5.1", "unsupported_firmware")],
)
async def test_unsupported(
    hass: HomeAssistant,
    mock_client: MagicMock,
    hardware: str,
    version: str,
    reason: str,
) -> None:
    """Reject untested hardware and firmware baselines."""
    mock_client.get_hardware.return_value = hardware
    mock_client.get_info.return_value = Info(
        device_key="fixture-device",
        application=version,
        image="v1.3.0",
        mdns="kvm-test.local",
    )
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("start_reauth_flow", "reauth_successful"),
        ("start_reconfigure_flow", "reconfigure_successful"),
    ],
)
async def test_update(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    source: str,
    reason: str,
) -> None:
    """Reauthentication and reconfiguration retain identity, including no TLS pin."""
    mock_config_entry.add_to_hass(hass)
    result = await getattr(mock_config_entry, source)(hass)
    mock_client.authenticate.side_effect = AuthenticationError
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.authenticate.side_effect = None
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_discovery(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Workstation port nine must never become the API port."""
    result = await hass.config_entries.flow.async_init(
        "nanokvm",
        context={"source": SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("192.0.2.10"),
            ip_addresses=[ip_address("192.0.2.10")],
            port=9,
            hostname="kvm-test.local.",
            type="_workstation._tcp.local.",
            name="kvm-test [00:11:22:33:44:55]._workstation._tcp.local.",
            properties={},
        ),
    )
    assert result["step_id"] == "confirm"
    mock_client.authenticate.side_effect = NanoKvmConnectionError
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "cannot_connect"}
    mock_client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "fixture-device"
    assert result["data"]["url"] == "http://192.0.2.10"
    assert result["data"]["macs"] == ["00:11:22:33:44:55"]


async def test_invalid_fingerprint(hass: HomeAssistant) -> None:
    """Reject an incomplete certificate pin before sending credentials."""
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "fingerprint": "abcd"}
    )
    assert result["errors"] == {"fingerprint": "invalid_fingerprint"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "fixture-device"


async def test_invalid_url(hass: HomeAssistant) -> None:
    """A constructor validation failure returns to the same form."""
    with patch(
        "custom_components.nanokvm.config_flow.NanoKvmClient",
        side_effect=ValueError,
    ):
        result = await hass.config_entries.flow.async_init(
            "nanokvm", context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**DATA, "url": "ftp://192.0.2.10"}
        )
    assert result["errors"] == {"url": "invalid_url"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "fixture-device"


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("fixture-device", "https://192.0.2.20:8443"),
        ("other-device", "https://192.0.2.10:8443"),
    ],
)
async def test_dhcp_address(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    identity: str,
    expected: str,
) -> None:
    """Verify rediscovered identity while preserving HTTPS, port, and pin."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            "url": "https://192.0.2.10:8443",
            "fingerprint": "aa" * 32,
        },
    )
    mock_client.get_info.return_value = Info(
        device_key=identity, application="2.5.2", image="v1.3.0", mdns="renamed.local"
    )
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            "nanokvm",
            context={"source": "dhcp"},
            data=DhcpServiceInfo(
                ip="192.0.2.20", macaddress="001122334455", hostname="renamed"
            ),
        )
    assert result["type"] is FlowResultType.ABORT
    assert mock_config_entry.data["url"] == expected
    assert mock_config_entry.data["fingerprint"] == "aa" * 32


@pytest.mark.parametrize(
    ("ip", "mac"), [("192.0.2.10", "001122334455"), ("192.0.2.20", "000000000000")]
)
async def test_dhcp_no_change(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    ip: str,
    mac: str,
) -> None:
    """Known addresses and unrelated DHCP clients do not probe credentials."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "nanokvm",
        context={"source": "dhcp"},
        data=DhcpServiceInfo(ip=ip, macaddress=mac, hostname="renamed"),
    )
    assert result["type"] is FlowResultType.ABORT
    mock_client.authenticate.assert_not_awaited()


async def test_dhcp_failure(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Do not overwrite a trusted endpoint when probing fails."""
    mock_config_entry.add_to_hass(hass)
    mock_client.authenticate.side_effect = AuthenticationError()
    result = await hass.config_entries.flow.async_init(
        "nanokvm",
        context={"source": "dhcp"},
        data=DhcpServiceInfo(
            ip="192.0.2.20", macaddress="001122334455", hostname="renamed"
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert mock_config_entry.data["url"] == DATA["url"]


async def test_identity_mismatch(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfiguration cannot silently replace the registered device."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_info.return_value = Info(
        device_key="different", application="2.5.2", image="v1.3.0", mdns="other.local"
    )
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


async def test_invalid_version(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """An unparsable application version cannot satisfy the baseline."""
    mock_client.get_info.return_value = Info(
        device_key="fixture-device",
        application="not-a-version",
        image="v1.3.0",
        mdns="kvm-test.local",
    )
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_firmware"


async def test_concurrent_discoveries(hass: HomeAssistant) -> None:
    """Two advertisements for the same host share one pending flow."""
    discovery = ZeroconfServiceInfo(
        ip_address=ip_address("192.0.2.10"),
        ip_addresses=[ip_address("192.0.2.10")],
        port=9,
        hostname="kvm-test.local.",
        type="_workstation._tcp.local.",
        name="kvm-test._workstation._tcp.local.",
        properties={},
    )
    first = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_ZEROCONF}, data=discovery
    )
    second = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_ZEROCONF}, data=discovery
    )
    assert first["type"] is FlowResultType.FORM
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"


async def test_discovered_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An already registered address does not need another confirmation flow."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "nanokvm",
        context={"source": SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("192.0.2.10"),
            ip_addresses=[ip_address("192.0.2.10")],
            port=9,
            hostname="kvm-test.local.",
            type="_workstation._tcp.local.",
            name="kvm-test._workstation._tcp.local.",
            properties={},
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_entry_learns_mac(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    """A later advertisement enables address recovery for a manually added device."""
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    entry = result["result"]
    assert entry.data["macs"] == []
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("nanokvm", "fixture-device")}
    )
    discovery = ZeroconfServiceInfo(
        ip_address=ip_address("192.0.2.10"),
        ip_addresses=[ip_address("192.0.2.10")],
        port=9,
        hostname="kvm-test.local.",
        type="_workstation._tcp.local.",
        name="kvm-test [00:11:22:33:44:55]._workstation._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        "nanokvm", context={"source": SOURCE_ZEROCONF}, data=discovery
    )
    assert result["reason"] == "already_configured"
    assert entry.data["macs"] == ["00:11:22:33:44:55"]
    assert device_registry.async_get(device.id).connections == {
        (dr.CONNECTION_NETWORK_MAC, "00:11:22:33:44:55")
    }
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ) as reload:
        result = await hass.config_entries.flow.async_init(
            "nanokvm",
            context={"source": "dhcp"},
            data=DhcpServiceInfo(
                ip="192.0.2.20", macaddress="001122334455", hostname="renamed"
            ),
        )
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://192.0.2.20"
    reload.assert_awaited_once_with(entry.entry_id)


async def test_mac_learning_rejects_replacement(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A matching address does not establish the identity of a replacement device."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, "macs": []}
    )
    mock_client.get_info.return_value = replace(
        mock_client.get_info.return_value, device_key="replacement"
    )
    await hass.config_entries.flow.async_init(
        "nanokvm",
        context={"source": "dhcp"},
        data=DhcpServiceInfo(
            ip="192.0.2.10", macaddress="001122334455", hostname="renamed"
        ),
    )
    assert mock_config_entry.data["macs"] == []
