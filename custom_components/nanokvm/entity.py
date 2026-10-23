"""Common NanoKVM entity metadata."""

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NanoKvmConfigEntry, NanoKvmCoordinator


class NanoKvmEntity(CoordinatorEntity[NanoKvmCoordinator]):
    """A value in one NanoKVM polling group."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: NanoKvmConfigEntry,
        description: EntityDescription,
        *,
        configuration: bool = False,
    ) -> None:
        """Initialize stable entity identity and its polling context."""
        self.runtime = entry.runtime_data
        super().__init__(
            self.runtime.configuration if configuration else self.runtime.state,
            context=description.key,
        )
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = device_info(entry)


def device_info(entry: NanoKvmConfigEntry) -> DeviceInfo:
    """Describe the KVM rather than the computer attached to it."""
    data = entry.runtime_data
    return DeviceInfo(
        identifiers={(DOMAIN, data.info.device_key)},
        connections={
            (CONNECTION_NETWORK_MAC, mac) for mac in entry.data.get("macs", [])
        },
        manufacturer="Sipeed",
        model="NanoKVM PCIe" if data.hardware == "PCIE" else f"NanoKVM {data.hardware}",
        name=entry.title,
        sw_version=data.info.application,
        hw_version=data.hardware,
        configuration_url=str(data.client.url),
    )
