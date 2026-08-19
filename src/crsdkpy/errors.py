"""Exception hierarchy for CrSDKPy.

Every error carries the operation context and, where a backend produced it, the
raw backend code alongside a normalized meaning. Callers should be able to
branch on the normalized type without parsing vendor codes.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "AutofocusFailedError",
    "BackendError",
    "BackendUnavailableError",
    "CameraBusyError",
    "CameraConnectionError",
    "CrSDKPyError",
    "InvalidSessionStateError",
    "NativeBackendError",
    "OperationTimeoutError",
    "PropertyNotSupportedError",
    "SDKNotFoundError",
    "SDKNotStartedError",
    "SessionClosedError",
    "UnsupportedOperationError",
]


class CrSDKPyError(Exception):
    """Base class for every error raised by CrSDKPy."""

    def __init__(
        self,
        message: str,
        *,
        operation: Optional[str] = None,
        backend_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.operation = operation
        self.backend_code = backend_code

    def __str__(self) -> str:
        parts = [self.message]
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.backend_code is not None:
            parts.append(f"backend_code=0x{self.backend_code:X}")
        if len(parts) == 1:
            return parts[0]
        return f"{parts[0]} ({', '.join(parts[1:])})"


class BackendUnavailableError(CrSDKPyError):
    """The requested backend cannot be used in this environment."""


class SDKNotFoundError(BackendUnavailableError):
    """The vendor SDK runtime could not be located."""


class NativeBackendError(BackendUnavailableError):
    """The native backend exists but cannot be used yet."""


class SDKNotStartedError(CrSDKPyError):
    """An operation required a started :class:`~crsdkpy.SDK`."""


class CameraConnectionError(CrSDKPyError):
    """Opening or maintaining a camera connection failed."""


class SessionClosedError(CrSDKPyError):
    """The session has been closed and can no longer be used."""


class InvalidSessionStateError(CrSDKPyError):
    """The session is in a state that does not permit this operation."""

    def __init__(
        self,
        message: str,
        *,
        state: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.state = state


class UnsupportedOperationError(CrSDKPyError):
    """The camera or session does not support this operation.

    Raised from capability checks, so that an unsupported feature reports the
    missing capability rather than a vendor error code.
    """

    def __init__(
        self,
        message: str,
        *,
        capability: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.capability = capability


class PropertyNotSupportedError(CrSDKPyError):
    """The camera does not expose the requested property code."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message, operation="property")
        self.code = code


class OperationTimeoutError(CrSDKPyError):
    """A bounded wait elapsed before the expected result arrived."""

    def __init__(
        self,
        message: str,
        *,
        operation: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.timeout_ms = timeout_ms


class AutofocusFailedError(CrSDKPyError):
    """Autofocus did not reach an accepted focused state.

    This is distinct from a capture failure: no exposure was requested, so
    nothing was committed on the camera.
    """

    def __init__(
        self,
        message: str,
        *,
        focus_state: Any = None,
        elapsed_ms: Optional[int] = None,
    ) -> None:
        super().__init__(message, operation="autofocus")
        self.focus_state = focus_state
        self.elapsed_ms = elapsed_ms


class CameraBusyError(CrSDKPyError):
    """The camera rejected the operation because it is busy."""


class BackendError(CrSDKPyError):
    """A backend reported a failure that has no more specific mapping."""
