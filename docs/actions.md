# Actions

Actions target a `config_entry_id`. Use the device selector in Developer tools > Actions to obtain the correct entry.

- `press_button`: `button` is `power` or `reset`. `duration` is 1 to 10000 milliseconds (default 800).
- `send_text`: up to 833 supported characters. `language` is empty for US, `de` or `fr`. The attached host must use the corresponding keyboard layout.
- `send_keys`: up to six USB HID usage codes (4 to 231) and a modifier bitmask (0 to 255). Sends a chord and releases it.
- `move_pointer`: absolute coordinates 0 to 32767, or relative deltas -127 to 127. Optional button bitmask 0 to 31 and wheel delta -127 to 127. Buttons are released after the report.
- `set_usb_device`: request `network` or `disk` enabled/disabled. `disk` means sharing the raw data partition. It does not describe all mounted media.
- `mount_image`: mount an existing device image by `file`, optionally as `cdrom`. An empty filename restores the raw data partition. It is **not a safe eject**. Eject media in the attached operating system before changing it.
- `configure_video`: set encoding (`type`: 0=MJPEG, 1=H.264), FPS (10 to 60), resolution (0, 480, 600, 720, 1080), quality/bitrate (50, 60, 80, 100, 1000, 2000, 3000, 5000), or GOP (1 to 100). These settings are write-only.
- `set_frame_detection`: enable/disable MJPEG frame-change detection.
- `set_input_region`: explicit firmware region object with mode `off`, `auto` or `manual`. Manual geometry requires frameWidth, frameHeight, left, top, width and height. Optional resolutions, regions, selectedResolution and selectedRegion retain their firmware meanings. Omitted companion settings are preserved by the device.
- `run_script`: run an existing `.sh` or `.py` file in the background. No script output enters Home Assistant state or logs.
- `wake_on_lan`: send a magic packet from NanoKVM to a colon-separated MAC address.
- `set_control_mode`: choose `off`, `mcp` or `picoclaw`. Switching owners can stop another extension or release its input session.
- `control_extension`: start/stop MCP or PicoClaw. Start/stop/restart/up/down Tailscale. Installation, credentials and account administration remain in the native UI.

For example, an ATX power-button press:

```yaml
action: nanokvm.press_button
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  button: power
  duration: 800
```

Use ordinary state triggers and conditions for automations, such as the power sensor becoming `off` or the HDMI signal becoming unavailable. The native web UI remains the interactive console and administration interface. Use the device's configuration link.
