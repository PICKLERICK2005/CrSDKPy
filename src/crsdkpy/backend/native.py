"""Native Sony backend.

    Sony CRSDK C++
      -> thin native C++ shared library
      -> strict C ABI
      -> this ctypes layer
      -> CrSDKPy public API

Implements the whole backend contract: discovery, sessions, connection state,
events, property reads and writes, generic commands, still capture, gated
autofocus, the content index with exact-still thumbnails and screennails, RAM
postview, live view, movie recording, battery and media status.

The public API and its test suite are unchanged, so every feature is validated
against the same contract the simulator already satisfies. Which of them have
been exercised against real hardware is recorded in the documentation, not
here: an implemented feature and a validated one are different claims.

The Sony Camera Remote SDK is never bundled. Users supply their own copy and
build ``native/`` against it.
"""

from __future__ import annotations

import atexit
import ctypes
import os
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .._jpeg import is_jpeg, jpeg_dimensions
from ..capabilities import CameraCapabilities, SessionCapabilities
from ..clock import Clock, RealClock
from ..commands import Command, CommandParameter
from ..enums import (
    ConnectionState,
    FocusState,
    PreviewKind,
    PropertyAccess,
    PropertyValueType,
    RecordingState,
    SessionMode,
    StillDestination,
    TransferOutcome,
)
from ..errors import (
    CameraConnectionError,
    NativeBackendError,
    PropertyNotSupportedError,
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
    TransferEvent,
    UnknownEvent,
    WarningEvent,
)
from ..previews import LiveViewFrame, Preview
from ..properties import Property, PropertyCode
from ..status import BatteryStatus, StorageSlot
from . import _cabi
from .contract import Backend, BackendCameraInfo, ContentRef, LiveViewInfo

__all__ = ["NativeBackend", "native_backend_available"]

_MODE_TO_ABI = {
    SessionMode.REMOTE: 0,
    SessionMode.CONTENTS_TRANSFER: 1,
    SessionMode.REMOTE_TRANSFER: 2,
}

_ABI_TO_STATE = {
    0: ConnectionState.CONNECTING,
    1: ConnectionState.CONNECTED,
    2: ConnectionState.RECONNECTING,
    3: ConnectionState.CLOSING,
    4: ConnectionState.CLOSED,
}

_ABI_TO_TRANSFER_OUTCOME = {
    _cabi.TRANSFER_IN_PROGRESS: TransferOutcome.IN_PROGRESS,
    _cabi.TRANSFER_OK: TransferOutcome.OK,
    _cabi.TRANSFER_FAILED: TransferOutcome.FAILED,
    _cabi.TRANSFER_BUSY: TransferOutcome.BUSY,
    _cabi.TRANSFER_STORAGE_FULL: TransferOutcome.STORAGE_FULL,
    _cabi.TRANSFER_STOPPED: TransferOutcome.STOPPED,
    _cabi.TRANSFER_CANCELED: TransferOutcome.CANCELED,
    _cabi.TRANSFER_UNKNOWN: TransferOutcome.UNKNOWN,
}

_ABI_TO_ACCESS = {
    0: PropertyAccess.UNKNOWN,
    1: PropertyAccess.READ_ONLY,
    2: PropertyAccess.WRITE_ONLY,
    3: PropertyAccess.READ_WRITE,
}

_ABI_TO_VALUE_TYPE = {
    0: PropertyValueType.UNKNOWN,
    1: PropertyValueType.INT,
    2: PropertyValueType.STRING,
    3: PropertyValueType.INT_ARRAY,
}

#: Vendor focus-indication values, as reported by the property channel.
_FOCUS_INDICATION = {
    0x0001: FocusState.UNLOCKED,
    0x0102: FocusState.FOCUSED_AF_S,
    0x0202: FocusState.NOT_FOCUSED_AF_S,
    0x0103: FocusState.FOCUSED_AF_C,
    0x0203: FocusState.NOT_FOCUSED_AF_C,
    0x0303: FocusState.TRACKING_AF_C,
}

#: The AF-status warning channel uses a *different* enumeration for the same
#: states. Decoding them with one table would be wrong.
_FOCUS_AF_STATUS = {
    1: FocusState.UNLOCKED,
    2: FocusState.FOCUSED_AF_S,
    3: FocusState.NOT_FOCUSED_AF_S,
    5: FocusState.TRACKING_AF_C,
    6: FocusState.FOCUSED_AF_C,
    7: FocusState.NOT_FOCUSED_AF_C,
}

def native_backend_available(path: Optional[str] = None) -> bool:
    """Whether a built bridge can be found and loaded."""
    try:
        _cabi.load_bridge(path)
    except Exception:
        return False
    return True



#: Public command -> vendor command id. Raw integers pass through untouched, so
#: a command CrSDKPy has never heard of is still reachable via session.raw.
_COMMAND_TO_ABI = {
    Command.RELEASE: 0,
    Command.MOVIE_RECORD: 1,
    Command.CANCEL_SHOOTING: 2,
    Command.S1_AND_RELEASE: 7,
}


def command_to_abi(command: Any) -> int:
    """Resolve a public command or raw vendor integer to a numeric id."""
    if isinstance(command, Command):
        try:
            return _COMMAND_TO_ABI[command]
        except KeyError:
            raise UnsupportedOperationError(
                f"no vendor id is mapped for {command}",
                capability=f"command.{command.value}",
                operation="send_command",
            ) from None
    return int(command)


def parameter_to_abi(parameter: Any) -> int:
    if isinstance(parameter, CommandParameter):
        return int(parameter.value)
    return int(parameter)



# Vendor codes and values. Backend-level knowledge on purpose: the public
# layer must never need to know a vendor code.
CODE_S1 = 0x0001
CODE_FOCUS_INDICATION = 0x0707
LOCK_UNLOCKED = 1
LOCK_LOCKED = 2

CODE_DESTINATION = 0x0119
CODE_BATTERY_REMAIN = 0x0702
CODE_BATTERY_LEVEL = 0x0703
CODE_RECORDING_STATE = 0x0705

#: Still destination, as the vendor numbers it.
_DESTINATION_TO_ABI = {
    StillDestination.HOST: 1,
    StillDestination.MEMORY_CARD: 2,
    StillDestination.HOST_AND_MEMORY_CARD: 3,
}
_ABI_TO_DESTINATION = {value: key for key, value in _DESTINATION_TO_ABI.items()}

#: Recording state. The interval-record wait is an active recording session
#: from a caller's point of view, so it is not reported as idle.
_ABI_TO_RECORDING = {
    0: RecordingState.IDLE,
    1: RecordingState.RECORDING,
    2: RecordingState.FAILED,
    3: RecordingState.RECORDING,
}

#: Coarse battery levels. The vendor expresses them as fractions with two
#: different denominators, so they are normalised rather than passed through.
_BATTERY_LEVEL = {
    1: 0.0,        # pre-end
    2: 0.25, 3: 0.50, 4: 0.75, 5: 1.0,
    6: 1 / 3, 7: 2 / 3, 8: 1.0,
}
_BATTERY_USB_POWER = 0x00010000

_SLOT_STATUS = {
    0: "ok",
    1: "no_card",
    2: "card_error",
    3: "locked_or_recognising",
    4: "database_error",
    5: "recognising",
    6: "locked_and_database_error",
}

#: Per-slot property codes. Slot 2 does not mirror slot 1's field order, so
#: the codes are listed rather than computed from an offset.
_SLOT_CODES = {
    1: {"status": 0x0708, "shots": 0x0709, "seconds": 0x070A},
    2: {"status": 0x070D, "shots": 0x070F, "seconds": 0x0710},
}

#: States in which pressing the movie-record button would *stop* a recording.
_ACTIVE_RECORDING = (RecordingState.RECORDING, RecordingState.STARTING)

#: Dwell between the movie-record button going down and coming back up.
_RECORD_PRESS_DWELL_MS = 35


class ShutterStageMixin:
    """Half-press and focus reads expressed through property access.

    Both are ordinary properties on this vendor SDK, so no extra ABI surface is
    needed. Kept in one place so the in-process and hosted backends cannot
    drift apart.
    """

    def get_half_press(self, session_id: str) -> bool:
        prop = self.get_property(session_id, PropertyCode(CODE_S1))
        return int(prop.value) == LOCK_LOCKED

    def set_half_press(self, session_id: str, engaged: bool) -> None:
        self.set_property(
            session_id,
            PropertyCode(CODE_S1),
            LOCK_LOCKED if engaged else LOCK_UNLOCKED,
        )

    def focus_state(self, session_id: str) -> FocusState:
        try:
            prop = self.get_property(session_id, PropertyCode(CODE_FOCUS_INDICATION))
        except PropertyNotSupportedError:
            return FocusState.UNKNOWN
        return _FOCUS_INDICATION.get(int(prop.value), FocusState.UNKNOWN)


class DeviceStatusMixin:
    """Destination, battery and media, all expressed through properties.

    No extra ABI surface: these are ordinary property reads and writes on this
    vendor SDK. Shared so the in-process and hosted backends cannot drift.
    """

    def _optional_value(self, session_id: str, code: int) -> Optional[int]:
        """Read a property, or ``None`` when the camera does not expose it."""
        try:
            return int(self.get_property(session_id, PropertyCode(code)).value)
        except PropertyNotSupportedError:
            return None

    # -- destination -------------------------------------------------------
    def get_destination(self, session_id: str) -> StillDestination:
        value = self._optional_value(session_id, CODE_DESTINATION)
        if value is None:
            # A body with no selectable destination writes to its card.
            return StillDestination.MEMORY_CARD
        return _ABI_TO_DESTINATION.get(value, StillDestination.MEMORY_CARD)

    def set_destination(
        self, session_id: str, destination: StillDestination
    ) -> None:
        try:
            abi = _DESTINATION_TO_ABI[destination]
        except KeyError:
            raise UnsupportedOperationError(
                f"unknown still destination {destination!r}",
                capability="destination",
                operation="set_destination",
            ) from None
        self.set_property(session_id, PropertyCode(CODE_DESTINATION), abi)
        self._invalidate_capabilities(session_id)

    def _invalidate_capabilities(self, session_id: str) -> None:
        """Hook: destination changes what a session can do."""

    def _apply_destination(
        self, session_id: str, destination: StillDestination
    ) -> None:
        """Set the destination when the camera is not already there.

        Skipping the write when it would be a no-op keeps a body that has no
        selectable destination usable: asking for the one it already uses is
        not a request it has to be able to honour.
        """
        if self.get_destination(session_id) is destination:
            return
        self.set_destination(session_id, destination)

    # -- battery -----------------------------------------------------------
    def battery(self, session_id: str) -> BatteryStatus:
        percent = self._optional_value(session_id, CODE_BATTERY_REMAIN)
        raw_level = self._optional_value(session_id, CODE_BATTERY_LEVEL)
        usb_power = raw_level == _BATTERY_USB_POWER
        level = None if raw_level is None else _BATTERY_LEVEL.get(raw_level)
        # A percentage outside 0-100 is not a percentage; report it as absent
        # rather than passing a nonsense number upward.
        if percent is not None and not 0 <= percent <= 100:
            percent = None
        return BatteryStatus(
            percent=percent,
            level=level,
            usb_power=usb_power,
            raw_level=raw_level,
        )

    # -- storage -----------------------------------------------------------
    def storage(self, session_id: str) -> Sequence[StorageSlot]:
        slots = []
        for number, codes in _SLOT_CODES.items():
            raw_status = self._optional_value(session_id, codes["status"])
            if raw_status is None:
                continue  # this body has no such slot
            slots.append(
                StorageSlot(
                    slot=number,
                    status=_SLOT_STATUS.get(raw_status, "unknown"),
                    remaining_shots=self._optional_value(session_id, codes["shots"]),
                    remaining_seconds=self._optional_value(
                        session_id, codes["seconds"]
                    ),
                    raw_status=raw_status,
                )
            )
        return tuple(slots)


class VideoStageMixin:
    """Movie recording expressed through a command and a property.

    The vendor's movie-record command is a button press, so it toggles. That
    makes a blind second start a stop, which is why both start and stop check
    the observed state first rather than trusting the caller's intent.
    """

    def recording_state(self, session_id: str) -> RecordingState:
        try:
            prop = self.get_property(session_id, PropertyCode(CODE_RECORDING_STATE))
        except PropertyNotSupportedError:
            raise UnsupportedOperationError(
                "this camera does not report a recording state, so movie "
                "recording cannot be driven safely",
                capability="video",
                operation="recording_state",
            ) from None
        return _ABI_TO_RECORDING.get(int(prop.value), RecordingState.IDLE)

    def _press_record(self, session_id: str) -> None:
        self.send_command(session_id, Command.MOVIE_RECORD, CommandParameter.DOWN)
        self.clock.sleep_ms(_RECORD_PRESS_DWELL_MS)
        self.send_command(session_id, Command.MOVIE_RECORD, CommandParameter.UP)

    def start_recording(self, session_id: str) -> None:
        if self.recording_state(session_id) in _ACTIVE_RECORDING:
            return  # already running; pressing again would stop it
        self._press_record(session_id)

    def stop_recording(self, session_id: str) -> None:
        if self.recording_state(session_id) not in _ACTIVE_RECORDING:
            return  # already stopped; pressing again would start a recording
        self._press_record(session_id)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


# Shared by the in-process and hosted backends: both receive the same POD.
def decode_camera_info(info: _cabi.CameraInfoStruct) -> BackendCameraInfo:
    # These describe what the bridge can ask this transport for, not what a
    # particular body will agree to. Nothing here is guessed from a model
    # name, and nothing here is a promise: a camera that refuses one of these
    # says so when asked, and the session's own capabilities - which are
    # measured against the open connection - are what a caller should test.
    capabilities = CameraCapabilities(
        still_capture=True,
        autofocus_s1=True,
        video=True,
        modes=frozenset({SessionMode.REMOTE, SessionMode.REMOTE_TRANSFER}),
        destinations=frozenset(StillDestination),
        # "In *some* mode", which is the question these two answer. Whether a
        # given session has them is measured; see session_capabilities.
        live_view_any_mode=True,
        content_index_any_mode=True,
        extra={"native": True},
    )
    return BackendCameraInfo(
        device_key=_decode(info.device_key),
        model=_decode(info.model),
        serial=_decode(info.serial) or None,
        firmware=_decode(info.firmware) or None,
        transport=_decode(info.transport) or "unknown",
        adapter=_decode(info.adapter) or None,
        usb_pid=info.usb_pid if info.usb_pid >= 0 else None,
        capabilities=capabilities,
        metadata={"backend": "native", "phase": 1},
    )

# -- sessions ----------------------------------------------------------

def decode_event(
    raw: _cabi.EventStruct, *, timestamp_ms: Optional[int] = None
) -> Event:
    # The bridge stamps events on its own monotonic clock, which shares no
    # origin with the backend clock the public layer measures against.
    # Callers pass their own observation time so one domain is used
    # throughout; ordering within a batch is preserved by list order.
    stamp = int(raw.timestamp_ms) if timestamp_ms is None else int(timestamp_ms)
    if raw.kind == _cabi.EVENT_CONNECTION:
        return ConnectionEvent(
            timestamp_ms=stamp,
            state=_ABI_TO_STATE.get(raw.i0, ConnectionState.CLOSED),
            recovered=bool(raw.i1),
            backend_code=int(raw.code) or None,
            connection_version=int(raw.i2) or None,
        )
    if raw.kind == _cabi.EVENT_TRANSFER:
        return TransferEvent(
            timestamp_ms=stamp,
            outcome=_ABI_TO_TRANSFER_OUTCOME.get(
                raw.i1, TransferOutcome.UNKNOWN
            ),
            percent=int(raw.i0),
            # Kept even when the outcome is recognised: deciding what to retry
            # is the caller's business and they may want the exact code.
            notify_code=int(raw.code) or None,
            has_path=bool(raw.i2),
        )
    if raw.kind == _cabi.EVENT_PROPERTY_CHANGED:
        return PropertyChangedEvent(
            timestamp_ms=stamp, codes=(PropertyCode(int(raw.code)),)
        )
    if raw.kind == _cabi.EVENT_FOCUS:
        if raw.i1 == _cabi.FOCUS_SRC_WARNING:
            state = _FOCUS_AF_STATUS.get(int(raw.i0), FocusState.UNKNOWN)
            source = FocusSource.STATUS_WARNING
        else:
            state = _FOCUS_INDICATION.get(int(raw.i0), FocusState.UNKNOWN)
            source = FocusSource.PROPERTY
        return FocusEvent(
            timestamp_ms=stamp,
            state=state,
            source=source,
            raw_value=int(raw.i0),
        )
    if raw.kind == _cabi.EVENT_CAPTURE:
        return CaptureEvent(timestamp_ms=stamp)
    if raw.kind == _cabi.EVENT_CONTENT:
        # The vendor notifies that the list changed without saying what
        # appeared, so this carries no identity. It is a prompt to re-read the
        # index, never the answer itself.
        return ContentEvent(timestamp_ms=stamp)
    if raw.kind in (_cabi.EVENT_WARNING, _cabi.EVENT_ERROR):
        return WarningEvent(timestamp_ms=stamp, code=int(raw.code))
    # Anything the bridge could not classify still reaches the caller.
    return UnknownEvent(
        timestamp_ms=stamp,
        code=int(raw.code),
        payload={"kind": int(raw.kind), "i0": int(raw.i0), "i1": int(raw.i1)},
    )


# -- content index -----------------------------------------------------

#: Preview forms the content index can serve, and their vendor codes. Postview
#: and live view are deliberately absent: neither comes from this API.
_PREVIEW_TO_ABI = {
    PreviewKind.THUMBNAIL: _cabi.PREVIEW_THUMBNAIL,
    PreviewKind.SCREENNAIL: _cabi.PREVIEW_SCREENNAIL,
}

#: Which capability each preview form belongs to, for error reporting.
PREVIEW_CAPABILITY = {
    PreviewKind.THUMBNAIL: "thumbnail",
    PreviewKind.SCREENNAIL: "screennail",
    PreviewKind.POSTVIEW: "postview_delivery",
    PreviewKind.LIVE_VIEW: "live_view",
}

#: Control modes that expose the content index at all. Established by
#: measurement, not by model: the same body exposes it in one mode and not the
#: other.
CONTENT_MODES = frozenset({SessionMode.REMOTE_TRANSFER})

#: How long to wait for one compressed preview. Characterized at roughly a
#: second end to end; this leaves room for a slower card without hanging.
_PREVIEW_TIMEOUT_MS = 15_000


def preview_kind_to_abi(kind: PreviewKind) -> int:
    """Resolve a preview form to its vendor code, or refuse clearly."""
    try:
        return _PREVIEW_TO_ABI[kind]
    except KeyError:
        raise UnsupportedOperationError(
            f"{kind.value} does not come from the content index; "
            "it is a separate capability with its own transport",
            capability=PREVIEW_CAPABILITY.get(kind, "content_index"),
            operation="get_preview",
        ) from None


def _captured_at(raw: _cabi.ContentStruct) -> Optional[str]:
    """Format the camera's creation time, or ``None`` when it reported none.

    No timezone is attached. The camera does not state one, and guessing would
    turn an unknown into a wrong answer.
    """
    if not raw.created_year:
        return None
    return (
        f"{raw.created_year:04d}-{raw.created_month:02d}-{raw.created_day:02d}"
        f"T{raw.created_hour:02d}:{raw.created_minute:02d}:"
        f"{raw.created_second:02d}.{raw.created_millisecond:03d}"
    )


def decode_content(raw: _cabi.ContentStruct, *, observed_ms: int) -> ContentRef:
    """Turn one content record into plain data.

    ``created_ms`` is the observation time on the backend clock, not the
    camera's calendar: the two share no origin and mixing them would make any
    latency computed from it meaningless. The camera's own timestamp is kept
    separately in ``captured_at``.
    """
    return ContentRef(
        content_id=int(raw.content_id),
        file_id=int(raw.file_id),
        file_number=int(raw.file_number) or None,
        path=raw.path.decode("utf-8", "replace") or None,
        created_ms=observed_ms,
        captured_at=_captured_at(raw),
        content_type=int(raw.content_type),
        file_format=int(raw.file_format),
        width=int(raw.image_width) or None,
        height=int(raw.image_height) or None,
        file_size=int(raw.file_size) if raw.file_size >= 0 else None,
        slot=int(raw.slot) or 1,
        file_count=int(raw.file_count) or 1,
    )


def build_content_preview(
    kind: PreviewKind,
    data: bytes,
    info: _cabi.PreviewInfoStruct,
    *,
    timestamp_ms: int,
    content: Optional[ContentRef] = None,
) -> Preview:
    """Validate transferred preview bytes and wrap them.

    The bytes are checked rather than trusted. Dimensions are parsed out of the
    JPEG itself: the content index reports the geometry of the *original*
    still, which is not the geometry of a screennail derived from it, so the
    only honest source for a preview's size is the preview.
    """
    if not is_jpeg(data):
        raise CameraConnectionError(
            f"the camera returned {len(data)} bytes for the "
            f"{kind.value} of content {int(info.content_id)} that are not a "
            "JPEG; refusing to present them as an image",
            operation="get_preview",
            backend_code=int(info.vendor_notify) or None,
        )
    dimensions = jpeg_dimensions(data)
    if dimensions is None:
        raise CameraConnectionError(
            f"the {kind.value} of content {int(info.content_id)} carries a "
            f"JPEG signature but no readable frame header in {len(data)} "
            "bytes; the transfer is most likely truncated",
            operation="get_preview",
            backend_code=int(info.vendor_notify) or None,
        )
    width, height = dimensions

    metadata: dict[str, object] = {
        "file_id": int(info.file_id),
        "slot": int(info.slot),
        # Both forms are derived by the camera from the identified still, so
        # the association is exact rather than temporal.
        "exact_still_association": "content_id",
        "transfer_ms": int(info.completed_ms) - int(info.requested_ms),
        "byte_length": len(data),
        # More than one means the vendor delivered in stages; returning on the
        # first stage would have produced a valid but non-final image.
        "deliveries": int(info.deliveries),
    }
    if info.vendor_notify:
        metadata["vendor_notify"] = int(info.vendor_notify)
    if content is not None:
        metadata["path"] = content.path
        metadata["filename"] = content.filename
        metadata["file_number"] = content.file_number
        metadata["content_type"] = content.content_type
        metadata["file_format"] = content.file_format
    return Preview(
        kind=kind,
        data=data,
        mime="image/jpeg",
        width=width,
        height=height,
        timestamp_ms=timestamp_ms,
        content_id=int(info.content_id),
        metadata=metadata,
    )


def build_postview(
    data: bytes, info: _cabi.PostviewInfoStruct, *, timestamp_ms: int
) -> Preview:
    """Validate delivered postview bytes and wrap them.

    A postview depicts the exposure that produced it, so it is an exact still
    even though it carries no content identifier: it arrives by announcement
    rather than by lookup, and the camera announces one per capture.
    """
    if not is_jpeg(data):
        raise CameraConnectionError(
            f"the camera delivered {len(data)} postview bytes that are not a "
            "JPEG; refusing to present them as an image",
            operation="pull_postview",
        )
    dimensions = jpeg_dimensions(data)
    if dimensions is None:
        raise CameraConnectionError(
            "the postview carries a JPEG signature but no readable frame "
            f"header in {len(data)} bytes; the delivery is most likely "
            "truncated",
            operation="pull_postview",
        )
    width, height = dimensions
    filename = info.filename.decode("utf-8", "replace") or None
    return Preview(
        kind=PreviewKind.POSTVIEW,
        data=data,
        mime="image/jpeg",
        width=width,
        height=height,
        timestamp_ms=timestamp_ms,
        # No content id: postview is delivered from memory and is not an item
        # in the index. Its exactness comes from being announced per capture.
        content_id=None,
        metadata={
            "exact_still_association": "announced_per_capture",
            "filename": filename,
            "byte_length": len(data),
            "delivery_ms": int(info.pulled_ms) - int(info.notified_ms),
        },
    )


def decode_live_view_info(raw: _cabi.LiveViewInfoStruct) -> LiveViewInfo:
    return LiveViewInfo(
        info_ok=bool(raw.info_ok),
        width=int(raw.width) or None,
        height=int(raw.height) or None,
        buffer_size=int(raw.buffer_size),
        error_code=int(raw.vendor_error) or None,
    )


def build_live_view_frame(
    data: bytes, info: _cabi.FrameInfoStruct, *, timestamp_ms: int
) -> LiveViewFrame:
    """Wrap one frame.

    Geometry is parsed from the frame when possible and falls back to what the
    info call reported. Unlike a capture preview, an unreadable live-view frame
    is not fatal: the stream is best-effort and the caller will get another.
    """
    parsed = jpeg_dimensions(data)
    width, height = parsed if parsed else (int(info.width) or None,
                                           int(info.height) or None)
    return LiveViewFrame(
        data=data,
        mime="image/jpeg",
        width=width,
        height=height,
        timestamp_ms=timestamp_ms,
        frame_number=int(info.frame_number),
        metadata={
            "byte_length": len(data),
            "time_code": int(info.time_code),
            "geometry_source": "frame" if parsed else "info",
        },
    )


class FrameSequencer:
    """Suppresses a frame the caller has already been given.

    The vendor hands back whatever it currently holds, so polling faster than
    the camera produces returns the same frame again. Live view wants the
    newest frame and nothing else, so a repeat is reported as "nothing new"
    rather than queued: there is no backlog to fall behind on by design.
    """

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        #: Frames the camera produced that a caller never asked for. Not a
        #: queue depth - it is how far behind the caller's polling is.
        self.skipped: dict[str, int] = {}

    def accept(self, session_id: str, frame_number: int) -> bool:
        previous = self._last.get(session_id)
        if previous is not None and frame_number == previous:
            return False
        if previous is not None and frame_number > previous + 1:
            self.skipped[session_id] = (
                self.skipped.get(session_id, 0) + frame_number - previous - 1
            )
        self._last[session_id] = frame_number
        return True

    def forget(self, session_id: str) -> None:
        self._last.pop(session_id, None)
        self.skipped.pop(session_id, None)


class MeasuredCapabilities:
    """Per-session capability facts that cost a camera round trip to learn.

    Measured rather than assumed, and cached because they cannot change within
    a session: the control mode is fixed at connect time and a body does not
    grow a video engine while connected. Destination is deliberately *not*
    cached here, because it can be changed at any time and changes what the
    session can do.
    """

    def __init__(self) -> None:
        self._live_view: dict[str, bool] = {}
        self._video: dict[str, bool] = {}
        #: Set once the camera has actually rejected a postview configuration.
        self.postview_configuration_refused: set = set()
        #: Set once a postview has actually been delivered.
        self.postview_delivered: set = set()

    def live_view(self, session_id: str, probe) -> bool:
        # Only a positive result is remembered. Live view can report itself
        # unusable immediately after connect and become usable shortly after,
        # so a negative answer is not treated as final.
        if self._live_view.get(session_id):
            return True
        try:
            usable = bool(probe())
        except Exception:
            usable = False
        if usable:
            self._live_view[session_id] = True
        return usable

    def video(self, session_id: str, probe) -> bool:
        if session_id not in self._video:
            try:
                self._video[session_id] = bool(probe())
            except Exception:
                self._video[session_id] = False
        return self._video[session_id]

    def forget(self, session_id: str) -> None:
        self._live_view.pop(session_id, None)
        self._video.pop(session_id, None)
        self.postview_configuration_refused.discard(session_id)
        self.postview_delivered.discard(session_id)


def _content_capabilities(mode: SessionMode) -> dict:
    """Content-related capabilities for a session in *mode*.

    Thumbnail and screennail travel with the index because they are served by
    the same vendor API; a mode that has no index has neither.
    """
    available = mode in CONTENT_MODES
    return {
        "content_index": available,
        "thumbnail": available,
        "screennail": available,
    }


class NativeCapabilityMixin:
    """Builds session capabilities from measurement, never from a model name.

    The destination is re-read each time because it can change at any moment
    and decides whether postview is delivered. The expensive facts - whether
    live view can actually produce a frame, whether the body reports a
    recording state - are measured once and cached.
    """

    def _measured(self) -> MeasuredCapabilities:  # pragma: no cover - interface
        raise NotImplementedError

    def _probe_live_view(self, session_id: str) -> bool:
        return self.live_view_info(session_id).usable

    def _probe_video(self, session_id: str) -> bool:
        return (
            self._optional_value(session_id, CODE_RECORDING_STATE) is not None
        )

    def _build_capabilities(
        self, session_id: str, mode: SessionMode, extra: Mapping[str, bool]
    ) -> SessionCapabilities:
        measured = self._measured()
        destination = self.get_destination(session_id)
        return SessionCapabilities(
            mode=mode,
            destination=destination,
            still_capture=True,
            autofocus_s1=True,
            video=measured.video(
                session_id, lambda: self._probe_video(session_id)
            ),
            live_view=measured.live_view(
                session_id, lambda: self._probe_live_view(session_id)
            ),
            **_content_capabilities(mode),
            # Optimistic until the camera says otherwise: whether the call is
            # accepted can only be learned by making it, so reporting False up
            # front would hide the one action that discovers the answer.
            postview_configuration=(
                session_id not in measured.postview_configuration_refused
            ),
            # Characterized as following the still destination, and confirmed
            # outright once a postview has actually arrived. Deliberately not
            # inferred from whether configuration was accepted: hardware showed
            # those two disagreeing.
            postview_delivery=(
                destination.includes_host
                or session_id in measured.postview_delivered
            ),
            raw_commands=True,
            extra=dict(extra),
        )


def unsupported_content_mode(mode: SessionMode, operation: str, capability: str):
    """The error for a content call made in a mode that has no content index."""
    return UnsupportedOperationError(
        "the content index is not exposed in control mode "
        f"{mode.value!r}; reopen the camera in "
        f"{SessionMode.REMOTE_TRANSFER.value!r} to reach it",
        capability=capability,
        operation=operation,
    )


# -- properties --------------------------------------------------------

def decode_property(raw: _cabi.PropertyStruct) -> Property:
    return Property(
        code=PropertyCode(int(raw.code)),
        value=int(raw.value),
        value_type=_ABI_TO_VALUE_TYPE.get(
            raw.value_type, PropertyValueType.UNKNOWN
        ),
        access=_ABI_TO_ACCESS.get(raw.access, PropertyAccess.UNKNOWN),
        metadata={"allowed_count": int(raw.allowed_count)},
    )


class NativeBackend(
    ShutterStageMixin,
    DeviceStatusMixin,
    VideoStageMixin,
    NativeCapabilityMixin,
    Backend,
):
    """In-process native driver."""

    name = "native"

    def __init__(
        self,
        sdk_path: Optional[str] = None,
        *,
        library_path: Optional[str] = None,
        clock: Optional[Clock] = None,
        enumerate_timeout_sec: int = 3,
        adapter_dir: Optional[str] = None,
    ) -> None:
        resolved = _cabi.find_library(library_path or sdk_path)
        self._lib = _cabi.load_bridge(library_path or sdk_path)
        # The vendor runtime and its adapter directory sit beside the bridge.
        self._adapter_dir = adapter_dir or (
            os.path.dirname(resolved) if resolved else None
        )
        self._clock = clock or RealClock()
        self._enumerate_timeout = enumerate_timeout_sec
        self._started = False
        self._handles: dict[str, int] = {}
        self._modes: dict[str, SessionMode] = {}
        self._counter = 0
        self._measurements = MeasuredCapabilities()
        self._frames = FrameSequencer()
        # The vendor SDK starts threads that keep the process alive, so a
        # missed shutdown hangs interpreter exit. Guarantee the release.
        atexit.register(self._atexit_shutdown)

    # -- lifecycle ---------------------------------------------------------
    @property
    def clock(self) -> Clock:
        return self._clock

    def start(self) -> None:
        if self._started:
            return
        adapter = self._adapter_dir.encode("utf-8") if self._adapter_dir else None
        _cabi.check(self._lib, self._lib.crsdkpy_init(adapter), "init")
        self._started = True

    def shutdown(self) -> None:
        if not self._started:
            return
        self._handles.clear()
        self._modes.clear()
        _cabi.check(self._lib, self._lib.crsdkpy_shutdown(), "shutdown")
        self._started = False

    def _atexit_shutdown(self) -> None:
        """Last-resort release so a missed shutdown cannot hang exit."""
        if not self._started:
            return
        try:
            self.shutdown()
        except Exception:  # pragma: no cover - best effort at interpreter exit
            pass

    def _require_started(self, operation: str) -> None:
        if not self._started:
            raise NativeBackendError(
                "the native backend has not been started", operation=operation
            )

    # -- discovery ---------------------------------------------------------
    def enumerate_cameras(self) -> Sequence[BackendCameraInfo]:
        self._require_started("enumerate_cameras")
        count = ctypes.c_uint32(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_enumerate(self._enumerate_timeout, ctypes.byref(count)),
            "enumerate_cameras",
        )

        cameras = []
        for index in range(count.value):
            info = _cabi.CameraInfoStruct()
            _cabi.check(
                self._lib,
                self._lib.crsdkpy_camera_at(index, ctypes.byref(info)),
                "camera_at",
            )
            cameras.append(self._to_camera_info(info))
        return cameras

    def _to_camera_info(self, info: _cabi.CameraInfoStruct) -> BackendCameraInfo:
        return decode_camera_info(info)

    def open_session(
        self,
        device_key: str,
        mode: SessionMode,
        destination: Optional[StillDestination] = None,
    ) -> str:
        self._require_started("open_session")
        abi_mode = _MODE_TO_ABI.get(mode)
        if abi_mode is None:
            raise UnsupportedOperationError(
                f"unknown control mode {mode!r}",
                capability=f"mode.{mode}",
                operation="open_session",
            )

        handle = ctypes.c_uint64(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_open_session(
                device_key.encode("utf-8"), abi_mode, ctypes.byref(handle)
            ),
            "open_session",
        )
        self._counter += 1
        session_id = f"native-session-{self._counter}"
        self._handles[session_id] = handle.value
        self._modes[session_id] = mode
        # Destination is a property, so it is applied after connecting rather
        # than being part of the connection itself.
        if destination is not None:
            self._apply_destination(session_id, destination)
        return session_id

    def _handle(self, session_id: str, operation: str) -> int:
        handle = self._handles.get(session_id)
        if handle is None:
            from ..errors import SessionClosedError

            raise SessionClosedError(
                f"unknown session {session_id!r}", operation=operation
            )
        return handle

    def close_session(self, session_id: str) -> None:
        handle = self._handles.pop(session_id, None)
        self._modes.pop(session_id, None)
        self._measurements.forget(session_id)
        self._frames.forget(session_id)
        if handle is None:
            return  # idempotent
        _cabi.check(
            self._lib, self._lib.crsdkpy_close_session(handle), "close_session"
        )

    def connection_state(self, session_id: str) -> ConnectionState:
        handle = self._handles.get(session_id)
        if handle is None:
            return ConnectionState.CLOSED
        state = ctypes.c_int32(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_connection_state(handle, ctypes.byref(state)),
            "connection_state",
        )
        return _ABI_TO_STATE.get(state.value, ConnectionState.CLOSED)

    def _measured(self) -> MeasuredCapabilities:
        return self._measurements

    def _invalidate_capabilities(self, session_id: str) -> None:
        # Only the destination-dependent facts change; the measured ones
        # cannot, so they are kept.
        return None

    def session_capabilities(self, session_id: str) -> SessionCapabilities:
        mode = self._modes.get(session_id, SessionMode.REMOTE)
        return self._build_capabilities(
            session_id, mode, {"backend": True, "hosted": False}
        )

    # -- events ------------------------------------------------------------
    def poll_events(self, session_id: str, timeout_ms: int = 0) -> Sequence[Event]:
        handle = self._handle(session_id, "poll_events")
        capacity = 128
        buffer = (_cabi.EventStruct * capacity)()
        produced = ctypes.c_uint32(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_poll_events(
                handle, buffer, capacity, ctypes.byref(produced), int(timeout_ms)
            ),
            "poll_events",
        )
        observed = self._clock.now_ms()
        return [
            decode_event(buffer[i], timestamp_ms=observed)
            for i in range(produced.value)
        ]

    def _to_event(self, raw: _cabi.EventStruct) -> Event:
        return decode_event(raw)

    def list_properties(self, session_id: str) -> Sequence[Property]:
        handle = self._handle(session_id, "list_properties")
        count = ctypes.c_uint32(0)
        # Size first, then fetch: the count is live and mode-dependent.
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_list_properties(handle, None, 0, ctypes.byref(count)),
            "list_properties",
        )
        if count.value == 0:
            return []
        buffer = (_cabi.PropertyStruct * count.value)()
        produced = ctypes.c_uint32(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_list_properties(
                handle, buffer, count.value, ctypes.byref(produced)
            ),
            "list_properties",
        )
        return [self._to_property(buffer[i]) for i in range(produced.value)]

    def get_property(self, session_id: str, code: PropertyCode) -> Property:
        handle = self._handle(session_id, "get_property")
        raw = _cabi.PropertyStruct()
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_get_property(handle, int(code), ctypes.byref(raw)),
            "get_property",
        )
        return self._to_property(raw)

    @staticmethod
    def _to_property(raw: _cabi.PropertyStruct) -> Property:
        return decode_property(raw)

    def set_property(self, session_id: str, code: PropertyCode, value: Any) -> None:
        handle = self._handle(session_id, "set_property")
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_set_property(handle, int(code), int(value)),
            "set_property",
        )

    def send_command(self, session_id: str, command: Any, parameter: Any) -> None:
        handle = self._handle(session_id, "send_command")
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_send_command(
                handle, command_to_abi(command), parameter_to_abi(parameter)
            ),
            "send_command",
        )

    # -- live view ---------------------------------------------------------
    def live_view_info(self, session_id: str) -> LiveViewInfo:
        handle = self._handle(session_id, "live_view_info")
        raw = _cabi.LiveViewInfoStruct()
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_get_live_view_info(handle, ctypes.byref(raw)),
            "live_view_info",
        )
        return decode_live_view_info(raw)

    def get_live_view_frame(self, session_id: str) -> Optional[LiveViewFrame]:
        handle = self._handle(session_id, "get_live_view_frame")
        info = _cabi.FrameInfoStruct()
        status = self._lib.crsdkpy_get_live_view_frame(
            handle, None, 0, ctypes.byref(info)
        )
        if status == _cabi.ERR_NOT_FOUND:
            return None  # nothing new; ordinary around an exposure
        _cabi.check(self._lib, status, "get_live_view_frame")

        size = int(info.byte_length)
        buffer = (ctypes.c_uint8 * max(1, size))()
        status = self._lib.crsdkpy_get_live_view_frame(
            handle, buffer, size, ctypes.byref(info)
        )
        if status == _cabi.ERR_NOT_FOUND:
            return None
        _cabi.check(self._lib, status, "get_live_view_frame")
        if not self._frames.accept(session_id, int(info.frame_number)):
            return None  # the caller already has this one
        data = bytes(bytearray(buffer[: int(info.byte_length)]))
        return build_live_view_frame(
            data, info, timestamp_ms=self._clock.now_ms()
        )

    # -- postview ----------------------------------------------------------
    def configure_postview(
        self, session_id: str, *, enabled: bool, transfer_to_ram: bool = True
    ) -> None:
        handle = self._handle(session_id, "configure_postview")
        status = self._lib.crsdkpy_configure_postview(
            handle, 1 if enabled else 0, 1 if transfer_to_ram else 0
        )
        if status == _cabi.ERR_UNSUPPORTED:
            self._measurements.postview_configuration_refused.add(session_id)
        _cabi.check(self._lib, status, "configure_postview")

    def pull_postview(self, session_id: str) -> Optional[Preview]:
        handle = self._handle(session_id, "pull_postview")
        info = _cabi.PostviewInfoStruct()
        status = self._lib.crsdkpy_pull_postview(handle, ctypes.byref(info))
        if status == _cabi.ERR_NOT_FOUND:
            return None  # the camera has not announced one yet
        _cabi.check(self._lib, status, "pull_postview")

        size = ctypes.c_uint32(0)
        buffer = (ctypes.c_uint8 * max(1, int(info.byte_length)))()
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_take_postview(
                handle, buffer, info.byte_length, ctypes.byref(size)
            ),
            "pull_postview",
        )
        self._measurements.postview_delivered.add(session_id)
        data = bytes(bytearray(buffer[: size.value]))
        return build_postview(data, info, timestamp_ms=self._clock.now_ms())

    # -- content index -----------------------------------------------------
    def _require_content_mode(self, session_id: str, operation: str,
                              capability: str) -> None:
        mode = self._modes.get(session_id, SessionMode.REMOTE)
        if mode not in CONTENT_MODES:
            raise unsupported_content_mode(mode, operation, capability)

    def latest_content(self, session_id: str) -> Optional[ContentRef]:
        items = self.list_content(session_id)
        return items[-1] if items else None

    def list_content(
        self, session_id: str, *, newer_than: Optional[int] = None
    ) -> Sequence[ContentRef]:
        handle = self._handle(session_id, "list_content")
        self._require_content_mode(session_id, "list_content", "content_index")
        # Clamped: the wire field is unsigned, so a negative bound would wrap
        # to the largest possible identifier and quietly match nothing.
        after = max(0, int(newer_than)) if newer_than is not None else 0
        count = ctypes.c_uint32(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_list_content(
                handle, _cabi.SLOT_1, after, None, 0, ctypes.byref(count)
            ),
            "list_content",
        )
        if count.value == 0:
            return ()
        # Size generously: the camera may still be writing, so the count can
        # grow between the sizing call and this one.
        capacity = count.value + 8
        buffer = (_cabi.ContentStruct * capacity)()
        produced = ctypes.c_uint32(0)
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_list_content(
                handle, _cabi.SLOT_1, after, buffer, capacity, ctypes.byref(produced)
            ),
            "list_content",
        )
        observed = self._clock.now_ms()
        return tuple(
            decode_content(buffer[i], observed_ms=observed)
            for i in range(produced.value)
        )

    def get_preview(
        self, session_id: str, content_id: int, kind: PreviewKind
    ) -> Preview:
        handle = self._handle(session_id, "get_preview")
        capability = PREVIEW_CAPABILITY.get(kind, "content_index")
        self._require_content_mode(session_id, "get_preview", capability)
        abi_kind = preview_kind_to_abi(kind)

        # The file id is not the content id and is not derivable from it, so
        # the index is consulted rather than guessed.
        content = self._content_by_id(session_id, int(content_id))
        info = _cabi.PreviewInfoStruct()
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_fetch_content_preview(
                handle,
                _cabi.SLOT_1,
                int(content_id),
                content.file_id if content else 0,
                abi_kind,
                _PREVIEW_TIMEOUT_MS,
                ctypes.byref(info),
            ),
            "get_preview",
        )
        size = ctypes.c_uint32(0)
        buffer = (ctypes.c_uint8 * int(info.byte_length))()
        _cabi.check(
            self._lib,
            self._lib.crsdkpy_take_content_preview(
                handle, buffer, info.byte_length, ctypes.byref(size)
            ),
            "get_preview",
        )
        return build_content_preview(
            kind,
            bytes(bytearray(buffer[: size.value])),
            info,
            timestamp_ms=self._clock.now_ms(),
            content=content,
        )

    def _content_by_id(self, session_id: str, content_id: int) -> Optional[ContentRef]:
        for item in self.list_content(session_id, newer_than=content_id - 1):
            if item.content_id == content_id:
                return item
        return None
