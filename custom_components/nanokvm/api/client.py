"""Authenticated HTTP and WebSocket access to NanoKVM 2.5.2."""

import asyncio
import base64
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import os
import re
from typing import Literal
from urllib.parse import quote

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    DummyCookieJar,
    Fingerprint,
    FormData,
    MultipartReader,
    WSMsgType,
    WSServerHandshakeError,
)
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import probatio
from yarl import URL

from .models import (
    Account,
    CaptureStatus,
    Event,
    Gpio,
    Hardware,
    Hdmi,
    Info,
    Jiggler,
    JsonValue,
    KeyboardLeds,
    Model,
    Oled,
    Payload,
    Resolution,
    Values,
    Versions,
    VideoClients,
    VirtualDevices,
    input_region_payload,
    json_object,
)

_COOKIE = "nano-kvm-token"


class NanoKvmError(Exception):
    """Device operation failed."""


class AuthenticationError(NanoKvmError):
    """Credentials or the session are no longer valid."""


class NanoKvmPermissionError(NanoKvmError):
    """The authenticated account lacks the required role."""


class NanoKvmConnectionError(NanoKvmError):
    """The device could not complete the request."""


class ProtocolError(NanoKvmError):
    """The response does not match the firmware contract."""


def _segment(value: str) -> str:
    """Validate a single firmware filename or account identifier."""
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("Expected a single name")
    return quote(value, safe="")


def _password(value: str) -> str:
    """Encode the firmware's CryptoJS password format without transport security."""
    salt = os.urandom(8)
    material = block = b""
    while len(material) < 48:
        block = hashlib.md5(block + b"nanokvm-sipeed-2024" + salt).digest()
        material += block
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(material[:32]), modes.CBC(material[32:48])
    ).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return quote(base64.b64encode(b"Salted__" + salt + encrypted).decode(), safe="")


class NanoKvmClient:
    """Use an injected session without changing or closing it."""

    def __init__(
        self, url: str, session: ClientSession, *, fingerprint: str | None = None
    ) -> None:
        """Bind a device to a caller-owned session with cookie storage disabled."""
        self.url = URL(url)
        if (
            self.url.scheme not in ("http", "https")
            or not self.url.host
            or self.url.user is not None
            or self.url.query_string
            or self.url.fragment
            or self.url.path not in ("", "/")
        ):
            raise ValueError("Expected an HTTP or HTTPS device URL")
        if not isinstance(session.cookie_jar, DummyCookieJar):
            raise TypeError(
                "The injected session must use DummyCookieJar for cookie isolation"
            )
        if fingerprint and self.url.scheme != "https":
            raise ValueError("A certificate fingerprint requires HTTPS")
        self._session = session
        self._ssl: bool | Fingerprint = (
            Fingerprint(bytes.fromhex(fingerprint)) if fingerprint else True
        )
        self._token = ""
        self._websockets: set[ClientWebSocketResponse] = set()
        self._media_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self.hardware = ""

    def _headers(self) -> dict[str, str]:
        # Keep credentials scoped to this client, including during WebSocket upgrades.
        return {"Cookie": f"{_COOKIE}={self._token}"}

    @asynccontextmanager
    async def _response(
        self,
        method: str,
        path: str,
        *,
        data: Payload | None = None,
        timeout: float | None = 10,
        form: FormData | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[ClientResponse]:
        try:
            async with self._session.request(
                method,
                self.url.join(URL("/api/" + path)),
                json=data,
                data=form,
                headers={**self._headers(), **(headers or {})},
                ssl=self._ssl,
                timeout=ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise AuthenticationError("Authentication required")
                if response.status == 403:
                    raise NanoKvmPermissionError("Administrator role required")
                if response.status != 200:
                    raise ProtocolError(f"Unexpected HTTP status {response.status}")
                yield response
        except (ClientError, TimeoutError) as err:
            raise NanoKvmConnectionError("Device request failed") from err

    async def request(
        self,
        method: str,
        path: str,
        data: Payload | None = None,
        *,
        timeout: float = 10,
        form: FormData | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonValue:
        """Execute a documented API operation once, without replaying commands."""
        async with self._response(
            method, path, data=data, timeout=timeout, form=form, headers=headers
        ) as response:
            try:
                body = json_object(await response.read())
            except probatio.Invalid, ValueError:
                raise ProtocolError("Invalid JSON response") from None
            if type(body.get("code")) not in (int, str):
                raise ProtocolError("Missing API result code")
            if body["code"] != 0:
                raise NanoKvmError("Device rejected operation")
            return body.get("data")

    async def read[T: Model](self, path: str, model: type[T]) -> T:
        """Read and validate a typed response."""
        try:
            return model.from_dict(await self.request("GET", path))
        except probatio.Invalid:
            raise ProtocolError(f"Invalid response for {path}") from None

    async def authenticate(self, username: str, password: str) -> None:
        """Authenticate using the stable firmware's HttpOnly cookie."""
        await self.close()
        self._token = ""
        async with self._response(
            "POST",
            "auth/login",
            data={"username": username, "password": _password(password)},
        ) as response:
            try:
                body = json_object(await response.read())
            except probatio.Invalid, ValueError:
                raise ProtocolError("Invalid login response") from None
            if body.get("code") != 0:
                raise AuthenticationError("Login rejected")
            cookie = response.cookies.get(_COOKIE)
            if cookie is None or not cookie.value:
                raise AuthenticationError("Login did not issue a session")
            self._token = cookie.value

    async def get_info(self) -> Info:
        """Read stable identity without contacting the update server."""
        return await self.read("vm/info", Info)

    async def get_hardware(self) -> str:
        """Read and remember the hardware revision."""
        self.hardware = (await self.read("vm/hardware", Hardware)).version
        return self.hardware

    async def get_account(self) -> Account:
        """Read the current role."""
        return await self.read("auth/account", Account)

    async def get_status(self, fields: set[str]) -> Values:
        """Read only the operational groups requested by consumers."""
        values: Values = {}
        if fields & {"power", "hdd"}:
            gpio = await self.read("vm/gpio", Gpio)
            values.update(power=gpio.pwr, hdd=gpio.hdd)
        if (
            fields & {"hdmi", "hdmi_signal", "hdmi_idle_timeout"}
            and self.hardware == "PCIE"
        ):
            hdmi = await self.read("vm/hdmi", Hdmi)
            values.update(
                hdmi=hdmi.enabled,
                hdmi_signal=hdmi.signal,
                hdmi_idle_timeout=hdmi.idle_timeout,
            )
        if fields & {"num_lock", "caps_lock", "scroll_lock"}:
            leds = await self.read("hid/leds", KeyboardLeds)
            values.update(self.led_values(leds))
        if "input_resolution" in fields:
            try:
                resolution = await self.read("vm/input-resolution", Resolution)
            except AuthenticationError, NanoKvmConnectionError:
                raise
            except NanoKvmError:
                values["input_resolution"] = None
            else:
                values["input_resolution"] = f"{resolution.width}x{resolution.height}"
        return values

    @staticmethod
    def led_values(leds: KeyboardLeds) -> Values:
        """Convert known host LED states to independently addressable values."""
        return {
            key: getattr(leds, key) if leds.known else None
            for key in ("num_lock", "caps_lock", "scroll_lock")
        }

    async def get_configuration(self, fields: set[str]) -> Values:
        """Read optional configuration groups without fetching credentials."""
        values: Values = {}
        if fields & {"jiggler", "jiggler_mode"}:
            jiggler = await self.read("vm/mouse-jiggler", Jiggler)
            values.update(jiggler=jiggler.enabled, jiggler_mode=jiggler.mode)
        if "oled_sleep" in fields:
            oled = await self.read("vm/oled", Oled)
            values["oled_sleep"] = oled.sleep if oled.exist else None
        if fields & {"usb_network", "usb_disk"}:
            devices = await self.read("vm/device/virtual", VirtualDevices)
            values.update(usb_network=devices.network, usb_disk=devices.disk)
        for key, path, field in (
            ("hid_mode", "hid/mode", "mode"),
            ("mounted_image", "storage/image/mounted", "file"),
            ("preview", "application/preview", "enabled"),
            ("tailscale", "extensions/tailscale/status", "state"),
            ("control_mode", "ai/control/status", "mode"),
            ("mcp", "ai/control/status", "mode"),
            ("picoclaw", "picoclaw/runtime/status", "status"),
            ("download_status", "download/image/status", "status"),
        ):
            if key not in fields:
                continue
            try:
                result = await self.request("GET", path)
            except AuthenticationError, NanoKvmConnectionError:
                raise
            except NanoKvmError:
                values[key] = None
                continue
            if not isinstance(result, dict) or not isinstance(
                result.get(field), (str, bool)
            ):
                raise ProtocolError(f"Invalid response for {path}")
            value = result[field]
            assert isinstance(value, str | bool)
            values[key] = value == "mcp" if key == "mcp" else value
        return values

    async def get_password_status(self) -> JsonValue:
        """Read password status."""
        return await self.request("GET", "auth/password")

    async def get_users(self) -> JsonValue:
        """Read users."""
        return await self.request("GET", "auth/users")

    async def get_scripts(self) -> JsonValue:
        """Read scripts."""
        return await self.request("GET", "vm/script")

    async def get_memory_limit(self) -> JsonValue:
        """Read memory limit."""
        return await self.request("GET", "vm/memory/limit")

    async def get_ssh(self) -> JsonValue:
        """Read ssh."""
        return await self.request("GET", "vm/ssh")

    async def get_swap(self) -> JsonValue:
        """Read swap."""
        return await self.request("GET", "vm/swap")

    async def get_hostname(self) -> JsonValue:
        """Read hostname."""
        return await self.request("GET", "vm/hostname")

    async def get_web_title(self) -> JsonValue:
        """Read web title."""
        return await self.request("GET", "vm/web-title")

    async def get_mdns(self) -> JsonValue:
        """Read mdns."""
        return await self.request("GET", "vm/mdns")

    async def get_autostart_scripts(self) -> JsonValue:
        """Read autostart scripts."""
        return await self.request("GET", "vm/autostart")

    async def get_shortcuts(self) -> JsonValue:
        """Read shortcuts."""
        return await self.request("GET", "hid/shortcuts")

    async def get_leader_key(self) -> JsonValue:
        """Read leader key."""
        return await self.request("GET", "hid/shortcut/leader-key")

    async def get_images(self) -> JsonValue:
        """Read images."""
        return await self.request("GET", "storage/image")

    async def get_cdrom(self) -> JsonValue:
        """Read cdrom."""
        return await self.request("GET", "storage/cdrom")

    async def get_saved_macs(self) -> JsonValue:
        """Read saved macs."""
        return await self.request("GET", "network/wol/mac")

    async def get_wifi(self) -> JsonValue:
        """Read wifi."""
        return await self.request("GET", "network/wifi")

    async def get_dns(self) -> JsonValue:
        """Read dns."""
        return await self.request("GET", "network/dns")

    async def get_update_server(self) -> JsonValue:
        """Read update server."""
        return await self.request("GET", "application/update-server")

    async def get_mcp_config(self) -> JsonValue:
        """Read mcp config."""
        return await self.request("GET", "mcp/config")

    async def get_picoclaw_sessions(self) -> JsonValue:
        """Read picoclaw sessions."""
        return await self.request("GET", "picoclaw/sessions")

    async def get_picoclaw_status(self) -> JsonValue:
        """Read picoclaw status."""
        return await self.request("GET", "picoclaw/runtime/status")

    async def get_download_enabled(self) -> JsonValue:
        """Read download enabled."""
        return await self.request("GET", "download/image/enabled")

    async def get_download_status(self) -> JsonValue:
        """Read download status."""
        return await self.request("GET", "download/image/status")

    async def get_input_region(self) -> JsonValue:
        """Read input region."""
        return await self.request("GET", "vm/input-region")

    async def get_tailscale_status(self) -> JsonValue:
        """Read tailscale status."""
        return await self.request("GET", "extensions/tailscale/status")

    async def logout(self) -> JsonValue:
        """Logout once."""
        return await self.request("POST", "auth/logout")

    async def regenerate_mcp_key(self) -> JsonValue:
        """Regenerate mcp key once."""
        return await self.request("POST", "mcp/key/regenerate")

    async def cancel_image_download(self) -> JsonValue:
        """Cancel image download once."""
        return await self.request("POST", "download/image/cancel")

    async def disconnect_wifi(self) -> JsonValue:
        """Disconnect wifi once."""
        return await self.request("POST", "network/wifi/disconnect")

    async def change_password(self, current_password: str, password: str) -> None:
        """Change this account's password, potentially revoking existing sessions."""
        await self.request(
            "POST",
            "auth/password",
            {
                "currentPassword": _password(current_password),
                "password": _password(password),
            },
        )

    async def create_user(
        self, username: str, password: str, role: Literal["admin", "user"]
    ) -> None:
        """Create a device account."""
        await self.request(
            "POST",
            "auth/users",
            {"username": username, "password": _password(password), "role": role},
        )

    async def update_user(
        self,
        username: str,
        *,
        role: Literal["admin", "user"] | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Change only the supplied account properties."""
        data: Payload = {}
        if role is not None:
            data["role"] = role
        if enabled is not None:
            data["enabled"] = enabled
        if not data:
            raise ValueError("Specify a role or enabled state")
        await self.request("PUT", "auth/users/" + _segment(username), data)

    async def delete_user(self, username: str) -> None:
        """Delete an account and revoke its sessions."""
        await self.request("DELETE", "auth/users/" + _segment(username))

    async def change_user_password(self, username: str, password: str) -> None:
        """Reset a non-owner account's password."""
        await self.request(
            "POST",
            "auth/users/" + _segment(username) + "/password",
            {"password": _password(password)},
        )

    async def delete_script(self, name: str) -> None:
        """Delete a saved script."""
        _segment(name)
        await self.request("DELETE", "vm/script", {"name": name})

    async def get_autostart(self, name: str) -> JsonValue:
        """Read an autostart file without executing it."""
        return await self.request("GET", "vm/autostart/" + _segment(name))

    async def set_autostart(self, name: str, content: str) -> None:
        """Replace an autostart file on the device."""
        await self.request(
            "POST", "vm/autostart/" + _segment(name), {"content": content}
        )

    async def delete_autostart(self, name: str) -> None:
        """Remove an autostart file."""
        await self.request("DELETE", "vm/autostart/" + _segment(name))

    async def set_memory_limit(self, enabled: bool, limit: int) -> None:
        """Set the application memory limit in MiB."""
        if limit < 0 or (enabled and limit < 50):
            raise ValueError("Memory limit cannot be negative")
        await self.request(
            "POST", "vm/memory/limit", {"enabled": enabled, "limit": limit}
        )

    async def set_swap(self, size: int) -> None:
        """Set swap size in MiB."""
        if size < 0:
            raise ValueError("Swap size cannot be negative")
        await self.request("POST", "vm/swap", {"size": size}, timeout=300)

    async def set_ssh(self, enabled: bool) -> None:
        """Enable or disable the SSH server."""
        await self.request("POST", "vm/ssh/" + ("enable" if enabled else "disable"))

    async def set_mdns(self, enabled: bool) -> None:
        """Enable or disable mDNS announcements."""
        await self.request("POST", "vm/mdns/" + ("enable" if enabled else "disable"))

    async def set_hostname(self, hostname: str) -> None:
        """Change the device hostname."""
        await self.request("POST", "vm/hostname", {"hostname": hostname})

    async def set_web_title(self, title: str) -> None:
        """Change the device's web title."""
        await self.request("POST", "vm/web-title", {"title": title})

    async def set_tls(self, enabled: bool) -> None:
        """Change TLS configuration and require an explicit reconnect."""
        await self.request("POST", "vm/tls", {"enabled": enabled})

    async def delete_image(self, file: str) -> None:
        """Delete a saved disk image."""
        async with self._media_lock:
            await self.request("POST", "storage/image/delete", {"file": file})

    async def delete_saved_mac(self, mac: str) -> None:
        """Remove a remembered Wake-on-LAN address."""
        await self.request("DELETE", "network/wol/mac", {"mac": mac})

    async def name_saved_mac(self, mac: str, name: str) -> None:
        """Name a remembered Wake-on-LAN address."""
        await self.request("POST", "network/wol/mac/name", {"mac": mac, "name": name})

    async def connect_wifi(self, ssid: str, password: str) -> None:
        """Change the Wi-Fi network, potentially losing the current connection."""
        await self.request(
            "POST",
            "network/wifi/connect",
            {"ssid": ssid, "password": password},
            timeout=60,
        )

    async def set_dns(
        self, mode: Literal["manual", "dhcp"], servers: list[str]
    ) -> None:
        """Set DNS configuration."""
        await self.request(
            "POST", "network/dns", {"mode": mode, "servers": list(servers)}
        )

    async def set_update_server(self, enabled: bool, url: str) -> None:
        """Change the application update source."""
        await self.request(
            "POST", "application/update-server", {"enabled": enabled, "url": url}
        )

    async def add_shortcut(self, keys: list[Payload]) -> JsonValue:
        """Save a named keyboard shortcut in the native UI."""
        return await self.request("POST", "hid/shortcut", {"keys": list(keys)})

    async def delete_shortcut(self, shortcut_id: str) -> None:
        """Remove a saved keyboard shortcut."""
        await self.request("DELETE", "hid/shortcut", {"id": shortcut_id})

    async def set_leader_key(self, key: str) -> None:
        """Set the native UI's shortcut leader key."""
        await self.request("POST", "hid/shortcut/leader-key", {"key": key})

    async def pause_frame_detection(self, duration: int) -> None:
        """Temporarily pause frame detection for a number of seconds."""
        if duration < 0:
            raise ValueError("Duration cannot be negative")
        await self.request(
            "POST",
            "stream/mjpeg/detect/stop",
            {"duration": duration},
            timeout=(duration or 10) + 10,
        )

    async def download_image(self, url: str, sha256: str = "") -> None:
        """Start downloading a disk image to the device."""
        await self.request("POST", "download/image", {"file": url, "sha256sum": sha256})

    async def get_picoclaw_session(self, session_id: str) -> JsonValue:
        """Read a private PicoClaw conversation."""
        return await self.request("GET", "picoclaw/sessions/" + _segment(session_id))

    async def delete_picoclaw_session(self, session_id: str) -> None:
        """Delete a PicoClaw conversation."""
        await self.request("DELETE", "picoclaw/sessions/" + _segment(session_id))

    async def release_picoclaw_session(self) -> JsonValue:
        """Release the active remote-control session."""
        return await self.request("DELETE", "picoclaw/runtime/session")

    async def set_picoclaw_model(self, model: str, api_base: str, api_key: str) -> None:
        """Replace the PicoClaw model configuration without retaining secrets."""
        await self.request(
            "POST",
            "picoclaw/model/config",
            {"model": model, "api_base": api_base, "api_key": api_key},
        )

    async def set_picoclaw_profile(self, profile: Literal["default", "kvm"]) -> None:
        """Select the PicoClaw agent profile."""
        await self.request("POST", "picoclaw/agent/profile", {"profile": profile})

    async def upload_script(self, name: str, content: bytes) -> JsonValue:
        """Upload a script without executing it."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:sh|py)", name):
            raise ValueError("Expected a script filename")
        return await self._upload("vm/script/upload", name, content)

    async def upload_image(
        self, name: str, content: bytes | AsyncIterable[bytes], *, sha256: str = ""
    ) -> JsonValue:
        """Stream an ISO image to the device without mounting it."""
        if ".." in name or not re.fullmatch(r"[A-Za-z0-9_.-]+\.[iI][sS][oO]", name):
            raise ValueError("Expected an ISO filename")
        async with self._media_lock:
            return await self._upload(
                "download/file", name, content, headers={"X-SHA256-Sum": sha256}
            )

    async def update_offline(
        self, name: str, content: bytes | AsyncIterable[bytes], *, sha256: str = ""
    ) -> None:
        """Upload and install one application archive without automatic retries."""
        if not re.fullmatch(r"nanokvm_[A-Za-z0-9_.-]+\.tar\.gz", name):
            raise ValueError("Expected a NanoKVM application archive filename")
        await self._upload(
            "application/update/offline",
            name,
            content,
            headers={"X-SHA256-Checksum": sha256},
        )

    async def _upload(
        self,
        path: str,
        name: str,
        content: bytes | AsyncIterable[bytes],
        *,
        headers: dict[str, str] | None = None,
    ) -> JsonValue:
        form = FormData()
        form.add_field(
            "file", content, filename=name, content_type="application/octet-stream"
        )
        return await self.request("POST", path, form=form, headers=headers, timeout=600)

    async def verify_access_point(self, key: str) -> None:
        """Verify the device's Wi-Fi provisioning key."""
        await self.request("POST", "network/wifi/verify", headers={"X-AP-Key": key})

    async def provision_wifi(self, ssid: str, password: str, key: str) -> None:
        """Connect a device in access-point mode to a Wi-Fi network."""
        await self.request(
            "POST",
            "network/wifi",
            {"ssid": ssid, "password": password},
            headers={"X-AP-Key": key},
            timeout=60,
        )

    async def open_terminal(self) -> ClientWebSocketResponse:
        """Open the native shell transport with message handling owned by the caller."""
        return await self.open_websocket("vm/terminal")

    async def open_webrtc(self) -> ClientWebSocketResponse:
        """Open signaling for a standard WebRTC implementation."""
        return await self.open_websocket("stream/h264")

    async def open_picoclaw_gateway(self) -> ClientWebSocketResponse:
        """Open PicoClaw's native gateway transport."""
        return await self.open_websocket("picoclaw/gateway/ws")

    async def open_direct_h264(self) -> ClientWebSocketResponse:
        """Open binary frames prefixed by a keyframe byte and uint64 LE timestamp."""
        return await self.open_websocket("stream/h264/direct")

    async def get_versions(self) -> Versions:
        """Check the application update feed through the device."""
        return await self.read("application/version", Versions)

    async def reboot(self) -> None:
        """Reboot the KVM, not the attached computer."""
        await self.request("POST", "vm/system/reboot")

    async def reset_hid(self) -> None:
        """Reconnect the emulated USB input devices."""
        await self.request("POST", "hid/reset")

    async def reset_hdmi(self) -> None:
        """Reset the PCIe HDMI capture device."""
        await self.request("POST", "vm/hdmi/reset")

    async def update_application(self) -> None:
        """Start one application update."""
        await self.request("POST", "application/update", timeout=300)

    async def send_text(
        self, text: str, language: Literal["", "de", "fr"] = ""
    ) -> None:
        """Type supported characters using the firmware's keyboard layouts."""
        extra = {
            "": "",
            "de": "\xe4\xc4\xf6\xd6\xfc\xdc\xdf\xb4\xb0\xa7\u20ac\xb2\xb3",
            "fr": "\xe9\xe8\xe7\xe0\xb0\xa8\xa3\xb5\xf9\xa7\u20ac",
        }
        if language not in extra or not 1 <= len(text) <= 833:
            raise ValueError("Unsupported keyboard layout or text length")
        if any(
            not 32 <= ord(char) <= 126 and char not in "\n\t" + extra[language]
            for char in text
        ):
            raise ValueError("Text contains unsupported keyboard characters")
        async with self._input_lock:
            await self.request(
                "POST", "hid/paste", {"content": text, "langue": language}, timeout=60
            )

    async def wake_on_lan(self, mac: str) -> None:
        """Send a magic packet from the KVM's network."""
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
            raise ValueError("Expected a colon-separated MAC address")
        await self.request("POST", "network/wol", {"mac": mac})

    async def run_script(self, name: str, background: bool = True) -> JsonValue:
        """Run a script whose foreground output may contain private data."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:sh|py)", name):
            raise ValueError("Expected a script filename")
        return await self.request(
            "POST",
            "vm/script/run",
            {"name": name, "type": "background" if background else "foreground"},
        )

    async def configure_video(self, setting: str, value: int) -> None:
        """Set an encoder parameter without claiming it can be read back."""
        limits = {
            "type": (0, 1),
            "fps": (10, 60),
            "gop": (1, 100),
            "quality": (1, 10000),
            "resolution": (0, 1080),
        }
        if (
            setting not in limits
            or not limits[setting][0] <= value <= limits[setting][1]
        ):
            raise ValueError("Invalid encoder parameter")
        if setting == "quality" and value not in (
            50,
            60,
            80,
            100,
            1000,
            2000,
            3000,
            5000,
        ):
            raise ValueError("Unsupported quality or bitrate")
        if setting == "resolution" and value not in (0, 480, 600, 720, 1080):
            raise ValueError("Unsupported resolution")
        await self.request("POST", "vm/screen", {"type": setting, "value": value})

    async def set_frame_detection(self, enabled: bool) -> None:
        """Control MJPEG frame-change detection."""
        await self.request("POST", "stream/mjpeg/detect", {"enabled": enabled})

    async def set_input_region(self, region: Payload) -> None:
        """Update the firmware input region settings."""
        validated = input_region_payload(region)
        await self.request(
            "POST",
            "vm/input-region",
            validated,
        )

    async def control_extension(self, extension: str, action: str) -> JsonValue:
        """Control an installed extension without exposing keys or login URLs."""
        if extension == "tailscale" and action in (
            "install",
            "uninstall",
            "login",
            "logout",
            "start",
            "stop",
            "restart",
            "up",
            "down",
        ):
            return await self.request(
                "POST",
                f"extensions/tailscale/{action}",
                timeout=600 if action == "install" else 10,
            )
        if extension == "picoclaw" and action in (
            "install",
            "uninstall",
            "start",
            "stop",
        ):
            return await self.request(
                "POST",
                f"picoclaw/runtime/{action}",
                timeout=10 if action == "install" else 60,
            )
        if extension == "mcp" and action in ("start", "stop"):
            return await self.request(
                "POST", "mcp/config", {"enabled": action == "start"}, timeout=60
            )
        raise ValueError("Unsupported extension action")

    async def press_button(
        self, button: Literal["power", "reset"], duration: int = 800
    ) -> None:
        """Pulse an ATX input once."""
        if button not in ("power", "reset") or not 1 <= duration <= 10000:
            raise ValueError("Invalid ATX button or duration")
        await self.request(
            "POST",
            "vm/gpio",
            {"type": button, "duration": duration},
            timeout=duration / 1000 + 10,
        )

    async def set_jiggler(
        self,
        *,
        enabled: bool | None = None,
        mode: Literal["relative", "absolute"] | None = None,
    ) -> None:
        """Preserve the setting not changed by the caller."""
        if mode is not None and mode not in ("relative", "absolute"):
            raise ValueError("Unsupported jiggler mode")
        async with self._input_lock:
            current = await self.read("vm/mouse-jiggler", Jiggler)
            await self.request(
                "POST",
                "vm/mouse-jiggler/",
                {
                    "enabled": current.enabled if enabled is None else enabled,
                    "mode": current.mode if mode is None else mode,
                },
            )

    async def set_virtual_device(
        self, device: Literal["network", "disk"], enabled: bool
    ) -> None:
        """Serialize the firmware's non-idempotent toggle and confirm its result."""
        if device not in ("network", "disk"):
            raise ValueError("Unsupported USB gadget")
        async with self._media_lock:
            current = await self.read("vm/device/virtual", VirtualDevices)
            if getattr(current, device) != enabled:
                await self.request("POST", "vm/device/virtual", {"device": device})
            actual = await self.read("vm/device/virtual", VirtualDevices)
            if getattr(actual, device) != enabled:
                raise NanoKvmError("USB state changed concurrently")

    async def mount_image(self, file: str = "", *, cdrom: bool = False) -> None:
        """Attach an image, or return to the raw data partition when empty."""
        async with self._media_lock:
            await self.request(
                "POST", "storage/image/mount", {"file": file, "cdrom": cdrom}
            )

    async def set_setting(
        self,
        setting: Literal[
            "hdmi",
            "preview",
            "oled_sleep",
            "hdmi_idle_timeout",
            "hid_mode",
            "control_mode",
        ],
        value: bool | int | str,
    ) -> None:
        """Set one readable configuration value."""
        if setting in ("hdmi", "preview") and type(value) is not bool:
            raise ValueError("Expected a boolean setting")
        if setting in ("oled_sleep", "hdmi_idle_timeout") and (
            type(value) is not int or value < 0
        ):
            raise ValueError("Expected a nonnegative timeout")
        if setting == "hdmi_idle_timeout" and isinstance(value, int) and value > 10080:
            raise ValueError("HDMI timeout exceeds seven days")
        if setting == "hid_mode" and value not in ("normal", "hid-only"):
            raise ValueError("Unsupported HID mode")
        if setting == "control_mode" and value not in ("off", "mcp", "picoclaw"):
            raise ValueError("Unsupported AI control mode")
        match setting:
            case "hdmi":
                await self.request(
                    "POST", f"vm/hdmi/{'enable' if value else 'disable'}"
                )
            case "preview":
                await self.request("POST", "application/preview", {"enable": value})
            case "oled_sleep":
                await self.request("POST", "vm/oled", {"sleep": value})
            case "hdmi_idle_timeout":
                await self.request("POST", "vm/hdmi/timeout", {"minutes": value})
            case "hid_mode":
                await self.request("POST", "hid/mode", {"mode": value})
            case "control_mode":
                await self.request(
                    "PUT", "ai/control/mode", {"mode": value}, timeout=60
                )
            case _:
                raise ValueError("Unsupported setting")

    async def open_websocket(self, path: str = "ws") -> ClientWebSocketResponse:
        """Open a protocol transport using the caller's session and explicit cookie."""
        try:
            ws = await self._session.ws_connect(
                self.url.join(URL("/api/" + path)),
                headers=self._headers(),
                ssl=self._ssl,
                max_msg_size=1024 * 1024,
                heartbeat=30,
            )
        except WSServerHandshakeError as err:
            if err.status == 401:
                raise AuthenticationError("Authentication required") from None
            if err.status == 403:
                raise NanoKvmPermissionError("Administrator role required") from None
            raise NanoKvmConnectionError("WebSocket handshake failed") from None
        except (ClientError, TimeoutError) as err:
            raise NanoKvmConnectionError("WebSocket connection failed") from err
        self._websockets.add(ws)
        return ws

    async def events(
        self, ws: ClientWebSocketResponse | None = None
    ) -> AsyncIterator[Event]:
        """Read typed notifications and ignore unknown future messages."""
        if ws is None:
            ws = await self.open_websocket()
        try:
            while True:
                try:
                    message = await ws.receive(timeout=25)
                except TimeoutError:
                    await ws.send_bytes(b"\x00")
                    continue
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    raise NanoKvmConnectionError("Event stream closed")
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    envelope = json_object(message.data)
                    if envelope.get("type") not in (
                        "capture-status",
                        "h264-mode-status",
                        "hid-led-status",
                    ):
                        continue
                    payload = envelope.get("data")
                    if not isinstance(payload, str):
                        raise ProtocolError("Expected JSON string payload")
                    match envelope.get("type"):
                        case "capture-status":
                            yield CaptureStatus.from_dict(json_object(payload))
                        case "h264-mode-status":
                            yield VideoClients.from_dict(json_object(payload))
                        case "hid-led-status":
                            yield KeyboardLeds.from_dict(json_object(payload))
                except probatio.Invalid, ValueError:
                    raise ProtocolError("Invalid event payload") from None
                await ws.send_bytes(b"\x00")
        except ClientError as err:
            raise NanoKvmConnectionError("Event stream failed") from err
        finally:
            await self.close_websocket(ws)

    async def send_keys(self, keys: list[int], modifiers: int = 0) -> None:
        """Send a USB HID chord and release it on the same connection."""
        if (
            len(keys) > 6
            or any(not 4 <= key <= 231 for key in keys)
            or not 0 <= modifiers <= 255
        ):
            raise ValueError("Invalid USB keyboard report")
        async with self._input_lock:
            ws = await self.open_websocket()
            try:
                await ws.send_bytes(
                    bytes([1, modifiers, 0, *keys, *([0] * (6 - len(keys)))])
                )
                await ws.send_bytes(bytes([1] + [0] * 8))
            except ClientError as err:
                raise NanoKvmConnectionError("Input report failed") from err
            finally:
                await self.close_websocket(ws)

    async def mouse(
        self,
        x: int,
        y: int,
        *,
        relative: bool = False,
        buttons: int = 0,
        wheel: int = 0,
    ) -> None:
        """Send a complete pointer report followed by button release."""
        bound = 127 if relative else 32767
        minimum = -127 if relative else 0
        if (
            not minimum <= x <= bound
            or not minimum <= y <= bound
            or not 0 <= buttons <= 31
            or not -127 <= wheel <= 127
        ):
            raise ValueError("Invalid mouse report")
        async with self._input_lock:
            ws = await self.open_websocket()
            report = (
                bytes((buttons, x & 255, y & 255, wheel & 255))
                if relative
                else bytes((buttons, x & 255, x >> 8, y & 255, y >> 8, wheel & 255))
            )
            try:
                await ws.send_bytes(b"\x02" + report)
                if buttons:
                    release = (
                        bytes((0, 0, 0, 0))
                        if relative
                        else bytes((0, x & 255, x >> 8, y & 255, y >> 8, 0))
                    )
                    await ws.send_bytes(b"\x02" + release)
            except ClientError as err:
                raise NanoKvmConnectionError("Input report failed") from err
            finally:
                await self.close_websocket(ws)

    async def jpeg(self) -> bytes:
        """Read one JPEG after the caller has consented to encoder sharing."""
        async with self._response("GET", "stream/mjpeg") as response:
            reader = MultipartReader.from_response(response)
            part = await reader.next()
            if part is None or isinstance(part, MultipartReader):
                raise ProtocolError("Missing JPEG frame")
            frame = bytes(await part.read())
            if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
                raise ProtocolError("Invalid JPEG frame")
            return frame

    async def close_websocket(self, ws: ClientWebSocketResponse) -> None:
        """Release one transport without interrupting other device operations."""
        self._websockets.discard(ws)
        await ws.close()

    async def close(self) -> None:
        """Close owned streams without logging out other sessions or closing the session."""
        for ws in tuple(self._websockets):
            await self.close_websocket(ws)
