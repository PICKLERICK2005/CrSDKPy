"""Generic absolute focus-position controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import (
    OperationTimeoutError,
    PropertyNotSupportedError,
    UnsupportedOperationError,
)
from .properties import Property, PropertyRange

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["Focus", "FocusDrivingStatus"]

FOCUS_POSITION_SETTING = 0x020E
FOCUS_POSITION_CURRENT = 0x0766
FOCUS_DRIVING_STATUS = 0x0767


@dataclass(frozen=True)
class FocusDrivingStatus:
    """Focus-motion state with the unmodified camera value retained."""

    value: int

    @property
    def name(self) -> Optional[str]:
        return {1: "idle", 2: "driving"}.get(self.value)

    @property
    def is_driving(self) -> Optional[bool]:
        """True/False for documented values, otherwise ``None``."""
        if self.value == 1:
            return False
        if self.value == 2:
            return True
        return None

    @property
    def known(self) -> bool:
        return self.name is not None


class Focus:
    """Absolute focus positioning in opaque camera/lens units.

    Values are not distances, diopters, millimetres, or normalized physical
    positions. Their only generic meaning is the range and step reported by
    the current camera/lens combination.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._pending_target: Optional[int] = None

    def _get(self, code: int) -> Property:
        return self._session.properties.get(code)

    @property
    def available(self) -> bool:
        try:
            self._get(FOCUS_POSITION_CURRENT)
            self._get(FOCUS_POSITION_SETTING)
        except PropertyNotSupportedError:
            return False
        return True

    @property
    def position(self) -> int:
        """Current opaque position reported by the camera."""
        try:
            return int(self._get(FOCUS_POSITION_CURRENT).value)
        except PropertyNotSupportedError as exc:
            raise UnsupportedOperationError(
                "this camera/lens does not report a focus position",
                capability="focus.position",
                operation="focus.position",
            ) from exc

    @property
    def range(self) -> Optional[PropertyRange]:
        """Advertised absolute range, or ``None`` when none is reported."""
        try:
            return self._get(FOCUS_POSITION_SETTING).value_range
        except PropertyNotSupportedError:
            return None

    @property
    def driving_status(self) -> Optional[FocusDrivingStatus]:
        """Current motion state, retaining unknown vendor values."""
        try:
            return FocusDrivingStatus(int(self._get(FOCUS_DRIVING_STATUS).value))
        except PropertyNotSupportedError:
            return None

    @property
    def is_driving(self) -> Optional[bool]:
        """Motion flag, or ``None`` if absent or reported with a new value."""
        status = self.driving_status
        return status.is_driving if status is not None else None

    def move_to(self, position: int) -> None:
        """Begin an absolute move without waiting for physical completion."""
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError("focus position must be an integer camera/lens unit")
        try:
            setting = self._get(FOCUS_POSITION_SETTING)
        except PropertyNotSupportedError as exc:
            raise UnsupportedOperationError(
                "this camera/lens does not expose absolute focus positioning",
                capability="focus.position",
                operation="focus.move_to",
            ) from exc
        if not setting.writable:
            raise UnsupportedOperationError(
                "the focus-position setting is not writable",
                capability="focus.position.write",
                operation="focus.move_to",
            )
        if not setting.accepts(position):
            raise ValueError(
                f"focus position {position} is outside the advertised constraints"
            )
        self._session.properties.set(FOCUS_POSITION_SETTING, position)
        self._pending_target = position

    def move_relative(self, delta: int) -> None:
        """Move to ``current absolute position + delta`` opaque units."""
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise TypeError("focus delta must be an integer camera/lens unit")
        self.move_to(self.position + delta)

    def wait_until_settled(
        self,
        *,
        timeout_ms: int = 3_000,
        target: Optional[int] = None,
        poll_interval_ms: int = 50,
    ) -> int:
        """Wait for idle plus optional exact target readback, then return position.

        When *target* is omitted, the last target passed to :meth:`move_to` is
        used. Property events wake the wait; bounded reads cover cameras that
        omit or coalesce those events. No fixed physical settling delay is
        assumed.
        """
        expected = self._pending_target if target is None else target
        clock = self._session._backend.clock
        timeout_ms = max(0, int(timeout_ms))
        deadline = clock.now_ms() + timeout_ms
        interval = max(1, int(poll_interval_ms))

        while True:
            position = self.position
            status = self.driving_status
            at_target = expected is None or position == expected
            if status is None:
                if at_target:
                    self._pending_target = None
                    return position
            elif status.is_driving is False and at_target:
                self._pending_target = None
                return position

            remaining = deadline - clock.now_ms()
            if remaining <= 0:
                raise OperationTimeoutError(
                    "focus did not settle before the deadline",
                    operation="focus.wait_until_settled",
                    timeout_ms=timeout_ms,
                )
            self._session._pump(timeout_ms=min(interval, remaining))
