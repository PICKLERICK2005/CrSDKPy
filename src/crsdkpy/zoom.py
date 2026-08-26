"""Zoom controls.

Every documented zoom control is a device property; the vendor exposes no
zoom-specific operation, so nothing here needs a native call. What it does
need is the vendor's preconditions, which are real: driving the zoom is valid
only while the camera reports ``Zoom_Operation_Status`` as Enable, and the
speed a drive accepts comes from ``Zoom_Speed_Range`` rather than from a
constant.

Support is a runtime question and is answered by the camera, never by a model
name. A body can expose this whole family with a lens attached that cannot
motorise anything, which is exactly what the reference body did.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import PropertyNotSupportedError, UnsupportedOperationError
from .properties import PropertyRange
from .settings import Setting

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = [
    "Zoom",
    "ZoomBarInformation",
    "ZoomDrivingStatus",
    "ZoomOperationStatus",
    "ZoomTypeStatus",
]

ZOOM_SCALE = 0x0124
ZOOM_SETTING = 0x0125
ZOOM_OPERATION = 0x0126
ZOOM_AND_FOCUS_POSITION_SAVE = 0x0134
ZOOM_AND_FOCUS_POSITION_LOAD = 0x0135
DIGITAL_ZOOM_SCALE = 0x014C
ZOOM_DISTANCE = 0x014D
ZOOM_DISTANCE_UNIT_SETTING = 0x014E
ZOOM_PRESET_ZOOM_ONLY_SET = 0x027C
ZOOM_POSITION_SETTING = 0x02BC
REMOCON_ZOOM_SPEED_TYPE = 0x050C
ZOOM_OPERATION_STATUS = 0x071E
ZOOM_BAR_INFORMATION = 0x071F
ZOOM_TYPE_STATUS = 0x0720
ZOOM_SPEED_RANGE = 0x0724
ZOOM_DRIVING_STATUS = 0x0792
ZOOM_PRESET_DATA_VERSION = 0x079A
ZOOM_POSITION_CURRENT_VALUE = 0x07A1

#: Documented drive values. The range is variable on bodies that advertise a
#: speed range, so these are the floor rather than the whole story.
ZOOM_WIDE = -1
ZOOM_STOP = 0
ZOOM_TELE = 1


@dataclass(frozen=True)
class ZoomOperationStatus:
    """Whether the camera will accept a zoom drive right now."""

    value: int

    @property
    def name(self) -> Optional[str]:
        return {0: "disable", 1: "enable"}.get(self.value)

    @property
    def enabled(self) -> Optional[bool]:
        """True/False for the documented values, otherwise ``None``."""
        if self.value == 0:
            return False
        if self.value == 1:
            return True
        return None

    @property
    def known(self) -> bool:
        return self.name is not None


@dataclass(frozen=True)
class ZoomTypeStatus:
    """Which kind of zoom the camera is currently applying."""

    value: int

    @property
    def name(self) -> Optional[str]:
        return {
            1: "optical",
            2: "smart",
            3: "clear_image",
            4: "digital",
        }.get(self.value)

    @property
    def known(self) -> bool:
        return self.name is not None

    @property
    def is_optical(self) -> Optional[bool]:
        """Whether the active zoom is purely optical, or ``None`` if unknown."""
        if not self.known:
            return None
        return self.value == 1


@dataclass(frozen=True)
class ZoomDrivingStatus:
    """Zoom-motion state, with the unmodified camera value retained.

    The vendor notes this reflects only the motion caused by an absolute
    position setting or a preset load -- not a speed-driven zoom -- so a
    ``False`` here does not mean the lens is stationary.
    """

    value: int

    @property
    def name(self) -> Optional[str]:
        return {1: "idle", 2: "driving"}.get(self.value)

    @property
    def is_driving(self) -> Optional[bool]:
        if self.value == 1:
            return False
        if self.value == 2:
            return True
        return None

    @property
    def known(self) -> bool:
        return self.name is not None


@dataclass(frozen=True)
class ZoomBarInformation:
    """The zoom bar, split the way the vendor documents the word.

    One 32-bit value carrying three fields: the total number of boxes in bits
    31-24, the current box in bits 23-16, and the position within that box in
    bits 15-0. This is a documented decomposition of one number, not a unit
    conversion, and :attr:`value` keeps the number itself either way.
    """

    value: int

    @property
    def total_boxes(self) -> int:
        return (self.value >> 24) & 0xFF

    @property
    def current_box(self) -> int:
        return (self.value >> 16) & 0xFF

    @property
    def position_in_box(self) -> int:
        return self.value & 0xFFFF


class Zoom:
    """Zoom state and control, as the attached camera and lens report it."""

    def __init__(self, session: Session) -> None:
        self._session = session
        #: What kind of zoom the camera applies to a drive request.
        self.setting = Setting(session, ZOOM_SETTING)
        #: Combined optical and digital scale, in the camera's own units.
        self.scale = Setting(session, ZOOM_SCALE)
        self.digital_scale = Setting(session, DIGITAL_ZOOM_SCALE)
        #: Focal length, when the camera reports one.
        self.distance = Setting(session, ZOOM_DISTANCE)
        self.distance_unit = Setting(session, ZOOM_DISTANCE_UNIT_SETTING)
        #: Absolute target position. Read back through :attr:`position`.
        self.position_setting = Setting(session, ZOOM_POSITION_SETTING)
        #: Whether the body takes a fixed or variable drive speed.
        self.speed_type = Setting(session, REMOCON_ZOOM_SPEED_TYPE)
        #: Preset slots for the saved zoom-and-focus positions.
        self.save_preset = Setting(session, ZOOM_AND_FOCUS_POSITION_SAVE)
        self.load_preset = Setting(session, ZOOM_AND_FOCUS_POSITION_LOAD)
        self.preset_zoom_only = Setting(session, ZOOM_PRESET_ZOOM_ONLY_SET)
        self.preset_data_version = Setting(session, ZOOM_PRESET_DATA_VERSION)

    # -- runtime state -----------------------------------------------------
    def _value(self, code: int) -> Optional[int]:
        try:
            return int(self._session.properties.get(code).value)
        except PropertyNotSupportedError:
            return None

    @property
    def available(self) -> bool:
        """Whether the camera exposes the zoom family at all.

        Exposure is not the same as a lens that can move: the reference body
        reported this whole family with a prime attached.
        """
        return self._value(ZOOM_OPERATION_STATUS) is not None

    @property
    def operation_status(self) -> Optional[ZoomOperationStatus]:
        """The camera's own gate on driving the zoom."""
        value = self._value(ZOOM_OPERATION_STATUS)
        return None if value is None else ZoomOperationStatus(value)

    @property
    def operable(self) -> bool:
        """Whether a drive would meet its documented precondition right now."""
        status = self.operation_status
        return bool(status is not None and status.enabled)

    @property
    def type_status(self) -> Optional[ZoomTypeStatus]:
        value = self._value(ZOOM_TYPE_STATUS)
        return None if value is None else ZoomTypeStatus(value)

    @property
    def driving_status(self) -> Optional[ZoomDrivingStatus]:
        value = self._value(ZOOM_DRIVING_STATUS)
        return None if value is None else ZoomDrivingStatus(value)

    @property
    def is_driving(self) -> Optional[bool]:
        status = self.driving_status
        return status.is_driving if status is not None else None

    @property
    def bar_information(self) -> Optional[ZoomBarInformation]:
        value = self._value(ZOOM_BAR_INFORMATION)
        return None if value is None else ZoomBarInformation(value)

    @property
    def position(self) -> Optional[int]:
        """Absolute position the camera currently reports, in its own units."""
        return self._value(ZOOM_POSITION_CURRENT_VALUE)

    @property
    def position_range(self) -> Optional[PropertyRange]:
        """Advertised absolute range, or ``None`` when none is reported."""
        return self.position_setting.value_range

    @property
    def speed_range(self) -> Optional[PropertyRange]:
        """Drive speeds this body accepts.

        The vendor documents this as meaningful only while the remote zoom
        speed type is Variable; a body reporting no range drives at the fixed
        :data:`ZOOM_WIDE` / :data:`ZOOM_STOP` / :data:`ZOOM_TELE` values.
        """
        try:
            return self._session.properties.get(ZOOM_SPEED_RANGE).value_range
        except PropertyNotSupportedError:
            return None

    # -- control -----------------------------------------------------------
    def operate(self, speed: int) -> None:
        """Drive the zoom at *speed*, or stop it with zero.

        Positive is tele, negative is wide, and the magnitude is the speed.
        The vendor documents this as executable only while the zoom operation
        status is Enable, and the vendor's own sample refuses the write
        otherwise, so this does too rather than letting the camera silently
        ignore it. What counts as a valid speed comes from the camera's
        advertised range.
        """
        if isinstance(speed, bool) or not isinstance(speed, int):
            raise TypeError("zoom speed must be an integer")
        setting = Setting(self._session, ZOOM_OPERATION)
        if not setting.available:
            raise UnsupportedOperationError(
                "this camera does not expose the zoom operation",
                capability="zoom.operation",
                operation="zoom.operate",
            )
        status = self.operation_status
        if not (status is not None and status.enabled):
            reported = "not reported" if status is None else repr(status.value)
            raise UnsupportedOperationError(
                "the camera does not currently allow a zoom operation "
                f"(operation status {reported})",
                capability="zoom.operation.enabled",
                operation="zoom.operate",
            )
        setting.set(speed)

    def stop(self) -> None:
        """Stop a drive already in progress."""
        self.operate(ZOOM_STOP)

    def move_to(self, position: int) -> None:
        """Begin an absolute move, without waiting for it to finish.

        Progress shows up on :attr:`driving_status`, which the vendor
        documents as reflecting exactly this kind of move.
        """
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError("zoom position must be an integer camera/lens unit")
        if not self.position_setting.available:
            raise UnsupportedOperationError(
                "this camera/lens does not expose absolute zoom positioning",
                capability="zoom.position",
                operation="zoom.move_to",
            )
        self.position_setting.set(position)

    def __repr__(self) -> str:
        if not self.available:
            return "Zoom(unavailable)"
        return f"Zoom(operable={self.operable}, position={self.position!r})"
