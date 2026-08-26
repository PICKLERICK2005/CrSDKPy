"""Lens identity and the lens's focus-distance table.

Two unrelated things share the word "lens" in the vendor SDK, and conflating
them is the mistake this module exists to avoid.

*Identity* -- model name, version, serial -- is three ordinary string-valued
properties. Nothing has to be requested; they are simply read.

*Focus-distance information* is a separate feature with its own three-call
protocol. The vendor documents it as valid only when a compatible lens is
attached and only while ``LensInformationEnableStatus`` reports Enable, its
outcome arriving as a warning rather than as a return value, and the read
being permitted exactly once per request. A lens that does not carry the data
is a lens limitation, not a body or API limitation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import (
    OperationTimeoutError,
    PropertyNotSupportedError,
    UnsupportedOperationError,
)
from .events import WarningEvent

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = [
    "LENS_INFORMATION_CHANGED",
    "LENS_REQUEST_BUSY",
    "LENS_REQUEST_FAILED",
    "LENS_REQUEST_SUCCEEDED",
    "Lens",
    "LensDistance",
    "LensInformationStatus",
]

LENS_MODEL_NAME = 0x0765
LENS_VERSION_NUMBER = 0x075F
LENS_SERIAL_NUMBER = 0x07CB
LENS_INFORMATION_ENABLE_STATUS = 0x0756

#: Vendor warning codes for the request's outcome. They arrive on the event
#: stream because the vendor reports the result there and nowhere else.
LENS_INFORMATION_CHANGED = 0x0002003A
LENS_REQUEST_SUCCEEDED = 0x0002003B
LENS_REQUEST_BUSY = 0x0002003C
LENS_REQUEST_FAILED = 0x0002003D

_REQUEST_OUTCOMES = frozenset(
    (LENS_REQUEST_SUCCEEDED, LENS_REQUEST_BUSY, LENS_REQUEST_FAILED)
)


@dataclass(frozen=True)
class LensInformationStatus:
    """Whether the camera will answer a focus-distance request right now.

    The unmodified camera value is kept: a body reporting something this
    release has not seen stays representable rather than being flattened into
    a boolean that would be a guess.
    """

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
class LensDistance:
    """One row of the lens's focus-distance table.

    :attr:`normalized_value` is comparable with the camera's follow-focus
    position properties; :attr:`focus_position` is the distance in the unit
    :attr:`unit` names. No scale is applied to either. The vendor's reference
    documents the fields and the unit selector but never a multiplier, and its
    own sample compares :attr:`focus_position` against a figure the operator
    types in centimetres -- an observation about that sample, not a documented
    contract, so nothing here converts anything.
    """

    info_type: int
    data_version: int
    normalized_value: int
    focus_position: int

    @property
    def unit(self) -> Optional[str]:
        """``"feet"``, ``"meter"``, or ``None`` for undefined/unrecognised."""
        return {1: "feet", 2: "meter"}.get(self.info_type)


class Lens:
    """The attached lens, as the camera describes it."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._distances: tuple[LensDistance, ...] = ()

    # -- identity ----------------------------------------------------------
    def _text(self, code: int) -> Optional[str]:
        try:
            value = self._session.properties.get(code).value
        except PropertyNotSupportedError:
            return None
        if not isinstance(value, str):
            return None
        return value or None

    @property
    def model_name(self) -> Optional[str]:
        """Lens model as a string, or ``None`` when the camera reports none."""
        return self._text(LENS_MODEL_NAME)

    @property
    def version(self) -> Optional[str]:
        """Lens firmware version. Blank on a lens that cannot report one."""
        return self._text(LENS_VERSION_NUMBER)

    @property
    def serial_number(self) -> Optional[str]:
        return self._text(LENS_SERIAL_NUMBER)

    @property
    def attached(self) -> bool:
        """Whether the camera names a lens.

        A body that reports no model name is the only generic signal there is;
        this is not a claim about what the lens can do.
        """
        return self.model_name is not None

    # -- focus-distance information ----------------------------------------
    @property
    def information_status(self) -> Optional[LensInformationStatus]:
        """The camera's own enable flag for focus-distance information."""
        try:
            value = self._session.properties.get(
                LENS_INFORMATION_ENABLE_STATUS
            ).value
        except PropertyNotSupportedError:
            return None
        return LensInformationStatus(int(value))

    @property
    def information_available(self) -> bool:
        """Whether the documented precondition for a request is met now.

        False also when the camera does not expose the status at all, since a
        request has no documented meaning in that case either.
        """
        status = self.information_status
        return bool(status is not None and status.enabled)

    @property
    def distances(self) -> tuple[LensDistance, ...]:
        """The table from the last successful read. Empty until one happens."""
        return self._distances

    def request_information(self) -> None:
        """Ask the camera to prepare the focus-distance table.

        Returns as soon as the request is accepted. The outcome arrives later
        as a :class:`~crsdkpy.WarningEvent` carrying one of
        :data:`LENS_REQUEST_SUCCEEDED`, :data:`LENS_REQUEST_BUSY` or
        :data:`LENS_REQUEST_FAILED`; use :meth:`load_information` to do the
        waiting. The precondition is not enforced here -- the camera's own
        answer is more authoritative than a property read taken a moment
        earlier.
        """
        self._session._check_usable("lens.request_information")
        self._session._backend.request_lens_information(self._session._id)

    def read_information(self) -> tuple[LensDistance, ...]:
        """Read the table a successful request prepared.

        The vendor permits one read per request, so this is not a poll: call
        it once after the success warning. Raises
        :class:`~crsdkpy.UnsupportedOperationError` when the camera reports no
        applicable information.
        """
        self._session._check_usable("lens.read_information")
        rows = self._session._backend.get_lens_information(self._session._id)
        self._distances = tuple(
            LensDistance(
                info_type=row.info_type,
                data_version=row.data_version,
                normalized_value=row.normalized_value,
                focus_position=row.focus_position,
            )
            for row in rows
        )
        return self._distances

    def load_information(
        self, *, timeout_ms: int = 5_000
    ) -> tuple[LensDistance, ...]:
        """Request the table, wait for the camera's verdict, then read it.

        The whole documented sequence in one call. Raises
        :class:`~crsdkpy.UnsupportedOperationError` when the camera refuses the
        request or reports no applicable information, and
        :class:`~crsdkpy.OperationTimeoutError` when no verdict arrives.
        """
        self.request_information()
        outcome = self._wait_for_outcome(timeout_ms)
        if outcome != LENS_REQUEST_SUCCEEDED:
            raise UnsupportedOperationError(
                "the camera refused the lens-information request "
                f"(vendor warning 0x{outcome:08X})",
                capability="lens.information",
                operation="lens.load_information",
            )
        return self.read_information()

    def _wait_for_outcome(self, timeout_ms: int) -> int:
        session = self._session
        clock = session._backend.clock
        deadline = clock.now_ms() + max(0, timeout_ms)
        while True:
            for event in session._take_op_events():
                if isinstance(event, WarningEvent) and event.code in _REQUEST_OUTCOMES:
                    return int(event.code)
            remaining = deadline - clock.now_ms()
            if remaining <= 0:
                raise OperationTimeoutError(
                    "the camera never reported the outcome of the "
                    "lens-information request",
                    operation="lens.load_information",
                    timeout_ms=timeout_ms,
                )
            session._pump(timeout_ms=max(1, min(50, remaining)))

    def __repr__(self) -> str:
        name = self.model_name
        return f"Lens({name!r})" if name else "Lens(no lens reported)"
