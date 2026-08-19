"""Capture lifecycle.

A capture is not a boolean. Hardware proved four distinct facts that clients
routinely conflate:

* the command was accepted;
* autofocus reached an accepted state (when applicable);
* an exposure actually completed;
* durable content exists and a preview can be pulled.

Each is separately observable and each can fail on its own, so
:class:`Capture` exposes progress rather than a success flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .enums import CaptureState, FocusState, PreviewKind
from .errors import OperationTimeoutError, UnsupportedOperationError
from .events import FocusSource
from .previews import Preview

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["Capture", "CapturedContent", "FocusResult"]


@dataclass(frozen=True)
class FocusResult:
    """Outcome of an autofocus attempt."""

    confirmed: bool
    state: FocusState
    elapsed_ms: int
    #: Which channel first reported the accepted state.
    source: str = FocusSource.PROPERTY

    def __bool__(self) -> bool:
        return self.confirmed


@dataclass(frozen=True)
class CapturedContent:
    """Durable media produced by a capture.

    ``content_id`` names the capture; ``file_id`` names one file within it,
    because a single exposure can write both a RAW and a JPEG.
    """

    content_id: int
    file_number: Optional[int] = None
    path: Optional[str] = None
    created_ms: int = 0
    file_id: int = 0
    #: Creation time as the camera reported it, ISO-8601 without a zone.
    captured_at: Optional[str] = None
    #: Geometry of the original still, when the camera reports it.
    width: Optional[int] = None
    height: Optional[int] = None
    file_size: Optional[int] = None

    @property
    def filename(self) -> Optional[str]:
        """Trailing filename component of :attr:`path`."""
        if not self.path:
            return None
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]


class Capture:
    """Progress of one capture operation."""

    def __init__(self, session: Session, *, requested_ms: int) -> None:
        self._session = session
        self._state = CaptureState.REQUESTED
        self.requested_ms = requested_ms
        self.focused_ms: Optional[int] = None
        self.exposed_ms: Optional[int] = None
        self.content_ms: Optional[int] = None
        self.focus: Optional[FocusResult] = None
        self.content: Optional[CapturedContent] = None
        self.failure: Optional[str] = None
        self._previews: dict[PreviewKind, Preview] = {}
        #: Newest content id before this capture, or ``None`` when the session
        #: has no content index. New content is anything strictly greater.
        self._baseline_content: Optional[int] = None

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> CaptureState:
        return self._state

    @property
    def exposed(self) -> bool:
        """Whether an exposure was confirmed by the camera.

        This is the first trustworthy evidence that a photo was taken.
        """
        return self.exposed_ms is not None

    @property
    def exposure_latency_ms(self) -> Optional[int]:
        """Milliseconds from requesting the capture to the exposure event.

        ``None`` until an exposure is confirmed. Both endpoints are measured on
        the same clock, so this is meaningful across every backend.
        """
        if self.exposed_ms is None:
            return None
        return self.exposed_ms - self.requested_ms

    @property
    def content_latency_ms(self) -> Optional[int]:
        """Milliseconds from requesting the capture to durable content."""
        if self.content_ms is None:
            return None
        return self.content_ms - self.requested_ms

    def _advance(self, state: CaptureState) -> None:
        self._state = state

    def _fail(self, reason: str) -> None:
        self.failure = reason
        self._state = CaptureState.FAILED

    # -- waiting -----------------------------------------------------------
    def wait_for_content(self, timeout_ms: int = 10_000) -> CapturedContent:
        """Block until durable content for this capture appears.

        Content is matched by identifier greater than the pre-capture
        baseline, never by ``baseline + 1``: identifiers are monotonic but
        have been observed to skip values.
        """
        if self.content is not None:
            return self.content
        content = self._session._await_content(self, timeout_ms=timeout_ms)
        if content is None:
            raise OperationTimeoutError(
                "durable content did not appear before the timeout",
                operation="capture.wait_for_content",
                timeout_ms=timeout_ms,
            )
        return content

    def preview(
        self, kind: PreviewKind = PreviewKind.SCREENNAIL, *, timeout_ms: int = 10_000
    ) -> Preview:
        """Fetch an exact-still preview of this capture.

        Raises :class:`~crsdkpy.errors.UnsupportedOperationError` when the
        session cannot provide that form; capability differs by control mode
        and, for postview, by still destination.
        """
        if kind is PreviewKind.LIVE_VIEW:
            raise UnsupportedOperationError(
                "a live-view frame is not a capture preview; it is never "
                "guaranteed to depict the captured exposure",
                capability="live_view",
                operation="capture.preview",
            )
        if kind in self._previews:
            return self._previews[kind]

        preview = self._session._fetch_capture_preview(
            self, kind, timeout_ms=timeout_ms
        )
        self._previews[kind] = preview
        if self._state is CaptureState.CONTENT_AVAILABLE:
            self._advance(CaptureState.PREVIEW_AVAILABLE)
        return preview

    def __repr__(self) -> str:
        return (
            f"Capture(state={self._state.value}, exposed={self.exposed}, "
            f"content={self.content.content_id if self.content else None})"
        )
