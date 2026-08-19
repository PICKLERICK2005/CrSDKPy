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

from ..capabilities import CameraCapabilities
from ..enums import (
    PreviewKind,
    PropertyAccess,
    PropertyValueType,
    SessionMode,
    StillDestination,
)
from ..properties import Property, PropertyCode

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
CODE_DESTINATION = 0x0119
CODE_CAMERA_CAUTION = 0x078B
CODE_SYSTEM_CAUTION = 0x078C


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
        CODE_DRIVE_MODE,
        CODE_FOCUS_MODE,
        CODE_RECORDING_STATE,
        CODE_FOCUS_INDICATION,
        CODE_CAMERA_CAUTION,
        CODE_SYSTEM_CAUTION,
        CODE_DESTINATION,
    )


def build_property(code: int, value: object) -> Property:
    """Construct a simulated property with plausible metadata."""
    access = PropertyAccess.READ_WRITE
    allowed: tuple[object, ...] = ()
    if code in (CODE_FOCUS_INDICATION, CODE_RECORDING_STATE, CODE_CAMERA_CAUTION,
                CODE_SYSTEM_CAUTION):
        access = PropertyAccess.READ_ONLY
    if code == CODE_S1:
        allowed = (LOCK_UNLOCKED, LOCK_LOCKED)
    if code == CODE_FOCUS_MODE:
        # Physical control on many bodies: observable, not settable remotely.
        access = PropertyAccess.READ_ONLY
    return Property(
        code=PropertyCode(code),
        value=value,
        value_type=PropertyValueType.INT,
        access=access,
        allowed_values=allowed,
    )


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

#: The first fully characterized body. Mode/destination matrix mirrors the
#: reviewed characterization: live view only in Remote, content APIs only in
#: RemoteTransfer, postview configuration only in RemoteTransfer, and postview
#: delivery gated on the destination including the host in either mode.
FX3A = CameraProfile(
    name="fx3a",
    model="ILME-FX3A",
    firmware="2.02",
    transport="usb",
    adapter="Cr_PTP_USB",
    usb_pid=0x0F52,
    still_capture=True,
    autofocus_s1=True,
    video=True,
    modes={
        SessionMode.REMOTE: ModeProfile(
            live_view=True,
            content_index=False,
            thumbnail=False,
            screennail=False,
            postview_configuration=False,
            postview_delivery_possible=True,
            postview_delivery_requires_host=True,
            # Two live-view codes exist only where live view exists. These are
            # deliberately codes CrSDKPy has no name for.
            extra_property_codes=(0x0581, 0x0582),
        ),
        SessionMode.REMOTE_TRANSFER: ModeProfile(
            live_view=False,
            content_index=True,
            thumbnail=True,
            screennail=True,
            postview_configuration=True,
            postview_delivery_possible=True,
            postview_delivery_requires_host=True,
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
