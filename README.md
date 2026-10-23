# NanoKVM for Home Assistant

Monitor and control Sipeed NanoKVM devices through Home Assistant. Supports Cube/Lite/Full (Alpha/Beta) and PCIe running application firmware **2.5.2 or newer**. The API contract and physical testing cover 2.5.2 on PCIe. Pro is not supported.

Requires **Home Assistant 2026.10.2 or newer**. This is a custom integration, not an official Home Assistant integration.

## Install

### HACS

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ConnorWidtfeldt/hass-nanokvm` with type **Integration**.
3. Find **NanoKVM**, download it, and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → NanoKVM**.

### Manual

Copy `custom_components/nanokvm` into your Home Assistant configuration directory as `custom_components/nanokvm`, then restart Home Assistant and add the integration.

## Connect

Enter the device's full HTTP or HTTPS URL, username and password. Custom ports are supported. For HTTPS with a self-signed certificate, enter an independently verified SHA-256 certificate fingerprint. HTTP does not encrypt credentials or input.

Automatic discovery requires mDNS and multicast connectivity, with the default `kvm-*` hostname. For renamed devices or routed networks, enter the URL manually. Discovery preserves a configured HTTPS scheme, custom port and certificate fingerprint. Use **Reconfigure** to change the URL or fingerprint. Expired credentials prompt reauthentication.

A standard device account exposes its permitted features. Administration features require a device administrator account. Custom actions require a Home Assistant administrator.

**Device** means the KVM. **Remote host** means the computer it controls. **Restart device** restarts the KVM, while ATX power and reset controls act on the remote host.

## Features

| Platform      | Capabilities                                                                                                                    |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Binary sensor | ATX power, Alpha disk activity, PCIe HDMI signal, keyboard lock LEDs, capture status, H.264 conflict, USB gadget and MCP status |
| Button        | Power, reset, forced power off, device restart, HID reset, PCIe HDMI reset                                                      |
| Switch        | Mouse jiggler, PCIe HDMI capture, application preview channel                                                                   |
| Number        | OLED sleep timeout, PCIe HDMI idle timeout                                                                                      |
| Select        | Mouse jiggler mode, USB input mode                                                                                              |
| Sensor        | Input resolution, mounted image, Tailscale, AI control mode, PicoClaw, image download status                                    |
| Update        | Device application updates                                                                                                      |
| Camera        | Optional MJPEG preview                                                                                                          |

Hardware-specific entities appear only on supported models. Advanced diagnostics and disruptive controls are disabled by default. Keyboard LED state stays unknown until reported by the remote host.

[Actions](docs/actions.md) support input injection, video settings, virtual media, scripts, Wake-on-LAN and extension controls. Use the native web console for interactive access, accounts, network settings and file management. The device page links to that console. See the [firmware capability inventory](docs/capabilities.md) for API coverage.

State polls every **5 seconds**, configuration every **60 seconds**, and application updates every **6 hours**. A shared WebSocket connection also receives capture, viewer and keyboard LED changes. ATX power uses polling. Update-server failures do not make local controls unavailable.

## Important behavior

- MJPEG preview can interrupt the shared H.264 encoder. It requires setup consent and explicit entity enablement.
- Commands are not retried automatically after timeouts. Check the device before repeating power, reset, USB, media, script or update operations.
- USB gadget changes use the firmware's toggle API. Another controller can race the operation.
- Mounting an empty image filename restores raw data-partition sharing. It is **not a safe eject**. Eject media in the remote host's operating system first.
- Application updates finish only when a changed installed version is observed. Firmware chooses the release at installation time, so the offered version cannot be pinned.
- Cube behavior and destructive operations have not been physically validated. Automated tests use mock devices and sanitized fixtures.

## Troubleshooting

If discovery is missing, check mDNS and multicast connectivity or enter the URL manually. If power is unavailable, check the ATX wiring and API connectivity. Missing HDMI does not prevent power monitoring.

For authentication failures, reauthenticate with an enabled account. For permission failures, use a permitted action or an administrator account. For TLS failures, verify the certificate or reconfigure its independently verified fingerprint. Inspect the native UI before retrying a failed application update.

Download diagnostics from the integration menu when reporting an issue. Diagnostics exclude credentials, cookies, URLs, device identifiers, typed input, script output and image paths.

## Remove

Delete the integration from **Settings → Devices & services**, then remove its download in HACS or delete `custom_components/nanokvm` and restart Home Assistant. Removal does not change device settings or the remote host.

## Development

```sh
uv venv --python 3.14
uv pip install -r requirements_test.txt
uv run --no-sync pytest --cov
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
```

Tests load the custom integration against released Home Assistant, without a Core checkout. Do not run destructive live tests on an active remote host.

The embedded async API client has no Home Assistant imports. Eventual Core adoption will require extracting it into a published library.

## License

Apache-2.0. The Sipeed brand icon comes from [Sipeed's GitHub organization](https://github.com/sipeed) and identifies the device manufacturer.
