"""The backend contract.

Everything above this line is generic Python; everything below it is a driver.
The contract is deliberately shaped so a native bridge can satisfy it without
redesign:

* sessions are referred to by **opaque string ids**, never by pointers;
* every value crossing the boundary is plain data (``dataclass``, ``bytes``,
  ``int``, ``str``) that a C ABI layer can construct;
* events are **pulled from a queue** rather than pushed through callbacks, so a
  native implementation can keep vendor callbacks on its own threads and hand
  Python a drained batch;
* image bytes are **copied out** by the backend, so no caller-owned buffer or
  vendor-owned memory is ever exposed;
* :meth:`Backend.close_session` is idempotent;
* the backend owns the clock, so a virtual clock can make waits deterministic.

The two known implementations are the simulator and, later, the native bridge.
No simulator concept may leak upward into the public API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from ..capabilities import CameraCapabilities, SessionCapabilities
from ..clock import Clock
from ..commands import CommandLike, CommandParameter
from ..enums import (
    ConnectionState,
    FocusState,
    PreviewKind,
    RecordingState,
    SessionMode,
    StillDestination,
)
from ..events import Event
from ..previews import LiveViewFrame, Preview
from ..properties import Property, PropertyCode
from ..status import BatteryStatus, StorageSlot

__all__ = [
    "Backend",
    "BackendCameraInfo",
    "ContentRef",
    "LiveViewInfo",
]


@dataclass(frozen=True)
class BackendCameraInfo:
    """Identity of a discovered camera.

    ``device_key`` is the stable handle the backend uses to reopen this camera.
    It must survive session close/reopen and control-mode changes, because the
    public :class:`~crsdkpy.Camera` object outlives any single session.
    """

    device_key: str
    model: str
    serial: Optional[str] = None
    firmware: Optional[str] = None
    transport: str = "unknown"
    adapter: Optional[str] = None
    usb_pid: Optional[int] = None
    capabilities: CameraCapabilities = field(default_factory=CameraCapabilities)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContentRef:
    """A durable item on the camera's media.

    Identifiers are monotonic but **not** contiguous; hardware has been
    observed skipping one. Detect new content with ``id > baseline``, never
    ``baseline + 1``.

    ``content_id`` alone does not identify a file: one capture can produce
    several (RAW plus JPEG), so a preview request needs ``file_id`` too.
    """

    content_id: int
    file_id: int = 0
    file_number: Optional[int] = None
    path: Optional[str] = None
    #: Backend clock reading when the item was observed. Same domain as event
    #: timestamps, so latencies computed against a capture are meaningful.
    created_ms: int = 0
    #: Creation time as the camera reported it, ISO-8601 without a zone. The
    #: camera states no timezone, so none is invented here.
    captured_at: Optional[str] = None
    #: Vendor content and file-format codes, preserved as reported.
    content_type: int = 0
    file_format: int = 0
    #: Geometry of the *original* still, not of any preview derived from it.
    width: Optional[int] = None
    height: Optional[int] = None
    file_size: Optional[int] = None
    slot: int = 1
    #: Files under this content id. Greater than one for RAW+JPEG captures.
    file_count: int = 1

    @property
    def filename(self) -> Optional[str]:
        """Trailing filename component of :attr:`path`."""
        if not self.path:
            return None
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class LiveViewInfo:
    """What the backend reports about the live-view stream.

    ``info_ok`` and ``usable`` are separate on purpose: a vendor info call can
    succeed while reporting a zero buffer and the frame fetch then fails hard.
    Reporting success is not the same as being able to deliver a frame.
    """

    info_ok: bool = False
    width: Optional[int] = None
    height: Optional[int] = None
    buffer_size: int = 0
    error_code: Optional[int] = None

    @property
    def usable(self) -> bool:
        return self.info_ok and self.buffer_size > 0


class Backend:
    """Interface every CrSDKPy driver implements.

    Subclasses raise :class:`~crsdkpy.errors.UnsupportedOperationError` for
    capabilities they do not provide, rather than returning a falsy value.
    """

    #: Short identifier used in diagnostics, e.g. ``"simulator"``.
    name: str = "backend"

    @property
    def clock(self) -> Clock:  # pragma: no cover - interface
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def shutdown(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def enumerate_cameras(self) -> Sequence[BackendCameraInfo]:  # pragma: no cover
        raise NotImplementedError

    # -- sessions ----------------------------------------------------------
    def open_session(
        self,
        device_key: str,
        mode: SessionMode,
        destination: Optional[StillDestination] = None,
    ) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def close_session(self, session_id: str) -> None:  # pragma: no cover
        """Close a session. Must be safe to call more than once."""
        raise NotImplementedError

    def connection_state(self, session_id: str) -> ConnectionState:  # pragma: no cover
        raise NotImplementedError

    def session_capabilities(
        self, session_id: str
    ) -> SessionCapabilities:  # pragma: no cover
        raise NotImplementedError

    # -- events ------------------------------------------------------------
    def poll_events(
        self, session_id: str, timeout_ms: int = 0
    ) -> Sequence[Event]:  # pragma: no cover
        """Drain pending events, waiting up to *timeout_ms* for the first.

        Returning an empty sequence means nothing happened, not that anything
        failed. Implementations must never block past the timeout.
        """
        raise NotImplementedError

    # -- properties --------------------------------------------------------
    def list_properties(  # pragma: no cover
        self, session_id: str
    ) -> Sequence[Property]:
        raise NotImplementedError

    def get_property(
        self, session_id: str, code: PropertyCode
    ) -> Property:  # pragma: no cover
        raise NotImplementedError

    def set_property(
        self, session_id: str, code: PropertyCode, value: Any
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- shutter stages and focus -----------------------------------------
    # These are semantic rather than code-based so the public layer never
    # needs to know a vendor property code or value encoding.

    def get_half_press(self, session_id: str) -> bool:  # pragma: no cover
        """Whether the half-press (autofocus) stage is currently engaged."""
        raise NotImplementedError

    def set_half_press(
        self, session_id: str, engaged: bool
    ) -> None:  # pragma: no cover
        """Engage or release the half-press stage.

        Engaging starts autofocus. Releasing must be safe to call when the
        stage is already released.
        """
        raise NotImplementedError

    def focus_state(self, session_id: str) -> FocusState:  # pragma: no cover
        """Read the current focus state directly.

        Required as a fallback: when the indication is already at a focused
        value, no change notification may ever fire, and a purely
        notification-driven wait would time out on a good focus.
        """
        raise NotImplementedError

    # -- commands ----------------------------------------------------------
    def send_command(
        self,
        session_id: str,
        command: CommandLike,
        parameter: CommandParameter,
    ) -> None:  # pragma: no cover
        """Send a command. Acceptance here never implies the camera acted."""
        raise NotImplementedError

    # -- device status -----------------------------------------------------
    def battery(self, session_id: str) -> BatteryStatus:  # pragma: no cover
        """Charge state. A camera reporting nothing returns empty fields."""
        raise NotImplementedError

    def storage(self, session_id: str) -> Sequence[StorageSlot]:  # pragma: no cover
        """Media slots the camera reports. May be empty."""
        raise NotImplementedError

    # -- destination -------------------------------------------------------
    def get_destination(self, session_id: str) -> StillDestination:  # pragma: no cover
        raise NotImplementedError

    def set_destination(
        self, session_id: str, destination: StillDestination
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- live view ---------------------------------------------------------
    def live_view_info(self, session_id: str) -> LiveViewInfo:  # pragma: no cover
        raise NotImplementedError

    def get_live_view_frame(
        self, session_id: str
    ) -> Optional[LiveViewFrame]:  # pragma: no cover
        """Return the next frame, ``None`` if no new frame is ready.

        Raises when the fetch itself fails, which is distinct from there being
        nothing new to return.
        """
        raise NotImplementedError

    # -- previews and content ---------------------------------------------
    def configure_postview(
        self, session_id: str, *, enabled: bool, transfer_to_ram: bool = True
    ) -> None:  # pragma: no cover
        """Configure postview.

        May be rejected on cameras that still deliver postview anyway;
        configuration support and delivery support are separate capabilities.
        """
        raise NotImplementedError

    def pull_postview(self, session_id: str) -> Optional[Preview]:  # pragma: no cover
        raise NotImplementedError

    def latest_content(  # pragma: no cover
        self, session_id: str
    ) -> Optional[ContentRef]:
        raise NotImplementedError

    def list_content(
        self, session_id: str, *, newer_than: Optional[int] = None
    ) -> Sequence[ContentRef]:  # pragma: no cover
        raise NotImplementedError

    def get_preview(
        self, session_id: str, content_id: int, kind: PreviewKind
    ) -> Preview:  # pragma: no cover
        raise NotImplementedError

    # -- video -------------------------------------------------------------
    def start_recording(self, session_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def stop_recording(self, session_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def recording_state(self, session_id: str) -> RecordingState:  # pragma: no cover
        raise NotImplementedError

    # -- extension point ---------------------------------------------------
    def raw_call(
        self, session_id: str, operation: str, payload: Mapping[str, Any]
    ) -> Any:
        """Escape hatch for vendor features with no typed wrapper yet.

        Feature families such as zoom control, focus distance, lens
        information and zoom/focus presets are not property- or command-shaped.
        They can be reached through this hook before CrSDKPy models them, so
        adding them later never requires changing the contract.
        """
        raise NotImplementedError(
            f"backend {self.name!r} does not implement raw_call({operation!r})"
        )
