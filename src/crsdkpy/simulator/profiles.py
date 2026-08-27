"""Simulator camera profiles.

A profile describes a synthetic camera: which modes it supports, what each mode
can do, which properties it exposes and roughly how fast it responds.

Timings in the shipped profiles are **representative observed values** from
characterization, not guarantees. They exist so that development against the
simulator feels like the real thing, not so that anything can depend on them.

The profile set deliberately includes cameras that contradict the first
characterized body, so that no implementation can quietly hard-code its shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

from .. import conformance
from ..capabilities import CameraCapabilities
from ..enums import (
    PreviewKind,
    PropertyAccess,
    PropertyValueType,
    SessionMode,
    StillDestination,
)
from ..properties import Property, PropertyCode, PropertyRange

__all__ = [
    "CameraProfile",
    "ModeProfile",
    "PreviewSpec",
    "Timings",
    "PROFILES",
    "get_profile",
    "profile_names",
]

# Vendor lock-indicator values, mirrored so simulated property values look like
# the real ones rather than Python booleans.
LOCK_UNLOCKED = 1
LOCK_LOCKED = 2

# Vendor focus-indication values.
FOCUS_UNLOCKED = 0x0001
FOCUS_FOCUSED_AF_S = 0x0102
FOCUS_NOT_FOCUSED_AF_S = 0x0202
FOCUS_FOCUSED_AF_C = 0x0103
FOCUS_NOT_FOCUSED_AF_C = 0x0203
FOCUS_TRACKING_AF_C = 0x0303

CODE_S1 = 0x0001
CODE_FOCUS_MODE = 0x0109
CODE_FOCUS_INDICATION = 0x0707
CODE_DRIVE_MODE = 0x010E
CODE_RECORDING_STATE = 0x0705
CODE_ISO = 0x0104
CODE_F_NUMBER = 0x0100
CODE_EXPOSURE_COMPENSATION = 0x0101
CODE_SHUTTER_SPEED = 0x0103
CODE_EXPOSURE_PROGRAM = 0x0105
CODE_WHITE_BALANCE = 0x0108
CODE_DESTINATION = 0x0119
CODE_CAMERA_CAUTION = 0x078B
CODE_SYSTEM_CAUTION = 0x078C
CODE_FOCUS_POSITION_SETTING = 0x020E
CODE_FOCUS_POSITION_CURRENT = 0x0766
CODE_FOCUS_DRIVING_STATUS = 0x0767

# Lens identity is three string-valued properties; the focus-distance table is
# a separate feature gated on the enable status.
CODE_LENS_MODEL_NAME = 0x0765
CODE_LENS_VERSION_NUMBER = 0x075F
CODE_LENS_SERIAL_NUMBER = 0x07CB
CODE_LENS_INFORMATION_ENABLE_STATUS = 0x0756

# Zoom. Every documented control is a property; none of it is an operation.
CODE_ZOOM_SCALE = 0x0124
CODE_ZOOM_SETTING = 0x0125
CODE_ZOOM_OPERATION = 0x0126
CODE_ZOOM_POSITION_SETTING = 0x02BC
CODE_REMOCON_ZOOM_SPEED_TYPE = 0x050C
CODE_ZOOM_OPERATION_STATUS = 0x071E
CODE_ZOOM_BAR_INFORMATION = 0x071F
CODE_ZOOM_TYPE_STATUS = 0x0720
CODE_ZOOM_SPEED_RANGE = 0x0724
CODE_ZOOM_DRIVING_STATUS = 0x0792
CODE_ZOOM_POSITION_CURRENT_VALUE = 0x07A1

# Electronic framing. The command version doubles as the availability gate.
CODE_EFRAMING_RECORDING_IMAGE_CROP = 0x01F0
CODE_EFRAMING_HDMI_CROP = 0x01F1
CODE_CAMERA_EFRAMING = 0x01F2
CODE_EFRAMING_TYPE = 0x07D1
CODE_EFRAMING_COMMAND_VERSION = 0x07D2

# Interval (still) shooting. The camera owns the sequence.
CODE_INTERVAL_START_TIME = 0x01FC
CODE_INTERVAL_SHOOTING_INTERVAL = 0x01FD
CODE_INTERVAL_PRIORITY = 0x01FE
CODE_INTERVAL_NUMBER_OF_SHOTS = 0x01FF
CODE_INTERVAL_AE_TRACKING = 0x0200
CODE_INTERVAL_SHUTTER_TYPE = 0x0201
CODE_INTERVAL_REC_MODE = 0x0505
CODE_INTERVAL_REC_STATUS = 0x0718

CODE_STILL_IMAGE_TRANS_SIZE = 0x0506


@dataclass(frozen=True)
class Timings:
    """Representative latencies, in milliseconds."""

    connect: int = 150
    disconnect: int = 100
    #: Half-press asserted to a focused state on the leading channel.
    focus_confirm: int = 175
    #: How far the second focus channel trails the first.
    focus_channel_skew: int = 15
    #: Half-press to a definite not-focused verdict.
    focus_fail: int = 731
    #: AF-C passes through tracking before focusing.
    focus_tracking: int = 122
    #: Release down to the exposure-complete event.
    exposure: int = 430
    #: Exposure to durable content appearing in the index.
    content: int = 470
    #: Content appearing to preview bytes being pullable.
    preview: int = 90
    #: Release down to the postview notification.
    postview: int = 250
    recording_start: int = 200
    recording_stop: int = 175
    #: Live-view interruption around an exposure.
    live_view_gap: int = 108
    reconnect: int = 28_000
    #: A related property arriving after the batch that caused it.
    property_straggler: int = 102
    #: Request accepted to the vendor reporting its outcome as a warning.
    request_outcome: int = 120


@dataclass(frozen=True)
class PreviewSpec:
    """Shape of one preview form."""

    width: int
    height: int
    byte_length: int
    mime: str = "image/jpeg"


@dataclass(frozen=True)
class LiveViewProfile:
    width: int = 640
    height: int = 428
    #: Nominal cadence. Real cadence varies with scene and exposure, so the
    #: simulator jitters around this rather than emitting a metronome.
    frames_per_second: float = 29.2
    byte_length: int = 85_000
    byte_jitter: int = 12_000
    buffer_size: int = 307_200


@dataclass(frozen=True)
class ModeProfile:
    """What one control mode can do on this camera."""

    live_view: bool = False
    content_index: bool = False
    thumbnail: bool = False
    screennail: bool = False
    #: Whether the postview configuration call is accepted in this mode.
    postview_configuration: bool = False
    #: Whether this mode can deliver postview bytes at all.
    postview_delivery_possible: bool = False
    #: Whether delivery additionally requires the host in the destination.
    postview_delivery_requires_host: bool = True
    still_capture: bool = True
    video: bool = True
    #: Property codes present only in this mode. Models the observed case
    #: where live-view-related codes exist only where live view exists.
    extra_property_codes: tuple[int, ...] = ()
    extra_capabilities: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class CameraProfile:
    """A complete synthetic camera."""

    name: str
    model: str
    firmware: str = "1.00"
    serial: Optional[str] = None
    transport: str = "usb"
    adapter: Optional[str] = None
    usb_pid: Optional[int] = None
    still_capture: bool = True
    autofocus_s1: bool = True
    video: bool = True
    modes: Mapping[SessionMode, ModeProfile] = field(default_factory=dict)
    destinations: frozenset[StillDestination] = field(
        default_factory=lambda: frozenset({StillDestination.MEMORY_CARD})
    )
    default_destination: StillDestination = StillDestination.MEMORY_CARD
    base_property_codes: tuple[int, ...] = ()
    timings: Timings = field(default_factory=Timings)
    live_view: LiveViewProfile = field(default_factory=LiveViewProfile)
    previews: Mapping[PreviewKind, PreviewSpec] = field(default_factory=dict)
    extra_camera_capabilities: Mapping[str, bool] = field(default_factory=dict)
    #: Vendor focus values this camera reports, keyed by logical outcome.
    supports_af_c: bool = True
    #: Device status. Present on every profile so a client can read battery
    #: and media without knowing which body it is talking to.
    battery_percent: int = 87
    usb_power: bool = False
    #: One entry per media slot; False models an empty slot.
    slots: tuple[bool, ...] = (True,)
    remaining_shots: int = 1234
    remaining_seconds: int = 5678

    def camera_capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            still_capture=self.still_capture,
            autofocus_s1=self.autofocus_s1,
            video=self.video,
            modes=frozenset(self.modes),
            destinations=frozenset(self.destinations),
            live_view_any_mode=any(m.live_view for m in self.modes.values()),
            content_index_any_mode=any(m.content_index for m in self.modes.values()),
            extra=dict(self.extra_camera_capabilities),
        )

    def property_codes_for(self, mode: SessionMode) -> tuple[int, ...]:
        mode_profile = self.modes[mode]
        return tuple(
            sorted(
                set(self.base_property_codes)
                | set(mode_profile.extra_property_codes)
            )
        )


def _base_properties() -> tuple[int, ...]:
    return (
        CODE_S1,
        CODE_ISO,
        CODE_F_NUMBER,
        CODE_EXPOSURE_COMPENSATION,
        CODE_SHUTTER_SPEED,
        CODE_EXPOSURE_PROGRAM,
        CODE_WHITE_BALANCE,
        CODE_DRIVE_MODE,
        CODE_FOCUS_MODE,
        CODE_RECORDING_STATE,
        CODE_FOCUS_INDICATION,
        CODE_CAMERA_CAUTION,
        CODE_SYSTEM_CAUTION,
        CODE_DESTINATION,
        CODE_FOCUS_POSITION_SETTING,
        CODE_FOCUS_POSITION_CURRENT,
        CODE_FOCUS_DRIVING_STATUS,
        CODE_LENS_MODEL_NAME,
        CODE_LENS_VERSION_NUMBER,
        CODE_LENS_SERIAL_NUMBER,
        CODE_LENS_INFORMATION_ENABLE_STATUS,
        CODE_ZOOM_SCALE,
        CODE_ZOOM_SETTING,
        CODE_ZOOM_OPERATION,
        CODE_ZOOM_POSITION_SETTING,
        CODE_REMOCON_ZOOM_SPEED_TYPE,
        CODE_ZOOM_OPERATION_STATUS,
        CODE_ZOOM_BAR_INFORMATION,
        CODE_ZOOM_TYPE_STATUS,
        CODE_ZOOM_SPEED_RANGE,
        CODE_ZOOM_DRIVING_STATUS,
        CODE_ZOOM_POSITION_CURRENT_VALUE,
        CODE_EFRAMING_RECORDING_IMAGE_CROP,
        CODE_EFRAMING_HDMI_CROP,
        CODE_CAMERA_EFRAMING,
        CODE_EFRAMING_TYPE,
        CODE_EFRAMING_COMMAND_VERSION,
        CODE_INTERVAL_START_TIME,
        CODE_INTERVAL_SHOOTING_INTERVAL,
        CODE_INTERVAL_PRIORITY,
        CODE_INTERVAL_NUMBER_OF_SHOTS,
        CODE_INTERVAL_AE_TRACKING,
        CODE_INTERVAL_SHUTTER_TYPE,
        CODE_INTERVAL_REC_MODE,
        CODE_INTERVAL_REC_STATUS,
        CODE_STILL_IMAGE_TRANS_SIZE,
    )


def build_property(code: int, value: object) -> Property:
    """Construct a simulated property with plausible metadata."""
    access = PropertyAccess.READ_WRITE
    allowed: tuple[object, ...] = ()
    if code in (CODE_FOCUS_INDICATION, CODE_RECORDING_STATE, CODE_CAMERA_CAUTION,
                CODE_SYSTEM_CAUTION, CODE_FOCUS_POSITION_CURRENT,
                CODE_FOCUS_DRIVING_STATUS, CODE_LENS_MODEL_NAME,
                CODE_LENS_VERSION_NUMBER, CODE_LENS_SERIAL_NUMBER,
                CODE_LENS_INFORMATION_ENABLE_STATUS,
                CODE_ZOOM_OPERATION_STATUS, CODE_ZOOM_TYPE_STATUS,
                CODE_ZOOM_SPEED_RANGE, CODE_ZOOM_DRIVING_STATUS,
                CODE_ZOOM_BAR_INFORMATION, CODE_ZOOM_POSITION_CURRENT_VALUE,
                CODE_EFRAMING_TYPE, CODE_EFRAMING_COMMAND_VERSION,
                CODE_CAMERA_EFRAMING, CODE_INTERVAL_REC_STATUS):
        access = PropertyAccess.READ_ONLY
    # The zoom drive is a write-only property on the reference body.
    if code == CODE_ZOOM_OPERATION:
        access = PropertyAccess.WRITE_ONLY
    if code == CODE_S1:
        allowed = (LOCK_UNLOCKED, LOCK_LOCKED)
    if code == CODE_FOCUS_MODE:
        # Physical control on many bodies: observable, not settable remotely.
        access = PropertyAccess.READ_ONLY
    if code == CODE_ISO:
        allowed = (100, 125, 200, 400, 0xFFFFFF)
    if code == CODE_F_NUMBER:
        allowed = (140, 200, 280, 400)
    if code == CODE_SHUTTER_SPEED:
        allowed = (0x0001003C, 0x0001007D, 0x000101F4)
    if code == CODE_EXPOSURE_PROGRAM:
        allowed = (1, 2, 3, 4)
    if code == CODE_WHITE_BALANCE:
        allowed = (0x0000, 0x0011, 0x0012, 0x0013, 0x0014, 0x0100)
    if code == CODE_DRIVE_MODE:
        allowed = (0x00000001, 0x00010001, 0x00030001)
    value_range = None
    if code in (CODE_FOCUS_POSITION_SETTING, CODE_FOCUS_POSITION_CURRENT):
        value_range = PropertyRange(0, 65535, 1)
    if code == CODE_EXPOSURE_COMPENSATION:
        value_range = PropertyRange(-3000, 3000, 1000)
    if code in (CODE_ZOOM_POSITION_SETTING, CODE_ZOOM_POSITION_CURRENT_VALUE):
        value_range = PropertyRange(0, 65535, 1)
    if code == CODE_ZOOM_SCALE:
        # Units of 0.001, so this body spans x1.0 to x8.0 in 0.2 steps.
        value_range = PropertyRange(1000, 8000, 200)
    if code in (CODE_ZOOM_OPERATION, CODE_ZOOM_SPEED_RANGE):
        # A body with a variable speed range; the documented floor is -1..1.
        value_range = PropertyRange(-8, 8, 1)
    if code == CODE_INTERVAL_SHOOTING_INTERVAL:
        # Ten times the interval in seconds, so 1.0s to 60.0s in 0.1s steps.
        value_range = PropertyRange(10, 600, 1)
    if code == CODE_INTERVAL_START_TIME:
        value_range = PropertyRange(1, 6553, 1)
    if code == CODE_INTERVAL_NUMBER_OF_SHOTS:
        value_range = PropertyRange(1, 9999, 1)
    if code == CODE_LENS_INFORMATION_ENABLE_STATUS:
        allowed = (0, 1)
    if code == CODE_ZOOM_SETTING:
        allowed = (1, 2, 3, 4)
    if code == CODE_REMOCON_ZOOM_SPEED_TYPE:
        allowed = (0, 1, 2)
    if code == CODE_STILL_IMAGE_TRANS_SIZE:
        allowed = (0, 1)
    if code == CODE_INTERVAL_REC_MODE:
        allowed = (1, 2)
    if code == CODE_INTERVAL_PRIORITY:
        allowed = (1, 2)
    if code == CODE_INTERVAL_AE_TRACKING:
        allowed = (1, 2, 3, 4)
    if code == CODE_INTERVAL_SHUTTER_TYPE:
        allowed = (1, 2, 3)
    if code in (CODE_EFRAMING_RECORDING_IMAGE_CROP, CODE_EFRAMING_HDMI_CROP):
        allowed = (1, 2)
    # Lens identity answers through the string accessor, as on real hardware.
    if code in (CODE_LENS_MODEL_NAME, CODE_LENS_VERSION_NUMBER,
                CODE_LENS_SERIAL_NUMBER):
        return Property(
            code=PropertyCode(code),
            value=value,
            value_type=PropertyValueType.STRING,
            access=access,
        )
    return Property(
        code=PropertyCode(code),
        value=value,
        value_type=PropertyValueType.INT,
        access=access,
        allowed_values=allowed,
        value_range=value_range,
    )


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

#: The first fully characterized body. Mode/destination matrix mirrors the
#: reviewed characterization: live view only in Remote, content APIs only in
#: RemoteTransfer, postview configuration only in RemoteTransfer, and postview
#: delivery gated on the destination including the host in either mode.
def _reference() -> conformance.ConformanceProfile:
    """The sanitized measurement the FX3A profile is built from.

    Loaded rather than transcribed. Every fact below that came off hardware --
    the identity, what each control mode can do, which codes exist only where
    live view does -- has one home, and it is the tracked profile.
    """
    return conformance.reference_profile()


def _mode_from_reference(
    profile: conformance.ConformanceProfile, control_mode: str, **extra: object
) -> ModeProfile:
    """Build a mode from the measured capabilities of its two surfaces.

    Destination did not change the property surface on the reference body, but
    it did change postview delivery, so the two surfaces of a mode are read
    together: what they agree on describes the mode, and where they disagree
    describes the destination's effect.
    """
    card = profile.surfaces[f"{control_mode}+memory_card"].capabilities
    host = profile.surfaces[f"{control_mode}+host_and_memory_card"].capabilities
    # Codes this mode has that the other one does not. Recorded as the other
    # surface's absent list, which is where the measurement actually lives.
    other = (
        "remote_transfer" if control_mode == "remote" else "remote"
    )
    only_here = tuple(
        sorted(profile.surfaces[f"{other}+memory_card"].absent_codes)
    )
    return ModeProfile(
        live_view=bool(card.get("live_view")),
        content_index=bool(card.get("content_index")),
        thumbnail=bool(card.get("thumbnail")),
        screennail=bool(card.get("screennail")),
        postview_configuration=bool(card.get("postview_configuration")),
        postview_delivery_possible=bool(
            card.get("postview_delivery") or host.get("postview_delivery")
        ),
        postview_delivery_requires_host=(
            not card.get("postview_delivery") and bool(host.get("postview_delivery"))
        ),
        still_capture=bool(card.get("still_capture", True)),
        video=bool(card.get("video", True)),
        extra_property_codes=only_here,
        **extra,
    )


_REFERENCE = _reference()

#: The reference body, as measured. The synthetic values, timings and preview
#: shapes below are the simulator's own; the identity and the per-mode
#: capabilities are read from the tracked profile so there is one place they
#: can be wrong. This is deliberately not an emulator: it does not reproduce
#: the body's several hundred properties or their values.
FX3A = CameraProfile(
    name="fx3a",
    model=_REFERENCE.body["model"],
    firmware=_REFERENCE.body["firmware"],
    transport="usb",
    adapter=_REFERENCE.body["adapter"],
    usb_pid=int(_REFERENCE.body["usb_pid"], 16),
    still_capture=True,
    autofocus_s1=True,
    video=True,
    modes={
        SessionMode.REMOTE: _mode_from_reference(_REFERENCE, "remote"),
        SessionMode.REMOTE_TRANSFER: _mode_from_reference(
            _REFERENCE, "remote_transfer"
        ),
    },
    destinations=frozenset(
        {StillDestination.MEMORY_CARD, StillDestination.HOST_AND_MEMORY_CARD}
    ),
    base_property_codes=_base_properties(),
    timings=Timings(),
    live_view=LiveViewProfile(),
    previews={
        PreviewKind.POSTVIEW: PreviewSpec(4240, 2832, 4_639_743),
        PreviewKind.SCREENNAIL: PreviewSpec(1616, 1080, 114_211),
        PreviewKind.THUMBNAIL: PreviewSpec(160, 120, 56_113),
    },
)


#: A deliberately limited body: no half-press stage, no video, no transfer
#: mode, small property set. Exercises graceful degradation.
MINIMAL_STILL = CameraProfile(
    name="minimal_still",
    model="SIM-MinimalStill",
    firmware="1.00",
    transport="usb",
    still_capture=True,
    autofocus_s1=False,
    video=False,
    supports_af_c=False,
    modes={
        SessionMode.REMOTE: ModeProfile(
            live_view=True,
            content_index=False,
            thumbnail=False,
            screennail=False,
            postview_configuration=False,
            postview_delivery_possible=False,
            video=False,
        ),
    },
    destinations=frozenset({StillDestination.MEMORY_CARD}),
    base_property_codes=(CODE_ISO, CODE_DRIVE_MODE, CODE_RECORDING_STATE),
    timings=Timings(focus_confirm=0, exposure=300, content=400),
    live_view=LiveViewProfile(width=512, height=384, frames_per_second=15.0,
                              byte_length=40_000, byte_jitter=5_000,
                              buffer_size=200_000),
    previews={},
)


#: Contradicts the first characterized body on purpose: live view works in
#: RemoteTransfer, the content index works in Remote, and postview delivery
#: does not care about the destination. Any implementation that hard-codes the
#: FX3A mapping fails against this profile.
INVERTED_MODES = CameraProfile(
    name="inverted_modes",
    model="SIM-InvertedModes",
    firmware="3.10",
    transport="ethernet",
    still_capture=True,
    autofocus_s1=True,
    video=True,
    modes={
        SessionMode.REMOTE: ModeProfile(
            live_view=False,
            content_index=True,
            thumbnail=True,
            screennail=True,
            postview_configuration=True,
            postview_delivery_possible=True,
            postview_delivery_requires_host=False,
        ),
        SessionMode.REMOTE_TRANSFER: ModeProfile(
            live_view=True,
            content_index=True,
            thumbnail=False,
            screennail=True,
            postview_configuration=False,
            postview_delivery_possible=False,
        ),
    },
    destinations=frozenset(
        {
            StillDestination.MEMORY_CARD,
            StillDestination.HOST,
            StillDestination.HOST_AND_MEMORY_CARD,
        }
    ),
    base_property_codes=_base_properties(),
    timings=Timings(focus_confirm=90, exposure=250, content=300, reconnect=5_000),
    live_view=LiveViewProfile(width=1024, height=576, frames_per_second=50.0),
    previews={
        PreviewKind.POSTVIEW: PreviewSpec(6000, 4000, 2_100_000),
        PreviewKind.SCREENNAIL: PreviewSpec(1920, 1080, 180_000),
        PreviewKind.THUMBNAIL: PreviewSpec(320, 240, 30_000),
    },
)


#: A body from the future: reports property codes and capability names this
#: release has never heard of, and emits event codes with no typed form.
#: The library must stay fully usable.
FUTURE_UNKNOWN = CameraProfile(
    name="future_unknown",
    model="SIM-FutureBody",
    firmware="9.99",
    transport="usb",
    still_capture=True,
    autofocus_s1=True,
    video=True,
    modes={
        SessionMode.REMOTE: ModeProfile(
            live_view=True,
            content_index=True,
            thumbnail=True,
            screennail=True,
            postview_configuration=True,
            postview_delivery_possible=True,
            postview_delivery_requires_host=False,
            extra_property_codes=(0x7F01, 0x7F02, 0x7FFE),
            extra_capabilities={
                "holographic_viewfinder": True,
                "neural_subject_lock": True,
            },
        ),
    },
    destinations=frozenset(
        {StillDestination.MEMORY_CARD, StillDestination.HOST_AND_MEMORY_CARD}
    ),
    base_property_codes=_base_properties(),
    timings=Timings(focus_confirm=120, exposure=200, content=250),
    previews={
        PreviewKind.POSTVIEW: PreviewSpec(8000, 6000, 3_000_000),
        PreviewKind.SCREENNAIL: PreviewSpec(1600, 1200, 150_000),
        PreviewKind.THUMBNAIL: PreviewSpec(200, 150, 40_000),
    },
    extra_camera_capabilities={"quantum_stabilizer": True},
)


PROFILES: dict[str, CameraProfile] = {
    p.name: p for p in (FX3A, MINIMAL_STILL, INVERTED_MODES, FUTURE_UNKNOWN)
}


def profile_names() -> Sequence[str]:
    return tuple(sorted(PROFILES))


def get_profile(name: str) -> CameraProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(
            f"unknown simulator profile {name!r}; "
            f"available: {', '.join(profile_names())}"
        ) from None
