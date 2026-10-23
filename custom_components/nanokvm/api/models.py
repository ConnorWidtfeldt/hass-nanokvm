"""NanoKVM 2.5.2 wire models, independent of Home Assistant."""

from dataclasses import asdict, dataclass, field
import json
from typing import Annotated, Literal, cast

import probatio

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)


def json_object(value: str | bytes) -> dict[str, JsonValue]:
    """Decode a JSON object at the transport boundary."""
    data = json.loads(value)
    if not isinstance(data, dict):
        raise probatio.Invalid("Expected a JSON object")
    return cast(dict[str, JsonValue], data)


def integer(value: object) -> int:
    """Reject booleans where the firmware expects an integer."""
    if type(value) is not int:
        raise probatio.Invalid("Expected an integer")
    return value


NonNegativeInt = Annotated[int, integer, probatio.Range(min=0)]
PositiveInt = Annotated[int, integer, probatio.Range(min=1)]


class Model(probatio.SchemaMixin, extra=probatio.REMOVE_EXTRA):
    """Validate device responses without coercing invalid field types."""


@dataclass
class Info(Model):
    """Device identity and installed software."""

    device_key: Annotated[
        str, probatio.Key(alias="deviceKey"), probatio.Length(min=1)
    ] = field(repr=False)
    application: str
    image: str
    mdns: str


@dataclass
class Hardware(Model):
    """Hardware revision reported by the device."""

    version: str


@dataclass
class Account(Model):
    """Authenticated account role."""

    username: str = field(repr=False)
    role: Literal["admin", "user"]


@dataclass
class Gpio(Model):
    """ATX status LEDs."""

    pwr: bool
    hdd: bool


@dataclass
class Hdmi(Model):
    """PCIe capture settings and signal status."""

    enabled: bool
    signal: bool
    idle_timeout: Annotated[
        int, integer, probatio.Key(alias="idleTimeout"), probatio.Range(min=0)
    ]


@dataclass
class KeyboardLeds(Model):
    """Host keyboard LED states, preserving unknown values."""

    known: bool
    num_lock: Annotated[bool, probatio.Key(alias="numLock")]
    caps_lock: Annotated[bool, probatio.Key(alias="capsLock")]
    scroll_lock: Annotated[bool, probatio.Key(alias="scrollLock")]


@dataclass
class Resolution(Model):
    """Input signal dimensions."""

    width: Annotated[int, integer, probatio.Range(min=0)]
    height: Annotated[int, integer, probatio.Range(min=0)]


@dataclass
class ManualRegion(Model, extra=probatio.PREVENT_EXTRA):
    """A bounded crop within the input frame."""

    frame_width: Annotated[
        int, integer, probatio.Key(alias="frameWidth"), probatio.Range(min=1)
    ]
    frame_height: Annotated[
        int, integer, probatio.Key(alias="frameHeight"), probatio.Range(min=1)
    ]
    left: Annotated[int, integer, probatio.Range(min=0)]
    top: Annotated[int, integer, probatio.Range(min=0)]
    width: Annotated[int, integer, probatio.Range(min=1)]
    height: Annotated[int, integer, probatio.Range(min=1)]

    def __post_init__(self) -> None:
        """Reject crops extending beyond the input signal."""
        if (
            self.left + self.width > self.frame_width
            or self.top + self.height > self.frame_height
        ):
            raise ValueError("Region exceeds the input frame")


@dataclass
class InputRegionUpdate(Model, extra=probatio.PREVENT_EXTRA):
    """Update a region while leaving omitted settings on the device."""

    mode: Literal["off", "auto", "manual"]
    frame_width: Annotated[PositiveInt | None, probatio.Key(alias="frameWidth")] = None
    frame_height: Annotated[PositiveInt | None, probatio.Key(alias="frameHeight")] = (
        None
    )
    left: NonNegativeInt | None = None
    top: NonNegativeInt | None = None
    width: PositiveInt | None = None
    height: PositiveInt | None = None
    resolutions: Annotated[
        list[Resolution] | None, probatio.Key(extra=probatio.REMOVE_EXTRA)
    ] = None
    regions: list[ManualRegion] | None = None
    selected_resolution: Annotated[
        str | None, probatio.Key(alias="selectedResolution")
    ] = None
    selected_region: Annotated[str | None, probatio.Key(alias="selectedRegion")] = None

    def __post_init__(self) -> None:
        """Validate supplied geometry and selections without replacing other settings."""
        geometry = {
            name: value
            for name in (
                "frame_width",
                "frame_height",
                "left",
                "top",
                "width",
                "height",
            )
            if (value := getattr(self, name)) is not None
        }
        if geometry:
            ManualRegion.from_dict(geometry)
        if self.selected_resolution and self.selected_region:
            raise ValueError("Select only one region source")
        for items, selected in (
            (self.resolutions, self.selected_resolution),
            (self.regions, self.selected_region),
        ):
            if items is None:
                continue
            keys = [f"{item.width}x{item.height}" for item in items]
            if any(item.width <= 0 or item.height <= 0 for item in items) or len(
                keys
            ) != len(set(keys)):
                raise ValueError("Region sizes must be positive and unique")
            if selected and selected not in keys:
                raise ValueError("Selection is missing from the supplied list")


@dataclass
class Oled(Model):
    """Optional OLED display configuration."""

    exist: bool
    sleep: Annotated[int, integer, probatio.Range(min=0)]


@dataclass
class Jiggler(Model):
    """Mouse jiggler configuration."""

    enabled: bool
    mode: Literal["relative", "absolute"]


@dataclass
class VirtualDevices(Model):
    """USB network and raw data-partition attachment."""

    network: bool
    disk: bool


@dataclass
class Versions(Model):
    """Application update information."""

    current: str
    latest: str


@dataclass
class CaptureStatus(Model):
    """Status of one video capture mode."""

    ok: bool
    result: Annotated[int, integer]
    message: str = field(repr=False)
    mode: str
    severity: str
    updated_at: Annotated[str, probatio.Key(alias="updatedAt")]


@dataclass
class VideoClients(Model):
    """H.264 viewer counts scoped to a server generation."""

    generation: str
    revision: Annotated[int, integer, probatio.Range(min=0)]
    direct: Annotated[int, integer, probatio.Range(min=0)]
    webrtc: Annotated[int, integer, probatio.Range(min=0)]
    mixed: bool


type Event = CaptureStatus | VideoClients | KeyboardLeds
type Values = dict[str, bool | int | float | str | None]
type Payload = dict[str, JsonValue]


def input_region_payload(value: Payload) -> Payload:
    """Validate a partial update and preserve omitted versus explicit null fields."""
    try:
        validated = InputRegionUpdate.from_dict(value)
    except probatio.Invalid:
        raise ValueError("Invalid input region") from None
    parsed = asdict(validated)
    aliases = {
        "frame_width": "frameWidth",
        "frame_height": "frameHeight",
        "selected_resolution": "selectedResolution",
        "selected_region": "selectedRegion",
    }
    result = {
        aliases.get(key, key): item
        for key, item in parsed.items()
        if key in value or aliases.get(key, key) in value
    }
    if result.get("regions") is not None:
        result["regions"] = [
            {aliases.get(key, key): item for key, item in region.items()}
            for region in parsed["regions"]
        ]
    return result
