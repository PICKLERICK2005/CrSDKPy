"""Camera identity.

A :class:`Camera` is the persistent, logical device. It is deliberately *not*
one native connection: control mode cannot be changed on a live session, so
switching mode means closing one session and opening another, and the camera
object must survive that.

    Camera
      -> Session(remote)          close
      -> Session(remote_transfer) close
      -> Session(remote)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Union

from .capabilities import CameraCapabilities
from .enums import ReconnectPolicy, SessionMode, StillDestination
from .errors import UnsupportedOperationError
from .session import Session

if TYPE_CHECKING:  # pragma: no cover
    from .backend.contract import Backend, BackendCameraInfo

__all__ = ["Camera", "CameraInfo"]


@dataclass(frozen=True)
class CameraInfo:
    """Stable identity and metadata for a discovered camera."""

    model: str
    serial: Optional[str] = None
    firmware: Optional[str] = None
    transport: str = "unknown"
    adapter: Optional[str] = None
    usb_pid: Optional[int] = None
    #: Opaque backend handle. Never a native pointer.
    device_key: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        serial = f" {self.serial}" if self.serial else ""
        return f"{self.model}{serial} ({self.transport})"


class Camera:
    """A discovered camera, independent of any open session."""

    def __init__(self, backend: Backend, info: BackendCameraInfo) -> None:
        self._backend = backend
        self._info = CameraInfo(
            model=info.model,
            serial=info.serial,
            firmware=info.firmware,
            transport=info.transport,
            adapter=info.adapter,
            usb_pid=info.usb_pid,
            device_key=info.device_key,
            metadata=dict(info.metadata),
        )
        self._capabilities = info.capabilities
        self._sessions: list[Session] = []

    @property
    def info(self) -> CameraInfo:
        return self._info

    @property
    def model(self) -> str:
        return self._info.model

    @property
    def device_key(self) -> str:
        return self._info.device_key

    @property
    def capabilities(self) -> CameraCapabilities:
        """Broad device capabilities.

        What the body can do at all. A capability true here may still be
        unavailable in a given control mode; check
        :attr:`~crsdkpy.session.Session.capabilities` for that.
        """
        return self._capabilities

    @property
    def sessions(self) -> list[Session]:
        """Currently open sessions for this camera."""
        return [s for s in self._sessions if not s.closed]

    def supports_mode(self, mode: SessionMode) -> bool:
        return self._capabilities.supports_mode(mode)

    def open(
        self,
        mode: Union[SessionMode, str] = SessionMode.REMOTE,
        *,
        destination: Optional[StillDestination] = None,
        reconnect: ReconnectPolicy = ReconnectPolicy.BOUNDED,
    ) -> Session:
        """Open a session in *mode*.

        ``mode`` accepts a :class:`~crsdkpy.enums.SessionMode` or its string
        value. The mode is explicit because it decides which operations exist
        and cannot be changed once the session is open.

        ``reconnect`` decides who recovers a dropped link. The default leaves
        the vendor's reconnection monitor off, so this call either connects or
        fails promptly; the monitor keeps trying for five minutes, which would
        otherwise become the worst case for opening a session. Pass
        :attr:`~crsdkpy.enums.ReconnectPolicy.VENDOR` for a long-lived session
        that should survive a cable event on its own.
        """
        mode = _coerce_mode(mode)
        if not self.supports_mode(mode):
            supported = ", ".join(sorted(m.value for m in self._capabilities.modes))
            raise UnsupportedOperationError(
                f"{self.model} does not support control mode {mode.value!r}; "
                f"supported: {supported or 'none'}",
                capability=f"mode.{mode.value}",
                operation="camera.open",
            )
        if destination is not None and not self._capabilities.supports_destination(
            destination
        ):
            supported = ", ".join(
                sorted(d.value for d in self._capabilities.destinations)
            )
            raise UnsupportedOperationError(
                f"{self.model} does not support destination {destination.value!r}; "
                f"supported: {supported or 'none'}",
                capability=f"destination.{destination.value}",
                operation="camera.open",
            )

        session_id = self._backend.open_session(
            self._info.device_key, mode, destination, reconnect
        )
        session = Session(self, self._backend, session_id, mode)
        self._sessions.append(session)
        return session

    def close_sessions(self) -> None:
        for session in list(self._sessions):
            session.close()

    def _forget_session(self, session: Session) -> None:
        if session in self._sessions:
            self._sessions.remove(session)

    def __repr__(self) -> str:
        return f"Camera({self._info.model!r}, key={self._info.device_key!r})"


def _coerce_mode(mode: Any) -> SessionMode:
    if isinstance(mode, SessionMode):
        return mode
    if isinstance(mode, str):
        try:
            return SessionMode(mode)
        except ValueError:
            valid = ", ".join(m.value for m in SessionMode)
            raise ValueError(
                f"unknown control mode {mode!r}; expected one of: {valid}"
            ) from None
    raise TypeError(f"mode must be a SessionMode or str, got {type(mode).__name__}")
