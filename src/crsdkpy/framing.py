"""Electronic framing.

Unlike zoom, this is not a property family with a write. The vendor exposes
two real operations and documents an order between them: ``ExecuteEframing``
applies a complete framing description by absolute position, and
``UpdateEframingArea`` nudges one already-applied area by a relative amount
and "is enabled after ExecuteEframing has been successfully executed. This API
alone cannot move or resize framing frames." That ordering is modelled here
rather than flattened into two independent calls.

Coordinates are in whatever space the caller's denominators define. The vendor
states that every coordinate and denominator "must be multiplied by 1024" on
the wire, so that scaling is applied here and callers pass plain numbers. That
is the vendor's own stated contract, not an inferred one -- and it is the only
arithmetic this module does. Nothing here infers crop semantics, aspect
behaviour, or what an area means to the camera.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import PropertyNotSupportedError, UnsupportedOperationError
from .settings import Setting

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = [
    "EFRAMING_AREA_MAX",
    "EFRAMING_SCALE",
    "Framing",
    "FramingArea",
    "FramingRectangle",
    "FramingType",
]

EFRAMING_SCALE_AUTO = 0x01ED
EFRAMING_SPEED_AUTO = 0x01EE
EFRAMING_MODE_AUTO = 0x01EF
EFRAMING_RECORDING_IMAGE_CROP = 0x01F0
EFRAMING_HDMI_CROP = 0x01F1
CAMERA_EFRAMING = 0x01F2
EFRAMING_AUTO_FRAMING = 0x0329
EFRAMING_TRACKING_START_MODE = 0x032A
EFRAMING_PRODUCTION_EFFECT = 0x032B
EFRAMING_SPEED_PTZ = 0x032C
EFRAMING_TYPE = 0x07D1
EFRAMING_COMMAND_VERSION = 0x07D2

#: The vendor's fixed multiplier for every coordinate and denominator.
EFRAMING_SCALE = 1024

#: CrEFRAMING_AREAINFO_MAX, and the range CrEframingAreaNumber covers.
EFRAMING_AREA_MAX = 4

#: Widths of the vendor parameters the scaled values must fit.
_EXECUTE_LIMIT = 2**31 - 1
_UPDATE_LIMIT = 2**15 - 1


class FramingType:
    """Documented ``CrEframingType`` values.

    A camera may report a value this release has not seen; every accessor
    keeps the number, so an unrecognised type stays usable.
    """

    NONE = 1
    AUTO = 2
    SINGLE = 3
    PTZ = 5
    HOLD_CURRENT_POSITION = 8
    FORCE_ZOOM_OUT = 9

    _NAMES = {
        1: "none",
        2: "auto",
        3: "single",
        5: "ptz",
        8: "hold_current_position",
        9: "force_zoom_out",
    }

    @classmethod
    def name_for(cls, value: int) -> Optional[str]:
        return cls._NAMES.get(int(value))


class FramingArea:
    """Documented ``CrEframingAreaGroup`` values."""

    INPUT = 1
    OUTPUT = 2


@dataclass(frozen=True)
class FramingRectangle:
    """One framing rectangle, in the caller's own coordinate space.

    These are plain numbers. The vendor's ``x1024`` wire scaling is applied
    when the rectangle is sent, not stored here.
    """

    x: int
    y: int
    width: int
    height: int


def _scaled(name: str, value: int, limit: int) -> int:
    """Apply the vendor's fixed multiplier, refusing what will not fit.

    The vendor's sample truncates the product into a narrower parameter
    without checking. Silently sending a different rectangle than the caller
    asked for is worse than refusing, so this refuses.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"framing {name} must be an integer")
    scaled = value * EFRAMING_SCALE
    if not -limit - 1 <= scaled <= limit:
        raise ValueError(
            f"framing {name} {value} does not fit the vendor parameter once "
            f"multiplied by {EFRAMING_SCALE}; the usable range is "
            f"{-(limit + 1) // EFRAMING_SCALE} to {limit // EFRAMING_SCALE}"
        )
    return scaled


def _area_number(area: int) -> int:
    if isinstance(area, bool) or not isinstance(area, int):
        raise TypeError("framing area number must be an integer")
    if not 1 <= area <= EFRAMING_AREA_MAX:
        raise ValueError(
            f"framing area number must be 1 to {EFRAMING_AREA_MAX}, got {area}"
        )
    return area


def _area_group(group: int) -> int:
    if group not in (FramingArea.INPUT, FramingArea.OUTPUT):
        raise ValueError(
            "framing area group must be FramingArea.INPUT or "
            f"FramingArea.OUTPUT, got {group!r}"
        )
    return int(group)


class Framing:
    """Electronic framing: its settings, its state, and its two operations."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._executed = False
        #: Whether the camera's own framing feature is switched on.
        self.camera_framing = Setting(session, CAMERA_EFRAMING)
        self.auto_framing = Setting(session, EFRAMING_AUTO_FRAMING)
        self.scale_auto = Setting(session, EFRAMING_SCALE_AUTO)
        self.speed_auto = Setting(session, EFRAMING_SPEED_AUTO)
        self.mode_auto = Setting(session, EFRAMING_MODE_AUTO)
        self.speed_ptz = Setting(session, EFRAMING_SPEED_PTZ)
        self.tracking_start_mode = Setting(session, EFRAMING_TRACKING_START_MODE)
        self.production_effect = Setting(session, EFRAMING_PRODUCTION_EFFECT)
        self.recording_image_crop = Setting(session, EFRAMING_RECORDING_IMAGE_CROP)
        self.hdmi_crop = Setting(session, EFRAMING_HDMI_CROP)

    # -- state -------------------------------------------------------------
    @property
    def available(self) -> bool:
        """Whether the camera says framing can be executed.

        The vendor gates the feature on ``EframingCommandVersion``: "When
        Eframing feature can not be executed, enableFlag is Disable." So the
        test is that the property is present *and* the camera has not disabled
        it, which is the camera's answer rather than a model lookup.
        """
        try:
            prop = self._session.properties.get(EFRAMING_COMMAND_VERSION)
        except PropertyNotSupportedError:
            return False
        return prop.access.readable or prop.access.writable

    @property
    def command_version(self) -> Optional[int]:
        """Vendor framing command version, unmodified."""
        try:
            return int(self._session.properties.get(EFRAMING_COMMAND_VERSION).value)
        except PropertyNotSupportedError:
            return None

    @property
    def type(self) -> Optional[int]:
        """Framing type the camera currently reports, as its own number."""
        try:
            return int(self._session.properties.get(EFRAMING_TYPE).value)
        except PropertyNotSupportedError:
            return None

    @property
    def type_name(self) -> Optional[str]:
        """Name for :attr:`type`, or ``None`` for an unrecognised value."""
        value = self.type
        return None if value is None else FramingType.name_for(value)

    @property
    def executed(self) -> bool:
        """Whether an execute has succeeded on this session.

        The vendor makes this the precondition for a relative update, and it
        is not something the camera reports back, so it is tracked here.
        """
        return self._executed

    # -- operations --------------------------------------------------------
    def execute(
        self,
        *,
        horizontal_denominator: int,
        vertical_denominator: int,
        framing_type: int,
        input_areas: Optional[dict[int, FramingRectangle]] = None,
        output_areas: Optional[dict[int, FramingRectangle]] = None,
    ) -> None:
        """Apply a complete framing description by absolute position.

        ``framing_type`` is a vendor ``CrEframingType`` value; unrecognised
        numbers pass through, because a firmware may add one. Area numbers run
        1 to :data:`EFRAMING_AREA_MAX`. Every coordinate and denominator is
        multiplied by :data:`EFRAMING_SCALE` on the way out, as the vendor
        requires.
        """
        self._require_available("framing.execute")
        if isinstance(framing_type, bool) or not isinstance(framing_type, int):
            raise TypeError("framing type must be an integer vendor value")

        payload = {
            "horizontal_denominator": _scaled(
                "horizontal_denominator", horizontal_denominator, _EXECUTE_LIMIT
            ),
            "vertical_denominator": _scaled(
                "vertical_denominator", vertical_denominator, _EXECUTE_LIMIT
            ),
            "framing_type": int(framing_type),
            "input_areas": _scale_areas(input_areas),
            "output_areas": _scale_areas(output_areas),
        }
        self._session._check_usable("framing.execute")
        self._session._backend.execute_framing(self._session._id, payload)
        self._executed = True

    def update_area(
        self,
        area: int,
        group: int,
        *,
        x: int = 0,
        y: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None:
        """Nudge one already-applied area by a relative amount.

        Negative x moves left and negative y moves up; negative width or
        height shrinks. The vendor states this call "is enabled after
        ExecuteEframing has been successfully executed" and "alone cannot move
        or resize framing frames", so it is refused before an execute rather
        than sent for the camera to ignore.

        The vendor's parameters here are 16-bit, and the same x1024 scaling
        applies, so the usable range per argument is -32 to 31.
        """
        self._require_available("framing.update_area")
        if not self._executed:
            raise UnsupportedOperationError(
                "a framing area can only be updated after execute() has "
                "succeeded; the vendor documents this call as enabled only "
                "afterwards and unable to move a frame on its own",
                capability="framing.execute",
                operation="framing.update_area",
            )
        payload = {
            "area": _area_number(area),
            "group": _area_group(group),
            "x": _scaled("x", x, _UPDATE_LIMIT),
            "y": _scaled("y", y, _UPDATE_LIMIT),
            "width": _scaled("width", width, _UPDATE_LIMIT),
            "height": _scaled("height", height, _UPDATE_LIMIT),
        }
        self._session._check_usable("framing.update_area")
        self._session._backend.update_framing_area(self._session._id, payload)

    def _require_available(self, operation: str) -> None:
        if not self.available:
            raise UnsupportedOperationError(
                "this camera does not report electronic framing as executable",
                capability="framing",
                operation=operation,
            )

    def __repr__(self) -> str:
        if not self.available:
            return "Framing(unavailable)"
        shown = self.type_name or self.type
        return f"Framing(type={shown!r}, executed={self._executed})"


def _scale_areas(
    areas: Optional[dict[int, FramingRectangle]],
) -> dict[int, tuple[int, int, int, int]]:
    scaled: dict[int, tuple[int, int, int, int]] = {}
    for area, rectangle in (areas or {}).items():
        number = _area_number(area)
        scaled[number] = (
            _scaled("x", rectangle.x, _EXECUTE_LIMIT),
            _scaled("y", rectangle.y, _EXECUTE_LIMIT),
            _scaled("width", rectangle.width, _EXECUTE_LIMIT),
            _scaled("height", rectangle.height, _EXECUTE_LIMIT),
        )
    return scaled
