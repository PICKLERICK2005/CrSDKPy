"""Raw low-level access.

The vendor SDK gains features faster than any high-level wrapper can model
them, and hardware already reports property codes the vendor's own enumeration
does not name. This layer keeps those reachable.

It is deliberately *below* the ergonomic API, not a replacement for it:

* unknown numeric codes are allowed;
* session state and lifetime checks still apply;
* no native pointers or vendor objects are ever exposed;
* nothing here bypasses the backend contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Union

from .commands import Command, CommandLike, CommandParameter
from .properties import Property, PropertyCode

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["RawAccess"]


class RawAccess:
    """Escape hatch bound to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- properties --------------------------------------------------------
    def get_property(self, code: Union[int, PropertyCode]) -> Property:
        """Read any property by numeric code, named or not."""
        session = self._session
        session._check_usable("raw.get_property")
        return session._backend.get_property(session._id, PropertyCode(code))

    def set_property(self, code: Union[int, PropertyCode], value: Any) -> None:
        session = self._session
        session._check_usable("raw.set_property")
        session._backend.set_property(session._id, PropertyCode(code), value)

    def property_codes(self) -> Sequence[PropertyCode]:
        session = self._session
        session._check_usable("raw.property_codes")
        return tuple(p.code for p in session._backend.list_properties(session._id))

    # -- commands ----------------------------------------------------------
    def send_command(
        self,
        command: CommandLike,
        parameter: Union[CommandParameter, int] = CommandParameter.DOWN,
    ) -> None:
        """Send a named or raw numeric command.

        Acceptance here proves only that the command was delivered. It is not
        evidence that the camera acted on it.
        """
        session = self._session
        session._check_usable("raw.send_command")
        if isinstance(parameter, int) and not isinstance(parameter, CommandParameter):
            parameter = CommandParameter(parameter)
        session._backend.send_command(session._id, command, parameter)

    def press(self, command: CommandLike, *, dwell_ms: int = 35) -> None:
        """Send ``DOWN``, wait, then ``UP``.

        A ``DOWN`` must always be followed by an ``UP``; this helper makes that
        hard to get wrong.
        """
        session = self._session
        session._check_usable("raw.press")
        self.send_command(command, CommandParameter.DOWN)
        session._backend.clock.sleep_ms(dwell_ms)
        self.send_command(command, CommandParameter.UP)

    def s1_and_release(self, *, dwell_ms: int = 35) -> None:
        """Fire the vendor's combined half-press-and-release command.

        Exposed here rather than as a capture method on purpose: it is
        **ungated**. Autofocus runs, but the exposure is committed before any
        code can inspect the focus result and decline. Use
        :meth:`~crsdkpy.Session.autofocus_and_capture` when focus failure must
        be handled.
        """
        self.press(Command.S1_AND_RELEASE, dwell_ms=dwell_ms)

    # -- shutter stages ----------------------------------------------------
    @property
    def half_press(self) -> bool:
        session = self._session
        session._check_usable("raw.half_press")
        return session._backend.get_half_press(session._id)

    def set_half_press(self, engaged: bool) -> None:
        session = self._session
        session._check_usable("raw.set_half_press")
        session._backend.set_half_press(session._id, engaged)

    # -- extension point ---------------------------------------------------
    def call(self, operation: str, **payload: Any) -> Any:
        """Invoke a backend operation CrSDKPy has no typed wrapper for.

        Intended for vendor feature families that are neither property- nor
        command-shaped, such as lens information, zoom or focus presets.
        """
        session = self._session
        session._check_usable(f"raw.call.{operation}")
        return session._backend.raw_call(session._id, operation, payload)

    def __repr__(self) -> str:
        return f"RawAccess(session={self._session._id!r})"
