"""Explicit NanoKVM automation actions."""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import partial
from typing import Any, cast

import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .coordinator import NanoKvmConfigEntry


class NanoKvmService(StrEnum):
    """Supported actions."""

    PRESS_BUTTON = "press_button"
    SEND_TEXT = "send_text"
    SEND_KEYS = "send_keys"
    MOVE_POINTER = "move_pointer"
    SET_USB_DEVICE = "set_usb_device"
    MOUNT_IMAGE = "mount_image"
    CONFIGURE_VIDEO = "configure_video"
    SET_CONTROL_MODE = "set_control_mode"
    SET_INPUT_REGION = "set_input_region"
    SET_FRAME_DETECTION = "set_frame_detection"
    RUN_SCRIPT = "run_script"
    WAKE_ON_LAN = "wake_on_lan"
    CONTROL_EXTENSION = "control_extension"


class NanoKvmServiceArgument(StrEnum):
    """Action fields."""

    MODE = "mode"
    REGION = "region"
    LANGUAGE = "language"
    BUTTON = "button"
    DURATION = "duration"
    TEXT = "text"
    KEYS = "keys"
    MODIFIERS = "modifiers"
    X = "x"
    Y = "y"
    RELATIVE = "relative"
    BUTTONS = "buttons"
    WHEEL = "wheel"
    DEVICE = "device"
    ENABLED = "enabled"
    FILE = "file"
    CDROM = "cdrom"
    SETTING = "setting"
    VALUE = "value"
    NAME = "name"
    MAC = "mac"
    EXTENSION = "extension"
    ACTION = "action"


ARG = NanoKvmServiceArgument
SCHEMAS: dict[NanoKvmService, dict[object, Any]] = {
    NanoKvmService.PRESS_BUTTON: {
        probatio.Required(ARG.BUTTON): probatio.In(("power", "reset")),
        probatio.Optional(ARG.DURATION, default=800): probatio.All(
            int, probatio.Range(min=1, max=10000)
        ),
    },
    NanoKvmService.SET_CONTROL_MODE: {
        probatio.Required(ARG.MODE): probatio.In(("off", "mcp", "picoclaw"))
    },
    NanoKvmService.SET_INPUT_REGION: {probatio.Required(ARG.REGION): dict},
    NanoKvmService.SEND_TEXT: {
        probatio.Optional(ARG.LANGUAGE, default=""): probatio.In(("", "de", "fr")),
        probatio.Required(ARG.TEXT): probatio.All(str, probatio.Length(min=1, max=833)),
    },
    NanoKvmService.SEND_KEYS: {
        probatio.Required(ARG.KEYS): probatio.All(
            [probatio.All(int, probatio.Range(min=4, max=231))], probatio.Length(max=6)
        ),
        probatio.Optional(ARG.MODIFIERS, default=0): probatio.All(
            int, probatio.Range(min=0, max=255)
        ),
    },
    NanoKvmService.MOVE_POINTER: {
        probatio.Required(ARG.X): int,
        probatio.Required(ARG.Y): int,
        probatio.Optional(ARG.RELATIVE, default=False): cv.boolean,
        probatio.Optional(ARG.BUTTONS, default=0): probatio.All(
            int, probatio.Range(min=0, max=31)
        ),
        probatio.Optional(ARG.WHEEL, default=0): probatio.All(
            int, probatio.Range(min=-127, max=127)
        ),
    },
    NanoKvmService.SET_USB_DEVICE: {
        probatio.Required(ARG.DEVICE): probatio.In(("network", "disk")),
        probatio.Required(ARG.ENABLED): cv.boolean,
    },
    NanoKvmService.MOUNT_IMAGE: {
        probatio.Optional(ARG.FILE, default=""): str,
        probatio.Optional(ARG.CDROM, default=False): cv.boolean,
    },
    NanoKvmService.CONFIGURE_VIDEO: {
        probatio.Required(ARG.SETTING): probatio.In(
            ("type", "fps", "quality", "resolution", "gop")
        ),
        probatio.Required(ARG.VALUE): int,
    },
    NanoKvmService.SET_FRAME_DETECTION: {
        probatio.Required(ARG.ENABLED): cv.boolean,
    },
    NanoKvmService.RUN_SCRIPT: {probatio.Required(ARG.NAME): str},
    NanoKvmService.WAKE_ON_LAN: {probatio.Required(ARG.MAC): cv.string},
    NanoKvmService.CONTROL_EXTENSION: {
        probatio.Required(ARG.EXTENSION): probatio.In(("tailscale", "picoclaw", "mcp")),
        probatio.Required(ARG.ACTION): probatio.In(
            ("start", "stop", "restart", "up", "down")
        ),
    },
}


async def _async_handle(call: ServiceCall) -> None:
    """Resolve a loaded device and send one validated command."""
    entry = cast(
        NanoKvmConfigEntry | None,
        call.hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID]),
    )
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    client = entry.runtime_data.client
    data = call.data
    command: Callable[[], Awaitable[object]]
    match call.service:
        case NanoKvmService.SET_CONTROL_MODE:
            command = partial(client.set_setting, "control_mode", data[ARG.MODE])
        case NanoKvmService.SET_INPUT_REGION:
            command = partial(client.set_input_region, data[ARG.REGION])
        case NanoKvmService.PRESS_BUTTON:
            command = partial(client.press_button, data[ARG.BUTTON], data[ARG.DURATION])
        case NanoKvmService.SEND_TEXT:
            command = partial(client.send_text, data[ARG.TEXT], data[ARG.LANGUAGE])
        case NanoKvmService.SEND_KEYS:
            command = partial(client.send_keys, data[ARG.KEYS], data[ARG.MODIFIERS])
        case NanoKvmService.MOVE_POINTER:
            command = partial(
                client.mouse,
                data[ARG.X],
                data[ARG.Y],
                relative=data[ARG.RELATIVE],
                buttons=data[ARG.BUTTONS],
                wheel=data[ARG.WHEEL],
            )
        case NanoKvmService.SET_USB_DEVICE:
            command = partial(
                client.set_virtual_device, data[ARG.DEVICE], data[ARG.ENABLED]
            )
        case NanoKvmService.MOUNT_IMAGE:
            command = partial(client.mount_image, data[ARG.FILE], cdrom=data[ARG.CDROM])
        case NanoKvmService.CONFIGURE_VIDEO:
            command = partial(
                client.configure_video, data[ARG.SETTING], data[ARG.VALUE]
            )
        case NanoKvmService.SET_FRAME_DETECTION:
            command = partial(client.set_frame_detection, data[ARG.ENABLED])
        case NanoKvmService.RUN_SCRIPT:
            command = partial(client.run_script, data[ARG.NAME])
        case NanoKvmService.WAKE_ON_LAN:
            command = partial(client.wake_on_lan, data[ARG.MAC])
        case NanoKvmService.CONTROL_EXTENSION:
            command = partial(
                client.control_extension, data[ARG.EXTENSION], data[ARG.ACTION]
            )

    try:
        await entry.runtime_data.async_command(command)
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_parameter"
        ) from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Require a Home Assistant administrator for remote control actions."""
    for service, fields in SCHEMAS.items():
        async_register_admin_service(
            hass,
            DOMAIN,
            service,
            _async_handle,
            probatio.Schema({probatio.Required(ATTR_CONFIG_ENTRY_ID): str, **fields}),
        )
