"""One active connection to a camera.

A :class:`Session` owns the control mode, the still destination, the connection
state and the runtime capabilities. It does **not** own device identity: see
:class:`~crsdkpy.camera.Camera`, which outlives any session and can reopen in a
different mode.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional, Union

from .capabilities import SessionCapabilities
from .capture import Capture, CapturedContent, FocusResult
from .commands import Command, CommandParameter
from .content import Content, _project
from .enums import (
    CaptureState,
    ConnectionState,
    FocusState,
    PreviewKind,
    SessionMode,
    StillDestination,
)
from .errors import (
    AutofocusFailedError,
    CameraConnectionError,
    InvalidSessionStateError,
    OperationTimeoutError,
    SessionClosedError,
    UnsupportedOperationError,
)
from .events import (
    CaptureEvent,
    ConnectionEvent,
    ContentEvent,
    Event,
    FocusEvent,
    FocusSource,
)
from .liveview import LiveView
from .previews import Preview
from .properties import Property, PropertyCode, PropertySnapshot
from .raw import RawAccess
from .status import BatteryStatus, StorageSlot
from .video import Video

if TYPE_CHECKING:  # pragma: no cover
    from .backend.contract import Backend
    from .camera import Camera

__all__ = ["Properties", "Session"]

#: Direct focus reads are scheduled sparsely rather than polled tightly.
#: Tight synchronous polling of focus properties was observed to hang a real
#: session, and these few reads are enough to defeat the sticky-value race.
_FOCUS_READ_POINTS_MS = (150, 400, 900, 1600, 2500)

#: Dwell between release down and release up.
_DEFAULT_RELEASE_DWELL_MS = 35

#: Minimum gap between content-index polls while waiting for a new item.
#: Content was characterized as appearing around 800 ms after the trigger, so
#: this is frequent enough to measure and sparse enough not to flood the
#: camera with synchronous reads.
_CONTENT_POLL_INTERVAL_MS = 150


def _content_from_ref(ref, *, created_ms: Optional[int] = None) -> CapturedContent:
    """Project a backend content record onto the public capture record."""
    projected = _project(ref)
    if created_ms is None:
        return projected
    return replace(projected, created_ms=created_ms)


class Properties:
    """Property access for one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self) -> PropertySnapshot:
        """Read every property the camera currently reports.

        The number of properties is not meaningful and must never be asserted:
        it varies with control mode on a single body.
        """
        session = self._session
        session._check_usable("properties.snapshot")
        props = session._backend.list_properties(session._id)
        return PropertySnapshot(
            list(props), timestamp_ms=session._backend.clock.now_ms()
        )

    def get(self, code: Union[int, PropertyCode]) -> Property:
        session = self._session
        session._check_usable("properties.get")
        return session._backend.get_property(session._id, PropertyCode(code))

    def set(self, code: Union[int, PropertyCode], value: Any) -> None:
        session = self._session
        session._check_usable("properties.set")
        session._backend.set_property(session._id, PropertyCode(code), value)

    def codes(self) -> Sequence[PropertyCode]:
        return tuple(self.snapshot().codes())

    def __getitem__(self, code: Union[int, PropertyCode]) -> Property:
        return self.get(code)

    def __contains__(self, code: object) -> bool:
        if not isinstance(code, (int, PropertyCode)):
            return False
        try:
            self.get(code)
        except Exception:
            return False
        return True


class Session:
    """An open connection in one control mode.

    Obtained from :meth:`~crsdkpy.camera.Camera.open`. Usable as a context
    manager; closing is idempotent.
    """

    def __init__(
        self,
        camera: Camera,
        backend: Backend,
        session_id: str,
        mode: SessionMode,
    ) -> None:
        self._camera = camera
        self._backend = backend
        self._id = session_id
        self._mode = mode
        self._closed = False
        self._state = ConnectionState.CONNECTING

        # Every backend event lands in both queues: one for the caller's
        # stream, one for internal operations. Neither starves the other.
        self._user_events: deque[Event] = deque()
        self._op_events: deque[Event] = deque()

        self.properties = Properties(self)
        self.content = Content(self)
        self.live_view = LiveView(self)
        self.video = Video(self)
        self.raw = RawAccess(self)

        self._pump(timeout_ms=0)
        self._state = backend.connection_state(session_id)

    # -- identity ----------------------------------------------------------
    @property
    def camera(self) -> Camera:
        return self._camera

    @property
    def mode(self) -> SessionMode:
        return self._mode

    @property
    def destination(self) -> StillDestination:
        return self._backend.get_destination(self._id)

    @property
    def battery(self) -> BatteryStatus:
        """Charge state, read fresh from the camera.

        Fields a camera does not report come back empty rather than as an
        error: not every body reports every reading.
        """
        self._check_usable("battery")
        return self._backend.battery(self._id)

    @property
    def storage(self) -> Sequence[StorageSlot]:
        """Media slots the camera reports. Possibly empty."""
        self._check_usable("storage")
        return tuple(self._backend.storage(self._id))

    @property
    def state(self) -> ConnectionState:
        if self._closed:
            return ConnectionState.CLOSED
        self._pump(timeout_ms=0)
        return self._state

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def capabilities(self) -> SessionCapabilities:
        """Capabilities of this session, given its mode and destination.

        Recomputed on access because changing the destination can change what
        the session can do.
        """
        if self._closed:
            raise SessionClosedError("session is closed", operation="capabilities")
        return self._backend.session_capabilities(self._id)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the session. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._state = ConnectionState.CLOSED
        self._backend.close_session(self._id)
        self._camera._forget_session(self)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _check_usable(self, operation: str) -> None:
        if self._closed:
            raise SessionClosedError("session is closed", operation=operation)
        state = self._backend.connection_state(self._id)
        self._state = state
        if state is ConnectionState.RECONNECTING:
            raise CameraConnectionError(
                "the camera transport is recovering; retry when reconnected",
                operation=operation,
            )
        if not state.is_usable:
            raise InvalidSessionStateError(
                f"session is {state.value}, which does not permit this operation",
                state=state.value,
                operation=operation,
            )

    def configure_postview(
        self, *, enabled: bool = True, transfer_to_ram: bool = True
    ) -> None:
        """Ask the camera to deliver a postview after each capture.

        Raises :class:`~crsdkpy.errors.UnsupportedOperationError` when the
        camera refuses. That refusal says nothing about whether postview will
        arrive: one body rejects this call and still delivers once the still
        destination includes the host. Configuration and delivery are separate
        capabilities for exactly that reason, so check
        ``capabilities.postview_delivery`` rather than assuming this failed
        for both.
        """
        self._check_usable("configure_postview")
        self._backend.configure_postview(
            self._id, enabled=enabled, transfer_to_ram=transfer_to_ram
        )

    def set_destination(self, destination: StillDestination) -> None:
        """Change where captured stills are stored.

        Destination is independent of control mode and can change what the
        session is capable of, so read :attr:`capabilities` again afterwards.
        """
        self._check_usable("set_destination")
        self._backend.set_destination(self._id, destination)

    # -- events ------------------------------------------------------------
    def _pump(self, timeout_ms: int = 0) -> Sequence[Event]:
        """Drain the backend queue into both internal queues."""
        if self._closed:
            return ()
        events = self._backend.poll_events(self._id, timeout_ms)
        for event in events:
            if isinstance(event, ConnectionEvent):
                self._state = event.state
            self._user_events.append(event)
            self._op_events.append(event)
        return events

    def events(
        self, *, timeout_ms: int = 0, limit: Optional[int] = None
    ) -> Iterator[Event]:
        """Iterate events as they arrive.

        Yields anything already buffered, then polls. Ends when nothing new
        arrives within *timeout_ms*, so a caller can drive it from a loop
        without blocking forever.
        """
        produced = 0
        while limit is None or produced < limit:
            if self._user_events:
                produced += 1
                yield self._user_events.popleft()
                continue
            if self._closed:
                return
            self._pump(timeout_ms=timeout_ms)
            if not self._user_events:
                return

    def drain_events(self, *, timeout_ms: int = 0) -> list[Event]:
        """Return buffered events, waiting up to *timeout_ms* for the first.

        This returns as soon as anything is available; it does not wait out the
        full timeout. To wait for a particular outcome use
        :meth:`wait_for_event` or :meth:`wait_for_state`.
        """
        self._pump(timeout_ms=timeout_ms)
        drained = list(self._user_events)
        self._user_events.clear()
        return drained

    def wait_for_event(
        self, event_type: type, *, timeout_ms: int = 10_000
    ) -> Optional[Event]:
        """Wait for the next event of *event_type*.

        Returns ``None`` on timeout. Other events stay buffered on the stream,
        so waiting for one kind never discards another.
        """
        clock = self._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while True:
            for index, event in enumerate(self._user_events):
                if isinstance(event, event_type):
                    del self._user_events[index]
                    return event
            remaining = deadline - clock.now_ms()
            if remaining <= 0 or self._closed:
                return None
            self._pump(timeout_ms=min(remaining, 250))

    def wait_for_state(
        self, state: ConnectionState, *, timeout_ms: int = 60_000
    ) -> bool:
        """Wait until the connection reaches *state*.

        Useful across a reconnect: recovery is not guaranteed to be preceded by
        a disconnect, so polling the state is more reliable than waiting for a
        particular event sequence.
        """
        clock = self._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while True:
            if self._closed:
                return state is ConnectionState.CLOSED
            if self._backend.connection_state(self._id) is state:
                self._state = state
                return True
            remaining = deadline - clock.now_ms()
            if remaining <= 0:
                return False
            self._pump(timeout_ms=min(remaining, 250))

    def _take_op_events(self) -> list[Event]:
        taken = list(self._op_events)
        self._op_events.clear()
        return taken

    # -- autofocus ---------------------------------------------------------
    def autofocus(self, *, timeout_ms: int = 3_000) -> FocusResult:
        """Assert the half-press stage and wait for a focus verdict.

        Returns a :class:`~crsdkpy.capture.FocusResult` rather than raising, so
        a caller can decide what a failure means. The half-press stage is left
        engaged on success so a release can follow immediately; on failure it
        is released, because a failed autofocus leaves it engaged on hardware.
        """
        self._check_usable("autofocus")
        if not self.capabilities.autofocus_s1:
            raise UnsupportedOperationError(
                "this camera has no separate half-press stage",
                capability="autofocus_s1",
                operation="autofocus",
            )
        self._clear_stale_half_press()
        result = self._focus_gate(timeout_ms=timeout_ms)
        if not result.confirmed:
            self._release_half_press()
        return result

    def _clear_stale_half_press(self) -> None:
        """Release a half-press left engaged by an earlier failed attempt."""
        try:
            if self._backend.get_half_press(self._id):
                self._backend.set_half_press(self._id, False)
        except UnsupportedOperationError:
            pass

    def _release_half_press(self, *, timeout_ms: int = 1_500) -> bool:
        """Release the half-press stage if it is still engaged, and confirm it.

        Verified rather than assumed, in both directions. A successful release
        clears the stage by itself on some bodies, so writing unconditionally
        would be a redundant round trip; a failed autofocus genuinely leaves it
        engaged. The write is also not instantaneous, so the cleared state is
        polled for rather than trusted immediately.

        Returns whether the stage is confirmed released.
        """
        clock = self._backend.clock
        try:
            if not self._backend.get_half_press(self._id):
                return True
            self._backend.set_half_press(self._id, False)
            deadline = clock.now_ms() + max(0, int(timeout_ms))
            while True:
                if not self._backend.get_half_press(self._id):
                    return True
                if clock.now_ms() >= deadline:
                    return False
                clock.sleep_ms(50)
        except UnsupportedOperationError:
            return True  # no such stage on this camera

    def _focus_gate(self, *, timeout_ms: int) -> FocusResult:
        """Engage the half-press stage and wait for an accepted focus state.

        Consumes both asynchronous focus channels and, in addition, performs a
        few spaced direct reads. Neither channel is reliably first, they can
        transiently disagree, and an already-focused value may emit no
        notification at all, so no single source is sufficient.
        """
        clock = self._backend.clock
        start = clock.now_ms()
        deadline = start + max(0, int(timeout_ms))

        self._backend.set_half_press(self._id, True)

        # Sticky-value guard: if focus is already achieved, no change event
        # will fire and a notification-only wait would time out on good focus.
        immediate = self._backend.focus_state(self._id)
        if immediate.is_focused:
            return FocusResult(
                confirmed=True,
                state=immediate,
                elapsed_ms=clock.now_ms() - start,
                source=FocusSource.DIRECT_READ,
            )

        read_points = deque(
            p for p in _FOCUS_READ_POINTS_MS if p <= max(0, int(timeout_ms))
        )
        latest = immediate

        while True:
            now = clock.now_ms()
            now - start
            if now >= deadline:
                break

            next_read = read_points[0] if read_points else None
            if next_read is None:
                slice_end = deadline
            else:
                slice_end = min(deadline, start + next_read)
            wait_ms = max(1, slice_end - now)
            self._pump(timeout_ms=wait_ms)

            for event in self._take_op_events():
                if isinstance(event, FocusEvent):
                    latest = event.state
                    if event.is_focused:
                        return FocusResult(
                            confirmed=True,
                            state=event.state,
                            elapsed_ms=event.timestamp_ms - start,
                            source=event.source,
                        )

            if read_points and clock.now_ms() - start >= read_points[0]:
                read_points.popleft()
                latest = self._backend.focus_state(self._id)
                if latest.is_focused:
                    return FocusResult(
                        confirmed=True,
                        state=latest,
                        elapsed_ms=clock.now_ms() - start,
                        source=FocusSource.DIRECT_READ,
                    )

        # One last direct read: the verdict may have landed in the final slice.
        final = self._backend.focus_state(self._id)
        if final.is_focused:
            return FocusResult(
                confirmed=True,
                state=final,
                elapsed_ms=clock.now_ms() - start,
                source=FocusSource.DIRECT_READ,
            )
        if final is not FocusState.UNKNOWN:
            latest = final
        return FocusResult(
            confirmed=False, state=latest, elapsed_ms=clock.now_ms() - start
        )

    # -- capture -----------------------------------------------------------
    def capture(
        self,
        *,
        dwell_ms: int = _DEFAULT_RELEASE_DWELL_MS,
        wait_for_exposure: bool = True,
        timeout_ms: int = 10_000,
    ) -> Capture:
        """Release the shutter without autofocus.

        This drives the full-press stage only. On a camera in an autofocus
        mode it may produce no exposure at all, which is why the returned
        :class:`~crsdkpy.capture.Capture` reports progress rather than success.
        """
        self._check_usable("capture")
        if not self.capabilities.still_capture:
            raise UnsupportedOperationError(
                "this session cannot capture stills",
                capability="still_capture",
                operation="capture",
            )
        clock = self._backend.clock
        capture = Capture(self, requested_ms=clock.now_ms())
        capture._baseline_content = self._content_baseline()

        self._backend.send_command(self._id, Command.RELEASE, CommandParameter.DOWN)
        clock.sleep_ms(dwell_ms)
        self._backend.send_command(self._id, Command.RELEASE, CommandParameter.UP)

        if wait_for_exposure:
            self._await_exposure(capture, timeout_ms=timeout_ms)
        return capture

    def autofocus_and_capture(
        self,
        *,
        focus_timeout_ms: int = 3_000,
        dwell_ms: int = _DEFAULT_RELEASE_DWELL_MS,
        timeout_ms: int = 10_000,
        raise_on_focus_failure: bool = True,
    ) -> Capture:
        """Autofocus, then release only if focus was confirmed.

        The release is gated: if autofocus does not reach an accepted state,
        no exposure is requested at all and the half-press stage is released.
        This is the difference between this method and the vendor's combined
        command, which commits the exposure before focus can be inspected.
        """
        self._check_usable("autofocus_and_capture")
        caps = self.capabilities
        if not caps.still_capture:
            raise UnsupportedOperationError(
                "this session cannot capture stills",
                capability="still_capture",
                operation="autofocus_and_capture",
            )
        if not caps.autofocus_s1:
            raise UnsupportedOperationError(
                "this camera has no separate half-press stage, so a gated "
                "autofocus capture is not possible; use capture() instead",
                capability="autofocus_s1",
                operation="autofocus_and_capture",
            )

        clock = self._backend.clock
        capture = Capture(self, requested_ms=clock.now_ms())
        capture._baseline_content = self._content_baseline()
        capture._advance(CaptureState.FOCUSING)

        self._clear_stale_half_press()
        focus = self._focus_gate(timeout_ms=focus_timeout_ms)
        capture.focus = focus

        if not focus.confirmed:
            self._release_half_press()
            capture._fail(f"autofocus did not confirm (last state {focus.state.value})")
            if raise_on_focus_failure:
                raise AutofocusFailedError(
                    "autofocus did not reach an accepted focused state; "
                    "no exposure was requested",
                    focus_state=focus.state,
                    elapsed_ms=focus.elapsed_ms,
                )
            return capture

        capture.focused_ms = clock.now_ms()
        capture._advance(CaptureState.FOCUSED)

        self._backend.send_command(self._id, Command.RELEASE, CommandParameter.DOWN)
        clock.sleep_ms(dwell_ms)
        self._backend.send_command(self._id, Command.RELEASE, CommandParameter.UP)

        self._await_exposure(capture, timeout_ms=timeout_ms)
        # Verify rather than assume the release cleared the half-press stage.
        self._release_half_press()
        return capture

    def _await_exposure(self, capture: Capture, *, timeout_ms: int) -> None:
        clock = self._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while clock.now_ms() < deadline:
            self._pump(timeout_ms=max(1, min(50, deadline - clock.now_ms())))
            for event in self._take_op_events():
                if isinstance(event, CaptureEvent):
                    # Observed time, not the backend's own stamp: the two
                    # need not share a clock origin.
                    capture.exposed_ms = clock.now_ms()
                    capture._advance(CaptureState.EXPOSED)
                    return
                if isinstance(event, ContentEvent):
                    self._record_content(capture, event)
        if not capture.exposed:
            capture._fail("no exposure was reported before the timeout")

    def _content_baseline(self) -> Optional[int]:
        if not self.capabilities.content_index:
            return None
        latest = self._backend.latest_content(self._id)
        return latest.content_id if latest else None

    def _record_content(self, capture: Capture, event: ContentEvent) -> None:
        baseline = capture._baseline_content
        if event.content_id is None:
            return
        # Identifiers are monotonic but not contiguous, so compare against the
        # baseline rather than expecting baseline + 1.
        if baseline is not None and event.content_id <= baseline:
            return
        if capture.content is not None:
            return
        capture.content = CapturedContent(
            content_id=event.content_id,
            file_number=event.file_number,
            path=event.path,
            created_ms=event.timestamp_ms,
        )
        # A notification says less than the index does, and on some backends
        # says nothing but the identifier. Fill in the rest so that content
        # resolved either way describes the same thing.
        self._enrich_content(capture)
        capture.content_ms = event.timestamp_ms
        capture._advance(CaptureState.CONTENT_AVAILABLE)

    def _enrich_content(self, capture: Capture) -> None:
        content = capture.content
        if content is None or content.file_id:
            return
        try:
            ref = self._find_content(content.content_id)
        except (UnsupportedOperationError, CameraConnectionError):
            return  # the notification is all this session can offer
        if ref is None:
            return
        capture.content = _content_from_ref(ref, created_ms=content.created_ms)

    def _find_content(self, content_id: int):
        for ref in self._backend.list_content(self._id, newer_than=content_id - 1):
            if ref.content_id == content_id:
                return ref
        return None

    def _await_content(
        self, capture: Capture, *, timeout_ms: int
    ) -> Optional[CapturedContent]:
        if capture.content is not None:
            return capture.content
        if not self.capabilities.content_index:
            raise UnsupportedOperationError(
                "the content index is not available in control mode "
                f"{self._mode.value!r}, so durable content cannot be resolved "
                "from this session",
                capability="content_index",
                operation="capture.wait_for_content",
            )
        clock = self._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        baseline = capture._baseline_content
        next_poll_ms = 0
        while clock.now_ms() < deadline:
            for event in self._take_op_events():
                if isinstance(event, ContentEvent):
                    self._record_content(capture, event)
            if capture.content is not None:
                return capture.content
            # Also poll the index: a notification is not guaranteed, and on
            # some backends it names nothing. Paced rather than tight, because
            # each poll is a real round trip to the camera and hammering a
            # synchronous vendor read has been observed to stall a session.
            now = clock.now_ms()
            if now < next_poll_ms:
                self._pump(timeout_ms=max(1, min(50, deadline - now)))
                continue
            next_poll_ms = now + _CONTENT_POLL_INTERVAL_MS
            newer = self._backend.list_content(self._id, newer_than=baseline)
            if newer:
                # Newest wins. A burst can produce several items past the
                # baseline; the one this capture asked for is the last.
                capture.content = _content_from_ref(newer[-1])
                capture.content_ms = clock.now_ms()
                capture._advance(CaptureState.CONTENT_AVAILABLE)
                return capture.content
            self._pump(timeout_ms=max(1, min(50, deadline - clock.now_ms())))
        return None

    def _fetch_capture_preview(
        self, capture: Capture, kind: PreviewKind, *, timeout_ms: int
    ) -> Preview:
        self._check_usable("capture.preview")
        caps = self.capabilities
        if kind is PreviewKind.POSTVIEW:
            if not caps.postview_delivery:
                raise UnsupportedOperationError(
                    "postview is not delivered with destination "
                    f"{self.destination.value!r} in control mode "
                    f"{self._mode.value!r}",
                    capability="postview_delivery",
                    operation="capture.preview",
                )
            clock = self._backend.clock
            deadline = clock.now_ms() + max(0, int(timeout_ms))
            while clock.now_ms() < deadline:
                preview = self._backend.pull_postview(self._id)
                if preview is not None:
                    return preview
                self._pump(timeout_ms=max(1, min(50, deadline - clock.now_ms())))
            raise OperationTimeoutError(
                "postview was not delivered before the timeout",
                operation="capture.preview",
                timeout_ms=timeout_ms,
            )

        content = capture.content or self._await_content(capture, timeout_ms=timeout_ms)
        if content is None:
            raise OperationTimeoutError(
                "durable content did not appear, so no exact preview exists yet",
                operation="capture.preview",
                timeout_ms=timeout_ms,
            )
        preview = self._backend.get_preview(self._id, content.content_id, kind)
        if preview.content_id is not None and preview.content_id != content.content_id:
            raise CameraConnectionError(
                "the camera returned a preview belonging to content "
                f"{preview.content_id}, not {content.content_id}; refusing to "
                "present a stale image",
                operation="capture.preview",
            )
        return preview

    def __repr__(self) -> str:
        state = "closed" if self._closed else self._state.value
        return (
            f"Session(model={self._camera.info.model!r}, mode={self._mode.value}, "
            f"state={state})"
        )
