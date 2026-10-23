"""Test protocol behavior without contacting physical devices."""

import asyncio
from collections.abc import AsyncIterator
import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import (
    ClientError,
    ClientSession,
    DummyCookieJar,
    Fingerprint,
    FormData,
    WSMessage,
    WSMsgType,
    WSServerHandshakeError,
)
import pytest
from pytest_homeassistant_custom_component.common import load_json_array_fixture
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.nanokvm.api import (
    AuthenticationError,
    NanoKvmClient,
    NanoKvmConnectionError,
    NanoKvmError,
    NanoKvmPermissionError,
    ProtocolError,
)
from custom_components.nanokvm.api.models import (
    CaptureStatus,
    Hdmi,
    JsonValue,
    KeyboardLeds,
    Model,
    VideoClients,
    VirtualDevices,
)

URL = "http://192.0.2.10/api/"


@pytest.fixture
def http() -> AiohttpClientMocker:
    """Return a fake device HTTP server."""
    return AiohttpClientMocker()


@pytest.fixture
async def client(http: AiohttpClientMocker) -> AsyncIterator[NanoKvmClient]:
    """Inject a cookie-free session and replace only its network transport."""
    async with ClientSession(cookie_jar=DummyCookieJar()) as session:
        with patch.object(session, "_request", side_effect=http.match_request):
            device = NanoKvmClient("http://192.0.2.10", session)
            yield device
            await device.close()
            assert not session.closed


async def test_authentication(client: NanoKvmClient, http: AiohttpClientMocker) -> None:
    """Keep cookie authentication scoped to the client."""
    http.post(
        URL + "auth/login",
        json={"code": 0},
        cookies={"nano-kvm-token": "fixture-token"},
    )
    http.get(URL + "vm/gpio", json={"code": 0, "data": {"pwr": True, "hdd": False}})
    await client.authenticate("test", "fixture")
    assert await client.get_status({"power"}) == {"power": True, "hdd": False}
    assert http.mock_calls[1][3]["Cookie"] == "nano-kvm-token=fixture-token"
    assert http.mock_calls[0][2]["password"] != "fixture"


async def test_shared_session(http: AiohttpClientMocker) -> None:
    """Clients sharing a session must not share credentials or close it."""
    other_url = "http://192.0.2.20/api/"
    http.post(URL + "auth/login", json={"code": 0}, cookies={"nano-kvm-token": "one"})
    http.post(
        other_url + "auth/login", json={"code": 0}, cookies={"nano-kvm-token": "two"}
    )
    http.get(URL + "vm/gpio", json={"code": 0, "data": {"pwr": True, "hdd": False}})
    http.get(
        other_url + "vm/gpio", json={"code": 0, "data": {"pwr": False, "hdd": False}}
    )
    async with ClientSession(cookie_jar=DummyCookieJar()) as session:
        with patch.object(session, "_request", side_effect=http.match_request):
            first = NanoKvmClient("http://192.0.2.10", session)
            second = NanoKvmClient("http://192.0.2.20", session)
            await first.authenticate("test", "fixture")
            await second.authenticate("test", "fixture")
            await first.get_status({"power"})
            await first.close()
            assert not session.closed
            await second.get_status({"power"})
            await second.close()
    assert http.mock_calls[2][3]["Cookie"] == "nano-kvm-token=one"
    assert http.mock_calls[3][3]["Cookie"] == "nano-kvm-token=two"


async def test_certificate_pin() -> None:
    """A supplied HTTPS fingerprint reaches aiohttp without disabling verification."""
    async with ClientSession(cookie_jar=DummyCookieJar()) as session:
        client = NanoKvmClient("https://192.0.2.10", session, fingerprint="ab" * 32)
        with (
            patch.object(session, "request", side_effect=ClientError) as request,
            pytest.raises(NanoKvmConnectionError),
        ):
            await client.get_status({"power"})
        pin = request.call_args.kwargs["ssl"]
        assert isinstance(pin, Fingerprint)
        assert pin.fingerprint == bytes.fromhex("ab" * 32)
        with pytest.raises(ValueError, match="requires HTTPS"):
            NanoKvmClient("http://192.0.2.10", session, fingerprint="ab" * 32)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthenticationError), (403, NanoKvmPermissionError), (502, ProtocolError)],
)
async def test_http_errors(
    client: NanoKvmClient,
    http: AiohttpClientMocker,
    status: int,
    error: type[NanoKvmError],
) -> None:
    """Differentiate revoked sessions, roles, and broken gateways."""
    http.get(URL + "vm/gpio", status=status)
    with pytest.raises(error):
        await client.get_status({"power"})


@pytest.mark.parametrize(
    "body",
    [[], {}, {"code": False}, {"code": 0, "data": {"pwr": "true", "hdd": False}}],
)
async def test_malformed(
    client: NanoKvmClient, http: AiohttpClientMocker, body: JsonValue
) -> None:
    """Malformed values must never be coerced into confirmed power state."""
    http.get(URL + "vm/gpio", json=body)
    with pytest.raises(ProtocolError):
        await client.get_status({"power"})


async def test_no_replay(client: NanoKvmClient, http: AiohttpClientMocker) -> None:
    """An ambiguous ATX timeout must not repeat the pulse."""
    http.post(URL + "vm/gpio", exc=TimeoutError())
    with pytest.raises(NanoKvmConnectionError):
        await client.press_button("power", 800)
    assert http.call_count == 1


@pytest.mark.parametrize(
    ("current", "requested", "calls"),
    [(False, True, 1), (True, True, 0), (True, False, 1)],
)
async def test_usb_set(
    client: NanoKvmClient, current: bool, requested: bool, calls: int
) -> None:
    """Read, compare, toggle only if needed, and confirm actual state."""
    with (
        patch.object(
            client,
            "read",
            side_effect=[
                VirtualDevices(network=current, disk=False),
                VirtualDevices(network=requested, disk=False),
            ],
        ),
        patch.object(client, "request") as request,
    ):
        await client.set_virtual_device("network", requested)
    assert request.await_count == calls


async def test_usb_race(client: NanoKvmClient) -> None:
    """Report an externally raced toggle without toggling again."""
    with (
        patch.object(
            client, "read", return_value=VirtualDevices(network=False, disk=False)
        ),
        patch.object(client, "request") as request,
        pytest.raises(NanoKvmError, match="concurrently"),
    ):
        await client.set_virtual_device("network", True)
    request.assert_awaited_once_with("POST", "vm/device/virtual", {"device": "network"})


async def test_jiggler_preserves_mode(
    client: NanoKvmClient, http: AiohttpClientMocker
) -> None:
    """Changing enable must retain the configured movement mode."""
    http.get(
        URL + "vm/mouse-jiggler",
        json={"code": 0, "data": {"enabled": False, "mode": "absolute"}},
    )
    http.post(URL + "vm/mouse-jiggler/", json={"code": 0})
    await client.set_jiggler(enabled=True)
    assert http.mock_calls[-1][2] == {"enabled": True, "mode": "absolute"}


@pytest.mark.parametrize(
    ("setting", "value"),
    [("fps", 1), ("quality", 42), ("gop", 101), ("resolution", 900), ("type", 2)],
)
async def test_video_validation(
    client: NanoKvmClient, http: AiohttpClientMocker, setting: str, value: int
) -> None:
    """Reject values the firmware silently substitutes with defaults."""
    with pytest.raises(ValueError):
        await client.configure_video(setting, value)
    assert http.call_count == 0


async def test_events(client: NanoKvmClient) -> None:
    """Ignore unknown messages, preserve unknown LEDs, and release streams."""
    ws = MagicMock()
    ws.receive = AsyncMock(
        side_effect=[
            WSMessage(WSMsgType.TEXT, '{"type":"future","data":{}}', ""),
            WSMessage(
                WSMsgType.TEXT,
                '{"type":"hid-led-status","data":"{\\"known\\":false,\\"numLock\\":false,\\"capsLock\\":false,\\"scrollLock\\":false}"}',
                "",
            ),
        ]
    )
    ws.close = AsyncMock()
    with patch.object(client, "open_websocket", return_value=ws):
        stream = client.events()
        event = await anext(stream)
        assert isinstance(event, KeyboardLeds)
        assert client.led_values(event) == {
            "num_lock": None,
            "caps_lock": None,
            "scroll_lock": None,
        }
        await stream.aclose()
    ws.close.assert_awaited_once()


async def test_keyboard_report(client: NanoKvmClient) -> None:
    """Send one key-down report followed by release, then close."""
    ws = MagicMock(send_bytes=AsyncMock(), close=AsyncMock())
    with patch.object(client, "open_websocket", return_value=ws):
        await client.send_keys([4], modifiers=2)
    assert ws.send_bytes.call_args_list[0].args == (bytes([1, 2, 0, 4, 0, 0, 0, 0, 0]),)
    assert ws.send_bytes.call_args_list[1].args == (bytes([1] + [0] * 8),)
    ws.close.assert_awaited_once()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://192.0.2.10",
        "http:///",
        "http://user:secret@192.0.2.10",
        "http://192.0.2.10/path",
        "http://192.0.2.10?secret=yes",
        "http://192.0.2.10/#fragment",
    ],
)
async def test_invalid_endpoint(url: str) -> None:
    """Reject endpoints that could alter credential or route scope."""
    async with ClientSession(cookie_jar=DummyCookieJar()) as session:
        with pytest.raises(ValueError):
            NanoKvmClient(url, session)


async def test_require_cookie_isolation() -> None:
    """A normal jar can override explicit cookies on WebSocket handshakes."""
    async with ClientSession() as session:
        with pytest.raises(TypeError, match="DummyCookieJar"):
            NanoKvmClient("https://kvm.example", session)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("not JSON", ProtocolError),
        ('{"code":1,"message":"sensitive"}', AuthenticationError),
        ('{"code":0}', AuthenticationError),
    ],
)
async def test_login_failure(
    client: NanoKvmClient,
    http: AiohttpClientMocker,
    body: str,
    error: type[NanoKvmError],
) -> None:
    """Malformed or rejected logins must not establish a session."""
    http.post(URL + "auth/login", text=body)
    with pytest.raises(error) as raised:
        await client.authenticate("test", "fixture")
    assert "sensitive" not in str(raised.value)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("not JSON", ProtocolError),
        ('{"code":-1,"message":"sensitive"}', NanoKvmError),
        ('{"code":"runtime_unavailable","message":"sensitive"}', NanoKvmError),
    ],
)
async def test_api_rejection(
    client: NanoKvmClient,
    http: AiohttpClientMocker,
    body: str,
    error: type[NanoKvmError],
) -> None:
    """Both API error-code formats retain sanitized exceptions."""
    http.get(URL + "vm/info", text=body)
    with pytest.raises(error) as raised:
        await client.get_info()
    assert "sensitive" not in str(raised.value)


async def test_status_groups(client: NanoKvmClient, http: AiohttpClientMocker) -> None:
    """Read hardware-dependent groups and retain unknown host LED state."""
    http.get(URL + "vm/hardware", json={"code": 0, "data": {"version": "PCIE"}})
    http.get(
        URL + "auth/account",
        json={"code": 0, "data": {"username": "test", "role": "user"}},
    )
    http.get(
        URL + "vm/hdmi",
        json={"code": 0, "data": {"enabled": True, "signal": False, "idleTimeout": 2}},
    )
    http.get(
        URL + "hid/leds",
        json={
            "code": 0,
            "data": {
                "known": True,
                "numLock": True,
                "capsLock": False,
                "scrollLock": False,
            },
        },
    )
    http.get(
        URL + "vm/input-resolution",
        json={"code": 0, "data": {"width": 1920, "height": 1080}},
    )
    assert await client.get_hardware() == "PCIE"
    assert (await client.get_account()).role == "user"
    assert await client.get_status({"hdmi", "num_lock", "input_resolution"}) == {
        "hdmi": True,
        "hdmi_signal": False,
        "hdmi_idle_timeout": 2,
        "num_lock": True,
        "caps_lock": False,
        "scroll_lock": False,
        "input_resolution": "1920x1080",
    }


@pytest.mark.parametrize(("status", "body"), [(200, {"code": -1}), (403, {})])
async def test_optional_resolution(
    client: NanoKvmClient, http: AiohttpClientMocker, status: int, body: dict[str, int]
) -> None:
    """An absent video signal cannot break power monitoring."""
    http.get(URL + "vm/input-resolution", status=status, json=body)
    http.get(URL + "vm/gpio", json={"code": 0, "data": {"pwr": True, "hdd": False}})
    assert await client.get_status({"power", "input_resolution"}) == {
        "power": True,
        "hdd": False,
        "input_resolution": None,
    }


async def test_configuration_groups(
    client: NanoKvmClient, http: AiohttpClientMocker
) -> None:
    """Decode optional configuration without retaining other private fields."""
    http.get(URL + "vm/oled", json={"code": 0, "data": {"exist": False, "sleep": 0}})
    http.get(
        URL + "vm/device/virtual",
        json={"code": 0, "data": {"network": True, "disk": False, "media": False}},
    )
    http.get(URL + "application/preview", json={"code": 0, "data": {"enabled": False}})
    http.get(
        URL + "vm/mouse-jiggler",
        json={"code": 0, "data": {"enabled": True, "mode": "relative"}},
    )
    http.get(URL + "picoclaw/runtime/status", json={"code": "runtime_unavailable"})
    assert await client.get_configuration(
        {"oled_sleep", "usb_disk", "preview", "jiggler", "picoclaw"}
    ) == {
        "oled_sleep": None,
        "usb_network": True,
        "usb_disk": False,
        "preview": False,
        "jiggler": True,
        "jiggler_mode": "relative",
        "picoclaw": None,
    }


@pytest.mark.parametrize("data", [None, {"enabled": 1}])
async def test_invalid_configuration(
    client: NanoKvmClient, http: AiohttpClientMocker, data: dict[str, int] | None
) -> None:
    """Configuration values must retain their wire types."""
    http.get(URL + "application/preview", json={"code": 0, "data": data})
    with pytest.raises(ProtocolError):
        await client.get_configuration({"preview"})


@pytest.mark.parametrize(("keys", "modifiers"), [([0], 0), ([4] * 7, 0), ([4], 256)])
async def test_invalid_keyboard(
    client: NanoKvmClient, keys: list[int], modifiers: int
) -> None:
    """Reject invalid reports before opening an input session."""
    with pytest.raises(ValueError):
        await client.send_keys(keys, modifiers)


@pytest.mark.parametrize(
    ("relative", "buttons", "expected"),
    [
        (True, 1, bytes([2, 1, 12, 34, 255])),
        (False, 1, bytes([2, 1, 12, 0, 34, 0, 255])),
        (False, 0, bytes([2, 0, 12, 0, 34, 0, 255])),
    ],
)
async def test_mouse_reports(
    client: NanoKvmClient, relative: bool, buttons: int, expected: bytes
) -> None:
    """Encode signed movement/wheel and release pressed buttons."""
    ws = MagicMock(send_bytes=AsyncMock(), close=AsyncMock())
    with patch.object(client, "open_websocket", return_value=ws):
        await client.mouse(12, 34, relative=relative, buttons=buttons, wheel=-1)
    assert ws.send_bytes.call_args_list[0].args == (expected,)
    ws.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("text", "language"), [("", ""), ("a" * 834, ""), ("🙂", ""), ("text", "unknown")]
)
async def test_invalid_text(client: NanoKvmClient, text: str, language: str) -> None:
    """Do not silently drop unsupported characters or truncate text."""
    with pytest.raises(ValueError):
        await client.send_text(text, language)


@pytest.mark.parametrize(
    ("text", "language"), [("a" * 833, ""), ("ä€", "de"), ("éç", "fr")]
)
async def test_paste_layout(
    client: NanoKvmClient, http: AiohttpClientMocker, text: str, language: str
) -> None:
    """Send the firmware's intentionally spelled langue parameter."""
    http.post(URL + "hid/paste", json={"code": 0})
    await client.send_text(text, language)
    assert http.mock_calls[0][2] == {"content": text, "langue": language}


@pytest.mark.parametrize(
    "name", ["../test.sh", "test.sh;reboot", "test", "test.py && reboot"]
)
async def test_script_name(client: NanoKvmClient, name: str) -> None:
    """Accept literal script filenames because the firmware invokes a shell."""
    with pytest.raises(ValueError):
        await client.run_script(name)


@pytest.mark.parametrize(
    "region",
    [
        {"mode": "unknown"},
        {"mode": "manual", "width": 10},
        {
            "mode": "manual",
            "frameWidth": 10,
            "frameHeight": 10,
            "left": 9,
            "top": 0,
            "width": 2,
            "height": 2,
        },
        {"mode": "auto", "selectedRegion": "2x2", "selectedResolution": "2x2"},
        {"mode": "auto", "resolutions": [{"width": 0, "height": 1}]},
        {
            "mode": "auto",
            "resolutions": [{"width": 10, "height": 10}],
            "selectedResolution": "20x20",
        },
    ],
)
async def test_invalid_region(client: NanoKvmClient, region: dict[str, object]) -> None:
    """Validate geometry and selections before changing the input region."""
    with pytest.raises(ValueError):
        await client.set_input_region(region)


async def test_partial_region(client: NanoKvmClient, http: AiohttpClientMocker) -> None:
    """Omit unmodified companion settings so the firmware preserves them."""
    http.post(URL + "vm/input-region", json={"code": 0})
    await client.set_input_region({"mode": "off"})
    assert http.mock_calls[0][2] == {"mode": "off"}


@pytest.mark.parametrize(
    "case",
    load_json_array_fixture("rest_contract.json"),
    ids=lambda case: f"{case['name']}-{case['path']}",
)
async def test_rest_contract(
    client: NanoKvmClient, http: AiohttpClientMocker, case: dict[str, JsonValue]
) -> None:
    """Check named operations against the stable route/payload fixture."""
    http.request(case["method"], URL + case["path"], json={"code": 0, "data": {}})
    await getattr(client, case["name"])(*case["args"], **case["kwargs"])
    assert http.call_count == 1
    assert http.mock_calls[0][2] == case["body"]


async def test_password_operations(
    client: NanoKvmClient, http: AiohttpClientMocker
) -> None:
    """Account mutations use the device encoding, never a plaintext password."""
    http.post(URL + "auth/password", json={"code": 0})
    http.post(URL + "auth/users", json={"code": 0})
    http.post(URL + "auth/users/test/password", json={"code": 0})
    await client.change_password("old-fixture", "new-fixture")
    await client.create_user("test", "new-fixture", "user")
    await client.change_user_password("test", "new-fixture")
    assert http.call_count == 3
    assert "new-fixture" not in repr(http.mock_calls)
    assert "old-fixture" not in repr(http.mock_calls)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_autostart", ["../test.sh"]),
        ("update_user", ["test"]),
        ("set_memory_limit", [True, 1]),
        ("set_swap", [-1]),
        ("pause_frame_detection", [-1]),
        ("upload_script", ["test.exe", b"test"]),
        ("upload_image", ["../test.iso", b"test"]),
        ("update_offline", ["test.zip", b"test"]),
        ("wake_on_lan", ["invalid"]),
        ("control_extension", ["unknown", "start"]),
        ("press_button", ["unknown"]),
        ("set_virtual_device", ["unknown", True]),
        ("set_setting", ["hdmi", 1]),
        ("set_setting", ["oled_sleep", -1]),
        ("set_setting", ["hdmi_idle_timeout", 10081]),
        ("set_setting", ["hid_mode", "unknown"]),
        ("set_setting", ["control_mode", "unknown"]),
        ("set_setting", ["unknown", False]),
        ("mouse", [32768, 0]),
    ],
)
async def test_command_validation(
    client: NanoKvmClient, http: AiohttpClientMocker, method: str, args: list[object]
) -> None:
    """Reject unsafe names, unsupported values and out-of-range input before IO."""
    with pytest.raises(ValueError):
        await getattr(client, method)(*args)
    assert http.call_count == 0


@pytest.mark.parametrize(
    ("method", "name", "path"),
    [
        ("upload_script", "test.sh", "vm/script/upload"),
        ("upload_image", "test.iso", "download/file"),
        ("update_offline", "nanokvm_2.5.2.tar.gz", "application/update/offline"),
    ],
)
async def test_upload(
    client: NanoKvmClient, http: AiohttpClientMocker, method: str, name: str, path: str
) -> None:
    """Uploads use multipart transport and execute exactly once."""
    http.post(URL + path, json={"code": 0})
    await getattr(client, method)(name, b"fixture")
    assert http.call_count == 1
    assert isinstance(http.mock_calls[0][2], FormData)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, NanoKvmPermissionError),
        (500, NanoKvmConnectionError),
    ],
)
async def test_websocket_auth(
    client: NanoKvmClient, status: int, error: type[NanoKvmError]
) -> None:
    """Handshake failures preserve authentication/permission semantics."""
    with (
        patch.object(
            ClientSession,
            "ws_connect",
            side_effect=WSServerHandshakeError(MagicMock(), (), status=status),
        ),
        pytest.raises(error),
    ):
        await client.open_websocket()


async def test_websocket_network_failure(client: NanoKvmClient) -> None:
    """Connection failures remain sanitized and recoverable."""
    with (
        patch.object(ClientSession, "ws_connect", side_effect=ClientError("private")),
        pytest.raises(NanoKvmConnectionError),
    ):
        await client.open_websocket()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("open_terminal", "vm/terminal"),
        ("open_webrtc", "stream/h264"),
        ("open_direct_h264", "stream/h264/direct"),
        ("open_picoclaw_gateway", "picoclaw/gateway/ws"),
    ],
)
async def test_protocol_transports(
    client: NanoKvmClient, method: str, path: str
) -> None:
    """Own upgraded connections while retaining the injected HTTP session."""
    ws = MagicMock(close=AsyncMock())
    with patch.object(
        ClientSession, "ws_connect", new_callable=AsyncMock, return_value=ws
    ) as connect:
        assert await getattr(client, method)() is ws
    assert str(connect.call_args.args[0]) == URL + path
    await client.close()
    ws.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("message", "error"),
    [
        (WSMessage(WSMsgType.CLOSED, None, ""), NanoKvmConnectionError),
        (WSMessage(WSMsgType.TEXT, "invalid", ""), ProtocolError),
        (
            WSMessage(WSMsgType.TEXT, '{"type":"capture-status","data":{}}', ""),
            ProtocolError,
        ),
        (
            WSMessage(WSMsgType.TEXT, '{"type":"capture-status","data":"{}"}', ""),
            ProtocolError,
        ),
    ],
)
async def test_invalid_event(
    client: NanoKvmClient, message: WSMessage, error: type[NanoKvmError]
) -> None:
    """Discard unusable event streams and release their transport."""
    ws = MagicMock(receive=AsyncMock(return_value=message), close=AsyncMock())
    with patch.object(client, "open_websocket", return_value=ws), pytest.raises(error):
        await anext(client.events())
    ws.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("kind", "payload", "model"),
    [
        (
            "capture-status",
            {
                "ok": True,
                "result": 0,
                "message": "private",
                "mode": "mjpeg",
                "severity": "info",
                "updatedAt": "fixture",
            },
            CaptureStatus,
        ),
        (
            "h264-mode-status",
            {
                "generation": "fixture",
                "revision": 0,
                "direct": 1,
                "webrtc": 0,
                "mixed": False,
            },
            VideoClients,
        ),
    ],
)
async def test_typed_events(
    client: NanoKvmClient, kind: str, payload: dict[str, JsonValue], model: type[Model]
) -> None:
    """Decode firmware envelopes, including initial snapshots, as state reports."""
    ws = MagicMock(
        receive=AsyncMock(
            side_effect=[
                WSMessage(
                    WSMsgType.TEXT,
                    json.dumps({"type": kind, "data": json.dumps(payload)}),
                    "",
                ),
                WSMessage(WSMsgType.CLOSED, None, ""),
            ]
        ),
        send_bytes=AsyncMock(),
        close=AsyncMock(),
    )
    with patch.object(client, "open_websocket", return_value=ws):
        stream = client.events()
        assert isinstance(await anext(stream), model)
        with pytest.raises(NanoKvmConnectionError):
            await anext(stream)
    ws.send_bytes.assert_awaited_once_with(b"\x00")
    ws.close.assert_awaited_once()


async def test_event_heartbeat(client: NanoKvmClient) -> None:
    """A quiet stream gets firmware heartbeats and connection errors reconnect."""
    ws = MagicMock(
        receive=AsyncMock(
            side_effect=[
                TimeoutError(),
                WSMessage(WSMsgType.BINARY, b"\x00", ""),
                ClientError(),
            ]
        ),
        send_bytes=AsyncMock(),
        close=AsyncMock(),
    )
    with (
        patch.object(client, "open_websocket", return_value=ws),
        pytest.raises(NanoKvmConnectionError),
    ):
        await anext(client.events())
    ws.send_bytes.assert_awaited_once_with(b"\x00")
    ws.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        pytest.param("send_text", ("second",), id="paste"),
        pytest.param("send_keys", ([4],), id="keys"),
        pytest.param("mouse", (10, 20), id="mouse"),
    ],
)
async def test_paste_serializes_input(
    client: NanoKvmClient, method: str, args: tuple[object, ...]
) -> None:
    """No other input operation starts while a paste is running."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def paste(*args: object, **kwargs: object) -> None:
        started.set()
        await release.wait()

    ws = MagicMock(send_bytes=AsyncMock(), close=AsyncMock())
    with (
        patch.object(client, "request", side_effect=paste) as request,
        patch.object(client, "open_websocket", return_value=ws) as connect,
    ):
        first = asyncio.create_task(client.send_text("first"))
        await started.wait()
        second = asyncio.create_task(getattr(client, method)(*args))
        await asyncio.sleep(0)
        request.assert_awaited_once()
        connect.assert_not_awaited()
        release.set()
        await asyncio.gather(first, second)


@pytest.mark.parametrize(
    ("method", "field", "path"),
    [
        pytest.param("get_configuration", "hid_mode", "hid/mode", id="configuration"),
        pytest.param(
            "get_status", "input_resolution", "vm/input-resolution", id="state"
        ),
    ],
)
async def test_optional_transport_failure(
    client: NanoKvmClient,
    http: AiohttpClientMocker,
    method: str,
    field: str,
    path: str,
) -> None:
    """A disconnected optional endpoint must fail the coordinator refresh."""
    http.get(URL + path, exc=TimeoutError())
    with pytest.raises(NanoKvmConnectionError):
        await getattr(client, method)({field})


async def test_close_one_transport(client: NanoKvmClient) -> None:
    """Releasing an event socket preserves a simultaneous input connection."""
    events = MagicMock(close=AsyncMock())
    inputs = MagicMock(close=AsyncMock())
    with patch.object(
        ClientSession,
        "ws_connect",
        new_callable=AsyncMock,
        side_effect=[events, inputs],
    ):
        await client.open_websocket()
        await client.open_websocket()
    await client.close_websocket(events)
    events.close.assert_awaited_once()
    inputs.close.assert_not_awaited()
    await client.close()
    events.close.assert_awaited_once()
    inputs.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("method", "args", "timeout"),
    [
        pytest.param(
            "control_extension", ("tailscale", "install"), 600, id="tailscale-install"
        ),
        pytest.param(
            "control_extension", ("tailscale", "start"), 10, id="tailscale-start"
        ),
        pytest.param(
            "control_extension",
            ("picoclaw", "install"),
            10,
            id="picoclaw-install-async",
        ),
        pytest.param(
            "control_extension", ("picoclaw", "start"), 60, id="picoclaw-start"
        ),
        pytest.param("control_extension", ("picoclaw", "stop"), 60, id="picoclaw-stop"),
        pytest.param(
            "control_extension", ("picoclaw", "uninstall"), 60, id="picoclaw-uninstall"
        ),
        pytest.param("control_extension", ("mcp", "start"), 60, id="mcp-start"),
        pytest.param("control_extension", ("mcp", "stop"), 60, id="mcp-stop"),
        pytest.param("set_setting", ("control_mode", "mcp"), 60, id="control-mode"),
    ],
)
async def test_extension_timeouts(
    client: NanoKvmClient, method: str, args: tuple[str, str], timeout: int
) -> None:
    """Synchronous firmware startup and download windows exceed normal read deadlines."""
    with patch.object(client, "request") as request:
        await getattr(client, method)(*args)
    assert request.call_args.kwargs["timeout"] == timeout


@pytest.mark.parametrize("frame", [b"", b"not a jpeg"])
async def test_invalid_jpeg(
    client: NanoKvmClient, http: AiohttpClientMocker, frame: bytes
) -> None:
    """Reject malformed frames without changing capture settings."""
    http.get(URL + "stream/mjpeg")
    part = MagicMock(read=AsyncMock(return_value=frame))
    reader = MagicMock(next=AsyncMock(return_value=part))
    with (
        patch(
            "custom_components.nanokvm.api.client.MultipartReader.from_response",
            return_value=reader,
        ),
        pytest.raises(ProtocolError),
    ):
        await client.jpeg()
    assert http.call_count == 1


async def test_missing_jpeg(client: NanoKvmClient, http: AiohttpClientMocker) -> None:
    """Handle a stream closing before its first frame."""
    http.get(URL + "stream/mjpeg")
    reader = MagicMock(next=AsyncMock(return_value=None))
    with (
        patch(
            "custom_components.nanokvm.api.client.MultipartReader.from_response",
            return_value=reader,
        ),
        pytest.raises(ProtocolError),
    ):
        await client.jpeg()


async def test_jpeg(client: NanoKvmClient, http: AiohttpClientMocker) -> None:
    """Return the frame bytes, not decoder or browser state."""
    http.get(URL + "stream/mjpeg")
    part = MagicMock(read=AsyncMock(return_value=b"\xff\xd8fixture\xff\xd9"))
    reader = MagicMock(next=AsyncMock(return_value=part))
    with patch(
        "custom_components.nanokvm.api.client.MultipartReader.from_response",
        return_value=reader,
    ):
        assert await client.jpeg() == b"\xff\xd8fixture\xff\xd9"


async def test_revoked_optional_reads(
    client: NanoKvmClient, http: AiohttpClientMocker
) -> None:
    """Optional endpoints do not swallow an expired session."""
    http.get(URL + "application/preview", status=401)
    http.get(URL + "vm/input-resolution", status=401)
    with pytest.raises(AuthenticationError):
        await client.get_configuration({"preview"})
    with pytest.raises(AuthenticationError):
        await client.get_status({"input_resolution"})


async def test_access_point_headers(
    client: NanoKvmClient, http: AiohttpClientMocker
) -> None:
    """AP provisioning uses its own explicit authentication header."""
    http.post(URL + "network/wifi/verify", json={"code": 0})
    http.post(URL + "network/wifi", json={"code": 0})
    await client.verify_access_point("fixture-key")
    await client.provision_wifi("fixture-network", "fixture-password", "fixture-key")
    assert http.mock_calls[0][3]["X-AP-Key"] == "fixture-key"
    assert http.mock_calls[1][2] == {
        "ssid": "fixture-network",
        "password": "fixture-password",
    }


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        pytest.param(
            {"mode": "off", "regions": None, "selectedResolution": None},
            {"mode": "off", "regions": None, "selectedResolution": None},
            id="explicit-null",
        ),
        pytest.param(
            {
                "mode": "manual",
                "frame_width": 10,
                "frame_height": 10,
                "left": 0,
                "top": 0,
                "width": 5,
                "height": 5,
            },
            {
                "mode": "manual",
                "frameWidth": 10,
                "frameHeight": 10,
                "left": 0,
                "top": 0,
                "width": 5,
                "height": 5,
            },
            id="canonical-field-names",
        ),
        pytest.param(
            {
                "mode": "manual",
                "regions": [
                    {
                        "frameWidth": 10,
                        "frameHeight": 10,
                        "left": 0,
                        "top": 0,
                        "width": 5,
                        "height": 5,
                    }
                ],
                "selectedRegion": "5x5",
            },
            {
                "mode": "manual",
                "regions": [
                    {
                        "frameWidth": 10,
                        "frameHeight": 10,
                        "left": 0,
                        "top": 0,
                        "width": 5,
                        "height": 5,
                    }
                ],
                "selectedRegion": "5x5",
            },
            id="nested-region",
        ),
    ],
)
async def test_region_serialization(
    client: NanoKvmClient,
    http: AiohttpClientMocker,
    region: dict[str, JsonValue],
    expected: dict[str, JsonValue],
) -> None:
    """Preserve partial update semantics across validation and serialization."""
    http.post(URL + "vm/input-region", json={"code": 0})
    await client.set_input_region(region)
    assert http.mock_calls[0][2] == expected


@pytest.mark.parametrize("value", [True, "1", 1.5, -1])
async def test_strict_integer_response(
    client: NanoKvmClient,
    http: AiohttpClientMocker,
    value: JsonValue,
) -> None:
    """Reject coercible values and invalid bounds at the device boundary."""
    http.get(
        URL + "vm/hdmi",
        json={
            "code": 0,
            "data": {
                "enabled": True,
                "signal": True,
                "idleTimeout": value,
            },
        },
    )
    with pytest.raises(ProtocolError):
        await client.read("vm/hdmi", Hdmi)
