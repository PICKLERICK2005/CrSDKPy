"""Interval (still) shooting.

The camera owns the interval sequence. The vendor exposes no start, stop or
step operation for it: arming is a property write, every parameter is a
property, and progress is a read-only status property the camera updates on
its own. So there is no scheduler here, and there should not be one -- a
host-side timer would be a second, competing implementation of something the
body already does.

Values stay in the camera's own units. The vendor documents the shooting
interval as ten times the value in seconds and gives the shot count as a plain
number; the start time it describes as seconds while also giving a maximum
that only holds at a tenth of a second, so that one is genuinely ambiguous and
is left exactly as reported. Ranges and permitted values come from the camera
at runtime, never from a table here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import PropertyNotSupportedError
from .settings import Setting

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["Interval", "IntervalStatus"]

INTERVAL_SHOOTING_START_TIME = 0x01FC
INTERVAL_SHOOTING_INTERVAL = 0x01FD
INTERVAL_SHOOT_INTERVAL_PRIORITY = 0x01FE
INTERVAL_NUMBER_OF_SHOTS = 0x01FF
INTERVAL_AE_TRACKING_SENSITIVITY = 0x0200
INTERVAL_SHUTTER_TYPE = 0x0201
INTERVAL_REC_MODE = 0x0505
INTERVAL_REC_STATUS = 0x0718

#: Documented CrIntervalRecMode values.
INTERVAL_MODE_OFF = 1
INTERVAL_MODE_ON = 2


@dataclass(frozen=True)
class IntervalStatus:
    """Where the camera is in its interval sequence.

    The camera's own number is kept, so a body reporting a state this release
    has not seen stays readable instead of being forced into a known one.
    """

    value: int

    @property
    def name(self) -> Optional[str]:
        return {1: "waiting_start", 2: "shooting"}.get(self.value)

    @property
    def shooting(self) -> Optional[bool]:
        """True/False for the documented values, otherwise ``None``."""
        if self.value == 1:
            return False
        if self.value == 2:
            return True
        return None

    @property
    def known(self) -> bool:
        return self.name is not None


class Interval:
    """Interval-shooting settings and the camera's own progress through them."""

    def __init__(self, session: Session) -> None:
        self._session = session
        #: Arms and disarms the sequence. The camera runs it, not CrSDKPy.
        self.mode = Setting(session, INTERVAL_REC_MODE)
        #: Delay before the first frame, in the camera's own units.
        self.start_time = Setting(session, INTERVAL_SHOOTING_START_TIME)
        #: Interval between frames. The vendor documents this as ten times the
        #: value in seconds; the number here is the camera's, unscaled.
        self.shooting_interval = Setting(session, INTERVAL_SHOOTING_INTERVAL)
        self.number_of_shots = Setting(session, INTERVAL_NUMBER_OF_SHOTS)
        self.interval_priority = Setting(session, INTERVAL_SHOOT_INTERVAL_PRIORITY)
        self.ae_tracking_sensitivity = Setting(
            session, INTERVAL_AE_TRACKING_SENSITIVITY
        )
        self.shutter_type = Setting(session, INTERVAL_SHUTTER_TYPE)

    @property
    def available(self) -> bool:
        """Whether this camera exposes interval shooting at all."""
        return self.mode.available

    @property
    def enabled(self) -> Optional[bool]:
        """Whether the sequence is armed, or ``None`` if the camera is silent
        or reports a mode value this release does not recognise."""
        value = self.mode.value
        if value == INTERVAL_MODE_ON:
            return True
        if value == INTERVAL_MODE_OFF:
            return False
        return None

    @property
    def status(self) -> Optional[IntervalStatus]:
        """The camera's progress through the sequence, read-only by design."""
        try:
            value = self._session.properties.get(INTERVAL_REC_STATUS).value
        except PropertyNotSupportedError:
            return None
        return IntervalStatus(int(value))

    @property
    def shooting(self) -> Optional[bool]:
        status = self.status
        return status.shooting if status is not None else None

    def enable(self) -> None:
        """Arm the sequence. The camera decides when frames happen."""
        self.mode.set(INTERVAL_MODE_ON)

    def disable(self) -> None:
        """Disarm the sequence."""
        self.mode.set(INTERVAL_MODE_OFF)

    def __repr__(self) -> str:
        if not self.available:
            return "Interval(unavailable)"
        status = self.status
        name = status.name if status is not None else None
        return f"Interval(enabled={self.enabled!r}, status={name or status!r})"
