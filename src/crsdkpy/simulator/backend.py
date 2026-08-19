"""Deterministic simulator backend.

This is a behavioural model, not a set of canned returns. It runs a scheduled
event timeline on a virtual clock, so a client can exercise a 28 second
reconnect or a 1.1 second content delay without spending that time.

Everything it models was observed on real hardware or is a failure a robust
client must survive. See :mod:`crsdkpy.simulator.scenarios`.
"""

from __future__ import annotations

import heapq
import itertools
import random
import struct
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Optional

from ..backend.contract import Backend, BackendCameraInfo, ContentRef, LiveViewInfo
from ..capabilities import SessionCapabilities
from ..clock import Clock, VirtualClock
from ..commands import Command, CommandLike, CommandParameter
from ..enums import (
    ConnectionState,
    FocusState,
    PreviewKind,
    RecordingState,
    SessionMode,
    StillDestination,
)
from ..errors import (
    CameraBusyError,
    CameraConnectionError,
    PropertyNotSupportedError,
    SessionClosedError,
    UnsupportedOperationError,
)
from ..events import (
    CaptureEvent,
    ConnectionEvent,
    ContentEvent,
    Event,
    FocusEvent,
    FocusSource,
    PropertyChangedEvent,
    RecordingEvent,
    UnknownEvent,
)
from ..previews import LiveViewFrame, Preview
from ..properties import Property, PropertyCode
from ..status import BatteryStatus, StorageSlot
from . import profiles as P
from .profiles import CameraProfile, get_profile
from .scenarios import AfOutcome, FocusChannel, Scenario

__all__ = ["SimulatedBackend"]

_FOCUS_VALUE_TO_STATE = {
    P.FOCUS_UNLOCKED: FocusState.UNLOCKED,
    P.FOCUS_FOCUSED_AF_S: FocusState.FOCUSED_AF_S,
    P.FOCUS_NOT_FOCUSED_AF_S: FocusState.NOT_FOCUSED_AF_S,
    P.FOCUS_FOCUSED_AF_C: FocusState.FOCUSED_AF_C,
    P.FOCUS_NOT_FOCUSED_AF_C: FocusState.NOT_FOCUSED_AF_C,
    P.FOCUS_TRACKING_AF_C: FocusState.TRACKING_AF_C,
}

_FOCUS_MODE_AF_S = 0x0002
_FOCUS_MODE_AF_C = 0x0003


def _synth_image(seed: int, length: int, width: int = 0, height: int = 0) -> bytes:
    """Deterministic JPEG bytes of the requested geometry, unique per seed.

    A real frame header is emitted rather than a plausible-looking prefix, so
    code that parses dimensions out of returned bytes - which is what the
    native backend does, because the camera's reported geometry belongs to the
    original still and not to a preview of it - behaves identically here.
    """
    length = max(32, int(length))
    rnd = random.Random(seed)
    header = b"\xff\xd8"
    if width > 0 and height > 0:
        # SOF0: length 11, 8-bit precision, height, width, one component.
        header += b"\xff\xc0" + struct.pack(
            ">HBHHBBBB", 11, 8, int(height), int(width), 1, 1, 0x11, 0
        )
    body_length = length - len(header) - 2
    body = bytes(rnd.getrandbits(8) for _ in range(min(48, max(0, body_length))))
    filler = bytes([rnd.getrandbits(8) or 0x5A]) * max(0, body_length - len(body))
    return header + body + filler + b"\xff\xd9"


class _Session:
    """Mutable state for one simulated connection."""

    def __init__(
        self,
        session_id: str,
        profile: CameraProfile,
        scenario: Scenario,
        mode: SessionMode,
        destination: StillDestination,
        opened_ms: int,
    ) -> None:
        self.id = session_id
        self.profile = profile
        self.scenario = scenario
        self.mode = mode
        self.destination = destination
        self.state = ConnectionState.CONNECTING
        self.opened_ms = opened_ms
        self.closed = False

        self.properties: dict[int, Any] = {}
        for code in profile.property_codes_for(mode):
            self.properties[code] = self._initial_value(code, destination)

        self.recording = RecordingState.IDLE
        self.postview_configured = False
        self.postview_pending: Optional[int] = None  # content id awaiting pull
        self.contents: list[ContentRef] = []
        self.next_content_id = 131_000
        self.capture_sequence = 0
        self.last_delivered_content: Optional[int] = None

        self.live_view_frame_no = 0
        self.next_frame_ms = opened_ms
        self.live_view_blocked_until = 0

        self._heap: list[tuple[int, int, Callable[[], Sequence[Event]]]] = []
        self._counter = itertools.count()
        self.ready: list[Event] = []

    @staticmethod
    def _initial_value(code: int, destination: StillDestination) -> Any:
        if code == P.CODE_S1:
            return P.LOCK_UNLOCKED
        if code == P.CODE_FOCUS_INDICATION:
            return P.FOCUS_UNLOCKED
        if code == P.CODE_FOCUS_MODE:
            return _FOCUS_MODE_AF_S
        if code == P.CODE_DRIVE_MODE:
            return 0x0001
        if code == P.CODE_RECORDING_STATE:
            return 0x0000
        if code == P.CODE_ISO:
            return 100
        if code in (P.CODE_CAMERA_CAUTION, P.CODE_SYSTEM_CAUTION):
            return 0x01
        if code == P.CODE_DESTINATION:
            return {
                StillDestination.MEMORY_CARD: 0x0002,
                StillDestination.HOST: 0x0001,
                StillDestination.HOST_AND_MEMORY_CARD: 0x0003,
            }[destination]
        return 0

    # -- scheduling --------------------------------------------------------
    def schedule(self, at_ms: int, action: Callable[[], Sequence[Event]]) -> None:
        heapq.heappush(self._heap, (int(at_ms), next(self._counter), action))

    def emit(self, at_ms: int, event: Event) -> None:
        self.schedule(at_ms, lambda e=event: (e,))

    def next_scheduled_ms(self) -> Optional[int]:
        return self._heap[0][0] if self._heap else None

    def drain_due(self, now_ms: int) -> list[Event]:
        out = list(self.ready)
        self.ready.clear()
        while self._heap and self._heap[0][0] <= now_ms:
            _, _, action = heapq.heappop(self._heap)
            out.extend(action())
        return out


class SimulatedBackend(Backend):
    """A behavioural Sony-camera simulator.

    Parameters
    ----------
    profile:
        Profile name or :class:`~crsdkpy.simulator.profiles.CameraProfile`.
    scenario:
        Behaviour for this run. Defaults to nominal.
    clock:
        Defaults to a :class:`~crsdkpy.clock.VirtualClock` so tests are
        deterministic and instantaneous. Pass a
        :class:`~crsdkpy.clock.RealClock` for a live demonstration.
    """

    name = "simulator"

    def __init__(
        self,
        profile: Any = "fx3a",
        *,
        scenario: Optional[Scenario] = None,
        clock: Optional[Clock] = None,
        cameras: Optional[Sequence[Any]] = None,
    ) -> None:
        if cameras is None:
            cameras = [profile]
        self._profiles: list[CameraProfile] = [
            p if isinstance(p, CameraProfile) else get_profile(str(p)) for p in cameras
        ]
        self._scenario = scenario or Scenario()
        self._clock = clock or VirtualClock()
        self._started = False
        self._sessions: dict[str, _Session] = {}
        self._session_counter = itertools.count(1)

    # -- lifecycle ---------------------------------------------------------
    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    def start(self) -> None:
        self._started = True

    def shutdown(self) -> None:
        for session_id in list(self._sessions):
            self.close_session(session_id)
        self._started = False

    def enumerate_cameras(self) -> Sequence[BackendCameraInfo]:
        return [
            BackendCameraInfo(
                device_key=f"sim:{profile.name}:{index}",
                model=profile.model,
                serial=profile.serial or f"SIM{index:06d}",
                firmware=profile.firmware,
                transport=profile.transport,
                adapter=profile.adapter,
                usb_pid=profile.usb_pid,
                capabilities=profile.camera_capabilities(),
                metadata={"profile": profile.name, "simulated": True},
            )
            for index, profile in enumerate(self._profiles)
        ]

    def _profile_for(self, device_key: str) -> CameraProfile:
        for index, profile in enumerate(self._profiles):
            if device_key == f"sim:{profile.name}:{index}":
                return profile
        raise CameraConnectionError(
            f"unknown device key {device_key!r}", operation="open_session"
        )

    # -- sessions ----------------------------------------------------------
    def open_session(
        self,
        device_key: str,
        mode: SessionMode,
        destination: Optional[StillDestination] = None,
    ) -> str:
        profile = self._profile_for(device_key)
        if mode not in profile.modes:
            raise UnsupportedOperationError(
                f"{profile.model} does not support control mode {mode.value!r}",
                capability=f"mode.{mode.value}",
                operation="open_session",
            )
        destination = destination or profile.default_destination
        if destination not in profile.destinations:
            raise UnsupportedOperationError(
                f"{profile.model} does not support destination {destination.value!r}",
                capability=f"destination.{destination.value}",
                operation="open_session",
            )

        now = self._clock.now_ms()
        session_id = f"sim-session-{next(self._session_counter)}"
        session = _Session(
            session_id, profile, self._scenario, mode, destination, now
        )
        self._sessions[session_id] = session
        session.ready.append(
            ConnectionEvent(timestamp_ms=now, state=ConnectionState.CONNECTING)
        )

        connect_at = now + profile.timings.connect

        def _connected() -> Sequence[Event]:
            session.state = ConnectionState.CONNECTED
            return (
                ConnectionEvent(
                    timestamp_ms=self._clock.now_ms(), state=ConnectionState.CONNECTED
                ),
            )

        session.schedule(connect_at, _connected)

        if self._scenario.reconnect_after_ms is not None:
            self._schedule_reconnect(
                session, connect_at + self._scenario.reconnect_after_ms
            )

        # Advance to the connected state so the caller receives a usable session.
        self._clock.sleep_ms(profile.timings.connect)
        session.ready.extend(session.drain_due(self._clock.now_ms()))
        return session_id

    def _schedule_reconnect(self, session: _Session, at_ms: int) -> None:
        scenario = session.scenario
        profile = session.profile

        def _begin() -> Sequence[Event]:
            session.state = ConnectionState.RECONNECTING
            events: list[Event] = [
                ConnectionEvent(
                    timestamp_ms=self._clock.now_ms(),
                    state=ConnectionState.RECONNECTING,
                )
            ]
            if not scenario.reconnect_without_disconnect:
                events.append(
                    ConnectionEvent(
                        timestamp_ms=self._clock.now_ms(), state=ConnectionState.CLOSED
                    )
                )
            return events

        def _recover() -> Sequence[Event]:
            session.state = ConnectionState.CONNECTED
            now = self._clock.now_ms()
            events: list[Event] = []
            if scenario.duplicate_connected_event:
                events.append(
                    ConnectionEvent(timestamp_ms=now, state=ConnectionState.CONNECTED)
                )
            events.append(
                ConnectionEvent(
                    timestamp_ms=now, state=ConnectionState.CONNECTED, recovered=True
                )
            )
            # Live view resumes on its own after recovery.
            session.next_frame_ms = now
            return events

        session.schedule(at_ms, _begin)
        session.schedule(at_ms + profile.timings.reconnect, _recover)

    def close_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return  # idempotent
        session.closed = True
        session.state = ConnectionState.CLOSED
        session.ready.append(
            ConnectionEvent(
                timestamp_ms=self._clock.now_ms(), state=ConnectionState.CLOSED
            )
        )

    def _session(self, session_id: str, operation: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionClosedError(
                f"unknown session {session_id!r}", operation=operation
            )
        if session.closed:
            raise SessionClosedError("session is closed", operation=operation)
        return session

    def _live_session(self, session_id: str, operation: str) -> _Session:
        session = self._session(session_id, operation)
        # Let time-based transitions land before judging the state.
        session.ready.extend(session.drain_due(self._clock.now_ms()))
        if session.state is ConnectionState.RECONNECTING:
            raise CameraConnectionError(
                "camera transport is recovering", operation=operation
            )
        if session.scenario.busy:
            raise CameraBusyError("camera is busy", operation=operation)
        return session

    def connection_state(self, session_id: str) -> ConnectionState:
        session = self._sessions.get(session_id)
        if session is None:
            return ConnectionState.CLOSED
        session.ready.extend(session.drain_due(self._clock.now_ms()))
        return session.state

    def session_capabilities(self, session_id: str) -> SessionCapabilities:
        session = self._session(session_id, "capabilities")
        return self._caps(session)

    def _caps(self, session: _Session) -> SessionCapabilities:
        return self._capabilities_for(
            session.profile, session.mode, session.destination
        )

    def _capabilities_for(
        self,
        profile: CameraProfile,
        mode: SessionMode,
        destination: StillDestination,
    ) -> SessionCapabilities:
        mode_profile = profile.modes[mode]
        delivery = mode_profile.postview_delivery_possible and (
            destination.includes_host
            if mode_profile.postview_delivery_requires_host
            else True
        )
        return SessionCapabilities(
            mode=mode,
            destination=destination,
            still_capture=profile.still_capture and mode_profile.still_capture,
            autofocus_s1=profile.autofocus_s1,
            video=profile.video and mode_profile.video,
            live_view=mode_profile.live_view,
            content_index=mode_profile.content_index,
            thumbnail=mode_profile.thumbnail,
            screennail=mode_profile.screennail,
            postview_configuration=mode_profile.postview_configuration,
            postview_delivery=delivery,
            raw_commands=True,
            extra=dict(mode_profile.extra_capabilities),
        )

    # -- events ------------------------------------------------------------
    def poll_events(self, session_id: str, timeout_ms: int = 0) -> Sequence[Event]:
        session = self._session(session_id, "poll_events")
        now = self._clock.now_ms()
        events = session.drain_due(now)
        if events or timeout_ms <= 0:
            return events

        deadline = now + int(timeout_ms)
        next_ms = session.next_scheduled_ms()
        if next_ms is None or next_ms > deadline:
            self._clock.sleep_ms(deadline - now)
        else:
            self._clock.sleep_ms(max(0, next_ms - now))
        return session.drain_due(self._clock.now_ms())

    # -- properties --------------------------------------------------------
    def list_properties(self, session_id: str) -> Sequence[Property]:
        session = self._live_session(session_id, "list_properties")
        return [
            P.build_property(code, value)
            for code, value in sorted(session.properties.items())
        ]

    def get_property(self, session_id: str, code: PropertyCode) -> Property:
        session = self._live_session(session_id, "get_property")
        key = int(code)
        if key not in session.properties:
            raise PropertyNotSupportedError(
                f"camera does not expose property {PropertyCode(key)}", code=key
            )
        return P.build_property(key, session.properties[key])

    def set_property(self, session_id: str, code: PropertyCode, value: Any) -> None:
        session = self._live_session(session_id, "set_property")
        key = int(code)
        if key not in session.properties:
            raise PropertyNotSupportedError(
                f"camera does not expose property {PropertyCode(key)}", code=key
            )
        prop = P.build_property(key, session.properties[key])
        if not prop.writable:
            raise UnsupportedOperationError(
                f"property {PropertyCode(key)} is read-only",
                capability="property.write",
                operation="set_property",
            )
        session.properties[key] = value
        now = self._clock.now_ms()
        session.ready.append(
            PropertyChangedEvent(timestamp_ms=now, codes=(PropertyCode(key),))
        )
        if key == P.CODE_S1:
            if value == P.LOCK_LOCKED:
                self._begin_autofocus(session)
            else:
                self._cancel_autofocus(session)

    # -- autofocus ---------------------------------------------------------
    def _focus_mode_is_af_c(self, session: _Session) -> bool:
        return session.properties.get(P.CODE_FOCUS_MODE) == _FOCUS_MODE_AF_C

    def _begin_autofocus(self, session: _Session) -> None:
        scenario = session.scenario
        timings = session.profile.timings
        now = self._clock.now_ms()
        af_c = self._focus_mode_is_af_c(session)

        if not session.profile.autofocus_s1:
            return

        if scenario.af_sticky:
            # Already focused, and no change notification will ever fire.
            # Only a direct read can discover this.
            session.properties[P.CODE_FOCUS_INDICATION] = (
                P.FOCUS_FOCUSED_AF_C if af_c else P.FOCUS_FOCUSED_AF_S
            )
            return

        if scenario.af_outcome is AfOutcome.SILENT:
            return

        skew = (
            scenario.af_channel_skew_ms
            if scenario.af_channel_skew_ms is not None
            else timings.focus_channel_skew
        )

        if scenario.af_outcome is AfOutcome.NO_LOCK:
            value = P.FOCUS_NOT_FOCUSED_AF_C if af_c else P.FOCUS_NOT_FOCUSED_AF_S
            self._schedule_focus(session, now + timings.focus_fail, value, skew)
            return

        if scenario.af_outcome is AfOutcome.TRACKING_THEN_FOCUS or af_c:
            self._schedule_focus(
                session, now + timings.focus_tracking, P.FOCUS_TRACKING_AF_C, skew
            )
            self._schedule_focus(
                session, now + timings.focus_confirm, P.FOCUS_FOCUSED_AF_C, skew
            )
            return

        self._schedule_focus(
            session, now + timings.focus_confirm, P.FOCUS_FOCUSED_AF_S, skew
        )

    def _schedule_focus(
        self, session: _Session, at_ms: int, vendor_value: int, skew_ms: int
    ) -> None:
        """Publish one focus state on both channels, with a deliberate skew.

        The channels are independent and neither is reliably first, so which
        one leads is scenario-controlled.
        """
        state = _FOCUS_VALUE_TO_STATE.get(vendor_value, FocusState.UNKNOWN)
        lead_is_property = session.scenario.af_leading_channel is FocusChannel.PROPERTY
        property_at = at_ms if lead_is_property else at_ms + skew_ms
        status_at = at_ms + skew_ms if lead_is_property else at_ms

        def _property_channel() -> Sequence[Event]:
            session.properties[P.CODE_FOCUS_INDICATION] = vendor_value
            now = self._clock.now_ms()
            return (
                PropertyChangedEvent(
                    timestamp_ms=now, codes=(PropertyCode(P.CODE_FOCUS_INDICATION),)
                ),
                FocusEvent(
                    timestamp_ms=now,
                    state=state,
                    source=FocusSource.PROPERTY,
                    raw_value=vendor_value,
                ),
            )

        def _status_channel() -> Sequence[Event]:
            return (
                FocusEvent(
                    timestamp_ms=self._clock.now_ms(),
                    state=state,
                    source=FocusSource.STATUS_WARNING,
                    raw_value=vendor_value,
                ),
            )

        session.schedule(property_at, _property_channel)
        session.schedule(status_at, _status_channel)

    def _cancel_autofocus(self, session: _Session) -> None:
        session.properties[P.CODE_FOCUS_INDICATION] = P.FOCUS_UNLOCKED

    # -- shutter stages and focus -----------------------------------------
    def get_half_press(self, session_id: str) -> bool:
        session = self._live_session(session_id, "get_half_press")
        if P.CODE_S1 not in session.properties:
            return False
        return session.properties[P.CODE_S1] == P.LOCK_LOCKED

    def set_half_press(self, session_id: str, engaged: bool) -> None:
        session = self._live_session(session_id, "set_half_press")
        if not session.profile.autofocus_s1 or P.CODE_S1 not in session.properties:
            raise UnsupportedOperationError(
                f"{session.profile.model} has no half-press stage",
                capability="autofocus_s1",
                operation="set_half_press",
            )
        self.set_property(
            session_id,
            PropertyCode(P.CODE_S1),
            P.LOCK_LOCKED if engaged else P.LOCK_UNLOCKED,
        )

    def focus_state(self, session_id: str) -> FocusState:
        session = self._live_session(session_id, "focus_state")
        raw = session.properties.get(P.CODE_FOCUS_INDICATION)
        if raw is None:
            return FocusState.UNKNOWN
        return _FOCUS_VALUE_TO_STATE.get(raw, FocusState.UNKNOWN)

    # -- commands ----------------------------------------------------------
    def send_command(
        self, session_id: str, command: CommandLike, parameter: CommandParameter
    ) -> None:
        session = self._live_session(session_id, "send_command")
        if command is Command.RELEASE:
            self._handle_release(session, parameter, gated=True)
        elif command is Command.S1_AND_RELEASE:
            self._handle_s1_and_release(session, parameter)
        elif command is Command.MOVIE_RECORD:
            if parameter is CommandParameter.DOWN:
                self._begin_recording(session)
        # Unknown / raw integer commands are accepted and do nothing, which
        # matches a camera that ignores a command it does not implement.

    def _handle_release(
        self, session: _Session, parameter: CommandParameter, *, gated: bool
    ) -> None:
        if parameter is CommandParameter.DOWN:
            self._schedule_exposure(session, self._clock.now_ms())
            return
        # UP: a successful release clears the half-press stage by itself on
        # the characterized body, but that is not universal.
        if session.scenario.release_clears_s1:
            session.properties[P.CODE_S1] = P.LOCK_UNLOCKED
            session.properties[P.CODE_FOCUS_INDICATION] = P.FOCUS_UNLOCKED

    def _handle_s1_and_release(
        self, session: _Session, parameter: CommandParameter
    ) -> None:
        if parameter is CommandParameter.DOWN:
            # Ungated: autofocus runs and the exposure follows regardless of
            # the focus outcome. There is no point at which a caller can abort.
            self._begin_autofocus(session)
            delay = session.profile.timings.focus_confirm
            self._schedule_exposure(session, self._clock.now_ms() + delay)
            return
        if session.scenario.release_clears_s1:
            session.properties[P.CODE_S1] = P.LOCK_UNLOCKED
            session.properties[P.CODE_FOCUS_INDICATION] = P.FOCUS_UNLOCKED

    def _schedule_exposure(self, session: _Session, from_ms: int) -> None:
        scenario = session.scenario
        timings = session.profile.timings
        if scenario.capture_without_exposure:
            # The command was accepted. Nothing else will ever happen.
            return

        exposure_at = from_ms + timings.exposure
        session.live_view_blocked_until = exposure_at + timings.live_view_gap
        # Fixed up front so the postview and the content record agree even
        # though the postview lands first.
        expected_content_id = session.next_content_id + scenario.content_id_step

        def _exposed() -> Sequence[Event]:
            session.capture_sequence += 1
            return (
                CaptureEvent(
                    timestamp_ms=self._clock.now_ms(),
                    sequence=session.capture_sequence,
                ),
            )

        session.schedule(exposure_at, _exposed)

        content_at = exposure_at + timings.content + scenario.content_extra_delay_ms

        def _content() -> Sequence[Event]:
            session.next_content_id = expected_content_id
            content_id = expected_content_id
            number = 3400 + len(session.contents)
            ref = ContentRef(
                content_id=content_id,
                file_id=1,
                file_number=number,
                path=f"A:/DCIM/100SIM/DSC{number:05d}.ARW",
                created_ms=self._clock.now_ms(),
                content_type=1,
                file_format=0xB101,
                width=4240,
                height=2832,
                file_size=24_000_000,
                slot=1,
            )
            session.contents.append(ref)
            return (
                ContentEvent(
                    timestamp_ms=self._clock.now_ms(),
                    content_id=ref.content_id,
                    file_number=ref.file_number,
                    path=ref.path,
                ),
            )

        session.schedule(content_at, _content)

        caps = self._caps(session)
        if caps.postview_delivery:
            postview_at = from_ms + timings.postview

            def _postview() -> Sequence[Event]:
                session.postview_pending = expected_content_id
                return ()

            session.schedule(postview_at, _postview)

        if scenario.emit_unknown_events:
            session.schedule(
                exposure_at + 5,
                lambda: (
                    UnknownEvent(
                        timestamp_ms=self._clock.now_ms(),
                        code=0xDEAD,
                        payload={"note": "vendor event with no typed form"},
                    ),
                ),
            )

    # -- device status -----------------------------------------------------
    def battery(self, session_id: str) -> BatteryStatus:
        session = self._live_session(session_id, "battery")
        # Drains by one point per exposure, so a client that watches the
        # battery sees it move rather than sitting at a constant.
        percent = max(0, session.profile.battery_percent - session.capture_sequence)
        return BatteryStatus(
            percent=percent,
            level=round(percent / 100, 2),
            usb_power=session.profile.usb_power,
            raw_level=None,
        )

    def storage(self, session_id: str) -> Sequence[StorageSlot]:
        session = self._live_session(session_id, "storage")
        slots = []
        for number, present in enumerate(session.profile.slots, start=1):
            if not present:
                slots.append(StorageSlot(slot=number, status="no_card"))
                continue
            slots.append(
                StorageSlot(
                    slot=number,
                    status="ok",
                    remaining_shots=max(
                        0, session.profile.remaining_shots - len(session.contents)
                    ),
                    remaining_seconds=session.profile.remaining_seconds,
                    raw_status=0,
                )
            )
        return tuple(slots)

    # -- destination -------------------------------------------------------
    def get_destination(self, session_id: str) -> StillDestination:
        return self._session(session_id, "get_destination").destination

    def set_destination(self, session_id: str, destination: StillDestination) -> None:
        session = self._live_session(session_id, "set_destination")
        if destination not in session.profile.destinations:
            raise UnsupportedOperationError(
                f"{session.profile.model} does not support destination "
                f"{destination.value!r}",
                capability=f"destination.{destination.value}",
                operation="set_destination",
            )
        session.destination = destination
        session.properties[P.CODE_DESTINATION] = _Session._initial_value(
            P.CODE_DESTINATION, destination
        )
        session.ready.append(
            PropertyChangedEvent(
                timestamp_ms=self._clock.now_ms(),
                codes=(PropertyCode(P.CODE_DESTINATION),),
            )
        )

    # -- live view ---------------------------------------------------------
    def live_view_info(self, session_id: str) -> LiveViewInfo:
        session = self._live_session(session_id, "live_view_info")
        caps = self._caps(session)
        lv = session.profile.live_view
        if not caps.live_view:
            # The vendor info call reports success with a zero buffer, then the
            # frame fetch fails hard. Reporting success is not availability.
            return LiveViewInfo(info_ok=True, buffer_size=0, width=0, height=0)
        if session.scenario.live_view_info_ok_but_empty:
            return LiveViewInfo(info_ok=True, buffer_size=0, width=0, height=0)
        return LiveViewInfo(
            info_ok=True,
            width=lv.width,
            height=lv.height,
            buffer_size=lv.buffer_size,
        )

    def get_live_view_frame(self, session_id: str) -> Optional[LiveViewFrame]:
        session = self._live_session(session_id, "get_live_view_frame")
        caps = self._caps(session)
        if not caps.live_view:
            raise UnsupportedOperationError(
                f"live view is not available in control mode {session.mode.value!r} "
                f"on {session.profile.model}",
                capability="live_view",
                operation="get_live_view_frame",
            )
        if session.scenario.live_view_fetch_fails:
            raise CameraConnectionError(
                "live view frame fetch failed", operation="get_live_view_frame"
            )

        now = self._clock.now_ms()
        if now < session.live_view_blocked_until:
            return None  # the stream pauses around the exposure
        if now < session.next_frame_ms:
            return None

        lv = session.profile.live_view
        interval = max(1, int(1000.0 / max(lv.frames_per_second, 0.001)))
        session.next_frame_ms = now + interval
        session.live_view_frame_no += 1

        rnd = random.Random(session.live_view_frame_no)
        size = max(1024, lv.byte_length + rnd.randint(-lv.byte_jitter, lv.byte_jitter))
        return LiveViewFrame(
            data=_synth_image(
                session.live_view_frame_no, size, lv.width, lv.height
            ),
            width=lv.width,
            height=lv.height,
            timestamp_ms=now,
            frame_number=session.live_view_frame_no,
        )

    # -- previews and content ---------------------------------------------
    def configure_postview(
        self, session_id: str, *, enabled: bool, transfer_to_ram: bool = True
    ) -> None:
        session = self._live_session(session_id, "configure_postview")
        caps = self._caps(session)
        if not caps.postview_configuration:
            raise UnsupportedOperationError(
                "postview configuration is rejected in control mode "
                f"{session.mode.value!r} on {session.profile.model}; note that "
                "delivery may still work",
                capability="postview_configuration",
                operation="configure_postview",
            )
        session.postview_configured = bool(enabled)

    def pull_postview(self, session_id: str) -> Optional[Preview]:
        session = self._live_session(session_id, "pull_postview")
        caps = self._caps(session)
        if not caps.postview_delivery:
            raise UnsupportedOperationError(
                "postview is not delivered with destination "
                f"{session.destination.value!r} in control mode "
                f"{session.mode.value!r}",
                capability="postview_delivery",
                operation="pull_postview",
            )
        if session.postview_pending is None:
            return None
        content_id = session.postview_pending
        session.postview_pending = None
        return self._make_preview(session, PreviewKind.POSTVIEW, content_id)

    def latest_content(self, session_id: str) -> Optional[ContentRef]:
        session = self._live_session(session_id, "latest_content")
        self._require_content_index(session, "latest_content")
        return session.contents[-1] if session.contents else None

    def list_content(
        self, session_id: str, *, newer_than: Optional[int] = None
    ) -> Sequence[ContentRef]:
        session = self._live_session(session_id, "list_content")
        self._require_content_index(session, "list_content")
        if newer_than is None:
            return tuple(session.contents)
        return tuple(c for c in session.contents if c.content_id > newer_than)

    def _require_content_index(self, session: _Session, operation: str) -> None:
        caps = self._caps(session)
        if not caps.content_index:
            raise UnsupportedOperationError(
                "the content index is not available in control mode "
                f"{session.mode.value!r} on {session.profile.model}",
                capability="content_index",
                operation=operation,
            )

    def get_preview(
        self, session_id: str, content_id: int, kind: PreviewKind
    ) -> Preview:
        session = self._live_session(session_id, "get_preview")
        caps = self._caps(session)
        capability = {
            PreviewKind.THUMBNAIL: "thumbnail",
            PreviewKind.SCREENNAIL: "screennail",
            PreviewKind.POSTVIEW: "postview_delivery",
        }.get(kind, "content_index")
        if not caps.get(capability):
            raise UnsupportedOperationError(
                f"{kind.value} is not available in control mode "
                f"{session.mode.value!r} on {session.profile.model}",
                capability=capability,
                operation="get_preview",
            )
        served = content_id
        stale = session.scenario.stale_preview
        if stale and session.last_delivered_content is not None:
            # Serve the previous shot's bytes. A client that does not verify
            # identity will silently show the wrong image.
            served = session.last_delivered_content
        session.last_delivered_content = content_id
        return self._make_preview(session, kind, served)

    def _make_preview(
        self, session: _Session, kind: PreviewKind, content_id: int
    ) -> Preview:
        spec = session.profile.previews.get(kind)
        if spec is None:
            spec = P.PreviewSpec(640, 480, 50_000)
        # Seeded by content id and a stable per-kind salt, so distinct captures
        # produce distinct bytes and results repeat across processes.
        salt = {
            PreviewKind.LIVE_VIEW: 11,
            PreviewKind.POSTVIEW: 23,
            PreviewKind.THUMBNAIL: 37,
            PreviewKind.SCREENNAIL: 51,
        }[kind]
        data = _synth_image(
            content_id * 31 + salt, spec.byte_length, spec.width, spec.height
        )
        source = next(
            (c for c in session.contents if c.content_id == content_id), None
        )
        metadata: dict[str, object] = {
            "file_id": source.file_id if source else 0,
            "slot": 1,
            "exact_still_association": "content_id",
            "byte_length": len(data),
        }
        if source is not None:
            metadata["path"] = source.path
            metadata["filename"] = source.filename
            metadata["file_number"] = source.file_number
            metadata["content_type"] = source.content_type
            metadata["file_format"] = source.file_format
        return Preview(
            kind=kind,
            data=data,
            mime=spec.mime,
            width=spec.width,
            height=spec.height,
            timestamp_ms=self._clock.now_ms(),
            content_id=content_id,
            metadata=metadata,
        )

    # -- video -------------------------------------------------------------
    def _begin_recording(self, session: _Session) -> None:
        if session.recording in (RecordingState.RECORDING, RecordingState.STARTING):
            return
        session.recording = RecordingState.STARTING
        now = self._clock.now_ms()
        session.ready.append(
            RecordingEvent(timestamp_ms=now, state=RecordingState.STARTING)
        )

        def _active() -> Sequence[Event]:
            session.recording = RecordingState.RECORDING
            session.properties[P.CODE_RECORDING_STATE] = 0x0001
            return (
                RecordingEvent(
                    timestamp_ms=self._clock.now_ms(), state=RecordingState.RECORDING
                ),
            )

        session.schedule(now + session.profile.timings.recording_start, _active)

    def start_recording(self, session_id: str) -> None:
        session = self._live_session(session_id, "start_recording")
        caps = self._caps(session)
        if not caps.video:
            raise UnsupportedOperationError(
                f"{session.profile.model} does not support movie recording",
                capability="video",
                operation="start_recording",
            )
        self._begin_recording(session)

    def stop_recording(self, session_id: str) -> None:
        session = self._live_session(session_id, "stop_recording")
        caps = self._caps(session)
        if not caps.video:
            raise UnsupportedOperationError(
                f"{session.profile.model} does not support movie recording",
                capability="video",
                operation="stop_recording",
            )
        if session.recording is RecordingState.IDLE:
            return
        session.recording = RecordingState.STOPPING
        now = self._clock.now_ms()
        session.ready.append(
            RecordingEvent(timestamp_ms=now, state=RecordingState.STOPPING)
        )

        def _idle() -> Sequence[Event]:
            session.recording = RecordingState.IDLE
            session.properties[P.CODE_RECORDING_STATE] = 0x0000
            return (
                RecordingEvent(
                    timestamp_ms=self._clock.now_ms(), state=RecordingState.IDLE
                ),
            )

        session.schedule(now + session.profile.timings.recording_stop, _idle)

    def recording_state(self, session_id: str) -> RecordingState:
        session = self._session(session_id, "recording_state")
        session.ready.extend(session.drain_due(self._clock.now_ms()))
        return session.recording

    # -- simulation control ------------------------------------------------
    def simulate_physical_property_change(
        self,
        session_id: str,
        codes: Sequence[int],
        values: Optional[Mapping[int, Any]] = None,
    ) -> None:
        """Model an operator touching a physical control.

        Related codes arrive as one coalesced batch, with an optional straggler
        afterwards, exactly as observed when the focus-mode switch was moved.
        """
        session = self._live_session(session_id, "simulate_physical_property_change")
        for code, value in (values or {}).items():
            if int(code) in session.properties:
                session.properties[int(code)] = value
        now = self._clock.now_ms()
        batch = tuple(PropertyCode(c) for c in codes if int(c) in session.properties)
        if batch:
            session.ready.append(PropertyChangedEvent(timestamp_ms=now, codes=batch))
        if session.scenario.property_stragglers and len(batch) > 1:
            straggler = batch[-1]
            session.schedule(
                now + session.profile.timings.property_straggler,
                lambda: (
                    PropertyChangedEvent(
                        timestamp_ms=self._clock.now_ms(), codes=(straggler,)
                    ),
                ),
            )

    def simulate_reconnect(self, session_id: str, *, delay_ms: int = 0) -> None:
        """Trigger a transport loss and recovery."""
        session = self._session(session_id, "simulate_reconnect")
        self._schedule_reconnect(session, self._clock.now_ms() + delay_ms)

    # -- extension point ---------------------------------------------------
    def raw_call(
        self, session_id: str, operation: str, payload: Mapping[str, Any]
    ) -> Any:
        session = self._live_session(session_id, f"raw_call.{operation}")
        if operation == "echo":
            return dict(payload)
        if operation == "lens_information":
            # Stands in for the vendor feature families that are neither
            # property- nor command-shaped.
            return [
                {
                    "type": "simulated",
                    "focal_length_mm": 24,
                    "focus_position": 512,
                    "model": session.profile.model,
                }
            ]
        raise UnsupportedOperationError(
            f"simulator does not implement raw operation {operation!r}",
            capability=f"raw.{operation}",
            operation="raw_call",
        )
