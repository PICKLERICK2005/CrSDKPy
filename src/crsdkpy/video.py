"""Movie recording facade.

Video is a first-class CrSDKPy feature and is optional per camera: a body
without it must still work normally, reporting the capability as absent rather
than failing obscurely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .enums import RecordingState
from .errors import OperationTimeoutError, UnsupportedOperationError

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["Recording", "Video"]


class Recording:
    """A single recording, returned by :meth:`Video.start`."""

    def __init__(self, video: Video, *, started_ms: int) -> None:
        self._video = video
        self.started_ms = started_ms
        self.stopped_ms: Optional[int] = None

    @property
    def state(self) -> RecordingState:
        return self._video.state

    @property
    def active(self) -> bool:
        return self._video.state in (RecordingState.STARTING, RecordingState.RECORDING)

    def wait_until_recording(self, *, timeout_ms: int = 5_000) -> bool:
        return self._video._wait_for(RecordingState.RECORDING, timeout_ms=timeout_ms)

    def stop(self, *, timeout_ms: int = 5_000) -> None:
        self._video.stop(timeout_ms=timeout_ms)
        self.stopped_ms = self._video._session._backend.clock.now_ms()

    def __enter__(self) -> Recording:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.active:
            self.stop()

    def __repr__(self) -> str:
        return f"Recording(state={self.state.value})"


class Video:
    """Movie recording for one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def available(self) -> bool:
        return self._session.capabilities.video

    @property
    def state(self) -> RecordingState:
        session = self._session
        session._check_usable("video.state")
        return session._backend.recording_state(session._id)

    @property
    def recording(self) -> bool:
        return self.state is RecordingState.RECORDING

    def _require(self, operation: str) -> None:
        session = self._session
        session._check_usable(operation)
        if not session.capabilities.video:
            raise UnsupportedOperationError(
                "this camera does not support movie recording in control mode "
                f"{session.mode.value!r}",
                capability="video",
                operation=operation,
            )

    def start(self, *, wait: bool = True, timeout_ms: int = 5_000) -> Recording:
        self._require("video.start")
        session = self._session
        started = session._backend.clock.now_ms()
        session._backend.start_recording(session._id)
        recording = Recording(self, started_ms=started)
        if wait and not self._wait_for(RecordingState.RECORDING, timeout_ms=timeout_ms):
            raise OperationTimeoutError(
                "recording did not become active before the timeout",
                operation="video.start",
                timeout_ms=timeout_ms,
            )
        return recording

    def stop(self, *, wait: bool = True, timeout_ms: int = 5_000) -> None:
        self._require("video.stop")
        session = self._session
        session._backend.stop_recording(session._id)
        if wait and not self._wait_for(RecordingState.IDLE, timeout_ms=timeout_ms):
            raise OperationTimeoutError(
                "recording did not stop before the timeout",
                operation="video.stop",
                timeout_ms=timeout_ms,
            )

    def _wait_for(self, target: RecordingState, *, timeout_ms: int) -> bool:
        session = self._session
        clock = session._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while True:
            if session._backend.recording_state(session._id) is target:
                return True
            if clock.now_ms() >= deadline:
                return False
            session._pump(timeout_ms=min(50, max(1, deadline - clock.now_ms())))
