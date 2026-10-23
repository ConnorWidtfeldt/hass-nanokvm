"""Configure NanoKVM through the UI or local discovery."""

from collections.abc import Mapping
import re
from typing import Any, Self, override

from aiohttp import DummyCookieJar
from awesomeversion import AwesomeVersion, AwesomeVersionException
import probatio
from yarl import URL

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import AuthenticationError, NanoKvmClient, NanoKvmError
from .const import CONF_FINGERPRINT, DOMAIN, MIN_VERSION


class NanoKvmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one authenticated NanoKVM."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize discovery defaults."""
        self._defaults: dict[str, Any] = {}
        self._macs: list[str] = []
        self._host: str | None = None

    @override
    def is_matching(self, other_flow: Self) -> bool:
        """Coalesce concurrent discoveries before credentials are available."""
        return self._host is not None and self._host == other_flow._host

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate credentials and identity before saving an entry."""
        return await self._async_form("user", user_input)

    async def _async_form(
        self, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._defaults, **user_input}
            fingerprint = (data.get(CONF_FINGERPRINT) or "").replace(":", "").lower()
            if fingerprint and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                errors[CONF_FINGERPRINT] = "invalid_fingerprint"
            else:
                data[CONF_FINGERPRINT] = fingerprint or None
                session = async_create_clientsession(
                    self.hass, auto_cleanup=False, cookie_jar=DummyCookieJar()
                )
                try:
                    client = NanoKvmClient(
                        data[CONF_URL],
                        session,
                        fingerprint=data[CONF_FINGERPRINT],
                    )
                except ValueError:
                    session.detach()
                    errors[CONF_URL] = "invalid_url"
                else:
                    try:
                        await client.authenticate(
                            data[CONF_USERNAME], data[CONF_PASSWORD]
                        )
                        info = await client.get_info()
                        hardware = await client.get_hardware()
                        await client.get_account()
                    except AuthenticationError:
                        errors["base"] = "invalid_auth"
                    except NanoKvmError:
                        errors["base"] = "cannot_connect"
                    else:
                        if hardware not in ("Alpha", "Beta", "PCIE"):
                            return self.async_abort(reason="unsupported_device")
                        try:
                            supported = AwesomeVersion(
                                info.application
                            ) >= AwesomeVersion(MIN_VERSION)
                        except AwesomeVersionException:
                            supported = False
                        if not supported:
                            return self.async_abort(reason="unsupported_firmware")
                        await self.async_set_unique_id(info.device_key)
                        data[CONF_URL] = str(client.url)
                        data["macs"] = self._macs or data.get("macs", [])
                        options = {"allow_preview": data.pop("allow_preview", False)}
                        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
                            self._abort_if_unique_id_mismatch()
                            entry = (
                                self._get_reauth_entry()
                                if self.source == SOURCE_REAUTH
                                else self._get_reconfigure_entry()
                            )
                            return self.async_update_reload_and_abort(
                                entry, data_updates=data, options=options
                            )
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=info.mdns.removesuffix(".local") or "NanoKVM",
                            data=data,
                            options=options,
                        )
                    finally:
                        await client.close()
                        session.detach()
        schema = probatio.Schema(
            {
                probatio.Required(CONF_URL): str,
                probatio.Required(CONF_USERNAME): str,
                probatio.Required(probatio.Secret(CONF_PASSWORD)): str,
                probatio.Optional(CONF_FINGERPRINT): str,
                probatio.Optional("allow_preview", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id=step,
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or self._defaults
            ),
            errors=errors,
        )

    @override
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Discover a workstation without mistaking its port 9 for the web server."""
        if match := re.search(r"\[([0-9a-fA-F:]{17})\]", discovery_info.name):
            self._macs = [format_mac(match[1])]
        host = self._host = discovery_info.host
        if self.hass.config_entries.flow.async_has_matching_flow(self):
            return self.async_abort(reason="already_in_progress")
        if await self._async_update_address(host, self._macs):
            return self.async_abort(reason="already_configured")
        self._defaults = {
            CONF_URL: str(URL.build(scheme="http", host=host)),
            CONF_USERNAME: "admin",
        }
        self._async_abort_entries_match({CONF_URL: self._defaults[CONF_URL]})
        self.context["title_placeholders"] = {
            "name": discovery_info.hostname.removesuffix(".local.")
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery and authenticate."""
        return await self._async_form("confirm", user_input)

    @override
    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Refresh addresses of registered devices only."""
        await self._async_update_address(
            discovery_info.ip, [format_mac(discovery_info.macaddress)]
        )
        return self.async_abort(reason="already_configured")

    async def _async_update_address(self, host: str, macs: list[str]) -> bool:
        for entry in self._async_current_entries():
            previous = URL(entry.data[CONF_URL])
            known_macs = set(entry.data.get("macs", []))
            if previous.host == host and set(macs) <= known_macs:
                return True
            if previous.host != host and not known_macs.intersection(macs):
                continue
            candidate = previous.with_host(host)
            session = async_create_clientsession(
                self.hass, auto_cleanup=False, cookie_jar=DummyCookieJar()
            )
            client = NanoKvmClient(
                str(candidate),
                session,
                fingerprint=entry.data.get(CONF_FINGERPRINT),
            )
            try:
                await client.authenticate(
                    entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
                )
                info = await client.get_info()
            except NanoKvmError:
                return True
            finally:
                await client.close()
                session.detach()
            if info.device_key == entry.unique_id:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_URL: str(candidate),
                        "macs": sorted(known_macs.union(macs)),
                    },
                )
                registry = dr.async_get(self.hass)
                if device := registry.async_get_device_by_identifier(
                    (DOMAIN, info.device_key), entry.entry_id
                ):
                    registry.async_update_device(
                        device.id,
                        new_connections=device.connections
                        | {(dr.CONNECTION_NETWORK_MAC, mac) for mac in macs},
                    )
                if previous != candidate:
                    await self.hass.config_entries.async_reload(entry.entry_id)
            return True
        return False

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Replace credentials without creating another device."""
        self._defaults = {**entry_data, **self._get_reauth_entry().options}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials."""
        return await self._async_form("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the endpoint while retaining device identity."""
        entry = self._get_reconfigure_entry()
        self._defaults = {**entry.data, **entry.options}
        return await self._async_form("reconfigure", user_input)
