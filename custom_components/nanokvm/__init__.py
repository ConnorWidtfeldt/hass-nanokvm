"""The NanoKVM integration."""

from aiohttp import DummyCookieJar
from awesomeversion import AwesomeVersion, AwesomeVersionException

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import AuthenticationError, NanoKvmClient, NanoKvmError
from .api.models import Info
from .const import CONF_FINGERPRINT, DOMAIN, MIN_VERSION
from .coordinator import (
    NanoKvmConfigEntry,
    NanoKvmCoordinator,
    NanoKvmData,
    NanoKvmUpdateCoordinator,
)
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


def _validate_device(info: Info, hardware: str, unique_id: str | None) -> None:
    """Reject replacement hardware and firmware downgrades before starting controls."""
    if hardware not in ("Alpha", "Beta", "PCIE"):
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="unsupported_device"
        )
    try:
        supported = AwesomeVersion(info.application) >= AwesomeVersion(MIN_VERSION)
    except AwesomeVersionException:
        supported = False
    if not supported:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="unsupported_firmware"
        )
    if info.device_key != unique_id:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="identity_mismatch"
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions independently of device availability."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NanoKvmConfigEntry) -> bool:
    """Connect and set up the device."""
    client = NanoKvmClient(
        entry.data[CONF_URL],
        async_create_clientsession(hass, cookie_jar=DummyCookieJar()),
        fingerprint=entry.data.get(CONF_FINGERPRINT),
    )
    try:
        await client.authenticate(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
        info = await client.get_info()
        hardware = await client.get_hardware()
        account = await client.get_account()
        _validate_device(info, hardware, entry.unique_id)
        ir.async_delete_issue(hass, DOMAIN, entry.entry_id)
        state = NanoKvmCoordinator(hass, entry, client, "state")
        configuration = NanoKvmCoordinator(hass, entry, client, "configuration")
        firmware = NanoKvmUpdateCoordinator(hass, entry, client)
        await state.async_config_entry_first_refresh()
        admin = account.role == "admin"
        if admin:
            await configuration.async_config_entry_first_refresh()
        else:
            configuration.async_set_updated_data({})
    except AuthenticationError as err:
        await client.close()
        raise ConfigEntryAuthFailed from err
    except NanoKvmError as err:
        await client.close()
        raise ConfigEntryNotReady from err
    except ConfigEntryError as err:
        if err.translation_key == "unsupported_firmware":
            ir.async_create_issue(
                hass,
                DOMAIN,
                entry.entry_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="unsupported_firmware",
            )
        await client.close()
        raise
    except Exception:
        await client.close()
        raise
    entry.runtime_data = NanoKvmData(
        client, info, hardware, admin, state, configuration, firmware
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanoKvmConfigEntry) -> bool:
    """Unload entities and close the client's streams."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.close()
    return unloaded
