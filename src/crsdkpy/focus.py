"""Generic absolute focus-position controls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import (
    CrSDKPyError,
    OperationTimeoutError,
    PropertyNotSupportedError,
    UnsupportedOperationError,
)
from .properties import Property, PropertyRange

if TYPE_CHECKING:  # pragma: no cover
    from .capture import Capture
    from .session import Session

__all__ = [
    "Focus",
    "FocusBracketError",
    "FocusBracketFrame",
    "FocusBracketResult",
    "FocusDrivingStatus",
]

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


@dataclass(frozen=True)
class FocusBracketFrame:
    """One commanded position and its single capture attempt."""

    commanded_position: int
    settled_position: int
    capture: Capture

    @property
    def exposed(self) -> bool:
        return self.capture.exposed

    @property
    def identity_resolved(self) -> bool:
        return self.capture.content is not None


@dataclass(frozen=True)
class FocusBracketResult:
    """Completed frames plus explicit optional-restoration state."""

    starting_position: int
    frames: tuple[FocusBracketFrame, ...]
    restoration_requested: bool = False
    restored_position: Optional[int] = None
    failure: Optional[Exception] = None
    restoration_failure: Optional[Exception] = None

    @property
    def successful(self) -> bool:
        return self.failure is None and self.restoration_failure is None


class FocusBracketError(CrSDKPyError):
    """A bracket stopped early or failed to restore its starting position."""

    def __init__(self, message: str, *, result: FocusBracketResult) -> None:
        super().__init__(message, operation="focus.capture_bracket")
        self.result = result


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

    def _setting(self) -> Property:
        try:
            return self._get(FOCUS_POSITION_SETTING)
        except PropertyNotSupportedError as exc:
            raise UnsupportedOperationError(
                "this camera/lens does not expose absolute focus positioning",
                capability="focus.position",
                operation="focus.move_to",
            ) from exc

    def _validate_position(self, position: int) -> None:
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError("focus position must be an integer camera/lens unit")
        setting = self._setting()
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
        self._validate_position(position)
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

    def capture_bracket(
        self,
        positions: Sequence[int],
        *,
        autofocus: bool = False,
        restore_start: bool = False,
        settle_timeout_ms: int = 3_000,
        exposure_timeout_ms: int = 10_000,
        content_timeout_ms: int = 10_000,
    ) -> FocusBracketResult:
        """Acquire one still at each caller-supplied absolute position.

        This only orchestrates existing focus and capture APIs; it performs no
        stacking or image processing. All positions are validated before the
        first move. Each position gets exactly one shutter attempt. A missing
        exposure or unresolved content identity is retained on that frame and
        is never retried automatically.

        Movement, session, or optional restoration failures raise
        :class:`FocusBracketError`; its ``result`` contains every completed
        frame and both failure fields.
        """
        requested = tuple(positions)
        if not requested:
            raise ValueError("focus bracket requires at least one position")
        for position in requested:
            self._validate_position(position)

        starting = self.position
        frames: list[FocusBracketFrame] = []
        failure: Optional[Exception] = None
        restoration_failure: Optional[Exception] = None
        restored_position: Optional[int] = None

        try:
            for commanded in requested:
                self.move_to(commanded)
                settled = self.wait_until_settled(
                    target=commanded, timeout_ms=settle_timeout_ms
                )
                capture = (
                    self._session.autofocus_and_capture(
                        timeout_ms=exposure_timeout_ms
                    )
                    if autofocus
                    else self._session.capture(timeout_ms=exposure_timeout_ms)
                )
                if capture.exposed and capture.content is None:
                    try:
                        capture.wait_for_content(timeout_ms=content_timeout_ms)
                    except (OperationTimeoutError, UnsupportedOperationError):
                        # Exposure is final. Preserve unresolved identity and
                        # never fire a second shutter for this frame.
                        pass
                frames.append(FocusBracketFrame(commanded, settled, capture))
        except Exception as exc:
            failure = exc
        finally:
            if restore_start:
                try:
                    self.move_to(starting)
                    restored_position = self.wait_until_settled(
                        target=starting, timeout_ms=settle_timeout_ms
                    )
                except Exception as exc:
                    restoration_failure = exc

        result = FocusBracketResult(
            starting_position=starting,
            frames=tuple(frames),
            restoration_requested=restore_start,
            restored_position=restored_position,
            failure=failure,
            restoration_failure=restoration_failure,
        )
        if failure is not None or restoration_failure is not None:
            cause = failure or restoration_failure
            raise FocusBracketError(
                "focus bracket stopped before completing cleanly", result=result
            ) from cause
        return result
