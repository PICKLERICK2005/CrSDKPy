"""Live view facade."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .errors import OperationTimeoutError, UnsupportedOperationError
from .previews import LiveViewFrame

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["LiveView", "LiveViewStats", "LiveViewStatus"]


@dataclass(frozen=True)
class LiveViewStats:
    """What a live-view stream actually did, as opposed to what it should do.

    Collected so a transport decision can be made from measurement rather than
    assumption. Cadence is reported as observed intervals, never as a nominal
    frame rate: a camera pauses around an exposure and resumes, so an average
    alone hides the behaviour that matters.
    """

    frames: int = 0
    #: Polls that returned nothing because no new frame had arrived.
    empty_polls: int = 0
    #: Frames the camera produced that were never collected, inferred from
    #: gaps in its own sequence numbers. Not a queue: live view keeps only
    #: the newest frame by design.
    skipped: int = 0
    elapsed_ms: int = 0
    total_bytes: int = 0
    min_bytes: Optional[int] = None
    max_bytes: Optional[int] = None
    #: Gaps between consecutive delivered frames.
    min_interval_ms: Optional[int] = None
    max_interval_ms: Optional[int] = None
    #: Wall time spent inside the fetch call itself.
    max_fetch_ms: Optional[int] = None

    @property
    def fps(self) -> float:
        if self.elapsed_ms <= 0 or self.frames == 0:
            return 0.0
        return self.frames * 1000.0 / self.elapsed_ms

    @property
    def mean_bytes(self) -> float:
        return self.total_bytes / self.frames if self.frames else 0.0

    @property
    def throughput_mib_s(self) -> float:
        if self.elapsed_ms <= 0:
            return 0.0
        return self.total_bytes / (1024 * 1024) / (self.elapsed_ms / 1000.0)

    def __repr__(self) -> str:
        return (
            f"LiveViewStats({self.frames} frames, {self.fps:.1f} fps, "
            f"{self.mean_bytes / 1024:.1f} KiB mean, "
            f"{self.throughput_mib_s:.2f} MiB/s)"
        )


@dataclass(frozen=True)
class LiveViewStatus:
    """What the camera reports about its live-view stream.

    ``info_ok`` and ``usable`` are separate because a vendor info call can
    report success while the stream cannot actually deliver a frame.
    """

    available: bool
    info_ok: bool
    width: Optional[int]
    height: Optional[int]
    buffer_size: int

    @property
    def usable(self) -> bool:
        return self.available and self.info_ok and self.buffer_size > 0


class LiveView:
    """Frame access for one session.

    Frames are copied out by the backend; there is no caller-managed buffer
    and no vendor-owned memory is ever exposed.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def available(self) -> bool:
        """Whether this session's capabilities include live view."""
        return self._session.capabilities.live_view

    def status(self) -> LiveViewStatus:
        session = self._session
        session._check_usable("live_view.status")
        info = session._backend.live_view_info(session._id)
        return LiveViewStatus(
            available=session.capabilities.live_view,
            info_ok=info.info_ok,
            width=info.width,
            height=info.height,
            buffer_size=info.buffer_size,
        )

    def get_frame(self, *, timeout_ms: int = 2_000) -> LiveViewFrame:
        """Return the next frame, waiting up to *timeout_ms*.

        Raises :class:`~crsdkpy.errors.OperationTimeoutError` if no frame
        arrives, which is a normal outcome around an exposure: the stream
        pauses briefly and then resumes on its own.
        """
        frame = self.try_get_frame(timeout_ms=timeout_ms)
        if frame is None:
            raise OperationTimeoutError(
                "no live-view frame arrived before the timeout",
                operation="live_view.get_frame",
                timeout_ms=timeout_ms,
            )
        return frame

    def try_get_frame(self, *, timeout_ms: int = 0) -> Optional[LiveViewFrame]:
        """Return the next frame, or ``None`` if none arrived in time."""
        session = self._session
        session._check_usable("live_view.try_get_frame")
        if not session.capabilities.live_view:
            raise UnsupportedOperationError(
                "live view is not available in control mode "
                f"{session.mode.value!r} on this camera",
                capability="live_view",
                operation="live_view.try_get_frame",
            )
        clock = session._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while True:
            frame = session._backend.get_live_view_frame(session._id)
            if frame is not None:
                return frame
            now = clock.now_ms()
            if now >= deadline:
                return None
            clock.sleep_ms(min(10, deadline - now))

    def frames(
        self, *, limit: Optional[int] = None, timeout_ms: int = 2_000
    ) -> Iterator[LiveViewFrame]:
        """Yield frames until *limit* is reached or a frame times out."""
        produced = 0
        while limit is None or produced < limit:
            frame = self.try_get_frame(timeout_ms=timeout_ms)
            if frame is None:
                return
            produced += 1
            yield frame

    def measure(
        self, *, duration_ms: int = 5_000, timeout_ms: int = 2_000
    ) -> LiveViewStats:
        """Stream for a while and report what the transport actually managed.

        Exists so a decision to change transport can be made from numbers. It
        counts empty polls and sequence gaps separately from delivered frames,
        because "slow" and "keeping up but idle" look identical in a frame rate
        alone.
        """
        session = self._session
        clock = session._backend.clock
        started = clock.now_ms()
        deadline = started + max(0, int(duration_ms))

        frames = empty = total = 0
        min_bytes = max_bytes = None
        min_interval = max_interval = max_fetch = None
        previous_ms = None
        first_number = last_number = None

        while clock.now_ms() < deadline:
            before = clock.now_ms()
            frame = self.try_get_frame(timeout_ms=min(timeout_ms, 100))
            after = clock.now_ms()
            if frame is None:
                empty += 1
                continue
            fetch_ms = after - before
            max_fetch = fetch_ms if max_fetch is None else max(max_fetch, fetch_ms)
            frames += 1
            size = frame.byte_length
            total += size
            min_bytes = size if min_bytes is None else min(min_bytes, size)
            max_bytes = size if max_bytes is None else max(max_bytes, size)
            if previous_ms is not None:
                gap = after - previous_ms
                min_interval = gap if min_interval is None else min(min_interval, gap)
                max_interval = gap if max_interval is None else max(max_interval, gap)
            previous_ms = after
            if first_number is None:
                first_number = frame.frame_number
            last_number = frame.frame_number

        # Gaps in the camera's own numbering are frames it produced that were
        # never collected. Only meaningful when it numbers them.
        skipped = 0
        if first_number is not None and last_number is not None and frames > 1:
            produced_by_camera = last_number - first_number + 1
            skipped = max(0, produced_by_camera - frames)

        return LiveViewStats(
            frames=frames,
            empty_polls=empty,
            skipped=skipped,
            elapsed_ms=clock.now_ms() - started,
            total_bytes=total,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            min_interval_ms=min_interval,
            max_interval_ms=max_interval,
            max_fetch_ms=max_fetch,
        )
