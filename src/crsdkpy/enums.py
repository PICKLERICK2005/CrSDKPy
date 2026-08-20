"""Enumerated concepts in the public API.

These describe CrSDKPy semantics, not vendor wire values. Backends translate
between vendor codes and these members, so an unrecognised vendor value becomes
an explicit ``UNKNOWN`` member rather than leaking a raw integer into the API.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CaptureState",
    "ConnectionState",
    "FocusState",
    "PreviewKind",
    "PropertyAccess",
    "PropertyValueType",
    "RecordingState",
    "SessionMode",
    "StillDestination",
    "TransferOutcome",
]


class SessionMode(Enum):
    """Vendor control mode, chosen when the session is opened.

    The mode cannot be changed on a live session; switching means closing the
    session and opening a new one.
    """

    REMOTE = "remote"
    REMOTE_TRANSFER = "remote_transfer"
    CONTENTS_TRANSFER = "contents_transfer"

    def __str__(self) -> str:
        return self.value


class StillDestination(Enum):
    """Where a captured still is stored.

    An axis independent of :class:`SessionMode`; on the first characterized
    body it independently determines whether a postview is delivered.
    """

    MEMORY_CARD = "memory_card"
    HOST = "host"
    HOST_AND_MEMORY_CARD = "host_and_memory_card"

    def __str__(self) -> str:
        return self.value

    @property
    def includes_host(self) -> bool:
        return self in (StillDestination.HOST, StillDestination.HOST_AND_MEMORY_CARD)

    @property
    def includes_card(self) -> bool:
        return self in (
            StillDestination.MEMORY_CARD,
            StillDestination.HOST_AND_MEMORY_CARD,
        )


class ConnectionState(Enum):
    """Lifecycle of a session's transport.

    ``CONNECTED -> RECONNECTING -> CONNECTED`` is a legal path with no
    intervening ``CLOSED``: recovery must never require a disconnect event.
    """

    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    CLOSED = "closed"

    @property
    def is_usable(self) -> bool:
        return self is ConnectionState.CONNECTED

    @property
    def is_terminal(self) -> bool:
        return self is ConnectionState.CLOSED


class FocusState(Enum):
    """Normalized autofocus state.

    Only :attr:`FOCUSED_AF_S` and :attr:`FOCUSED_AF_C` count as confirmation.
    :attr:`TRACKING_AF_C` is an in-progress state and must never gate a
    release; on the characterized body it preceded real focus by ~61 ms.
    """

    UNKNOWN = "unknown"
    UNLOCKED = "unlocked"
    FOCUSED_AF_S = "focused_af_s"
    NOT_FOCUSED_AF_S = "not_focused_af_s"
    FOCUSED_AF_C = "focused_af_c"
    NOT_FOCUSED_AF_C = "not_focused_af_c"
    TRACKING_AF_C = "tracking_af_c"

    @property
    def is_focused(self) -> bool:
        """True only for states that authorise a release."""
        return self in (FocusState.FOCUSED_AF_S, FocusState.FOCUSED_AF_C)

    @property
    def is_failure(self) -> bool:
        """True for states that mean autofocus gave up."""
        return self in (FocusState.NOT_FOCUSED_AF_S, FocusState.NOT_FOCUSED_AF_C)


class CaptureState(Enum):
    """Progress of a capture operation.

    A capture is not a boolean. Command acceptance does not imply exposure, and
    exposure does not imply that durable content exists yet.
    """

    REQUESTED = "requested"
    FOCUSING = "focusing"
    FOCUSED = "focused"
    EXPOSED = "exposed"
    CONTENT_AVAILABLE = "content_available"
    PREVIEW_AVAILABLE = "preview_available"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (CaptureState.PREVIEW_AVAILABLE, CaptureState.FAILED)


class RecordingState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    FAILED = "failed"


class TransferOutcome(Enum):
    """Normalized outcome of a transfer request.

    The camera reports progress and result through one notification, so a
    transfer is a sequence of these rather than a single answer. ``UNKNOWN``
    means the vendor sent a code this version does not recognise, which is not
    an error: the raw code travels on the event beside it.
    """

    IN_PROGRESS = "in_progress"
    OK = "ok"
    FAILED = "failed"
    BUSY = "busy"
    STORAGE_FULL = "storage_full"
    STOPPED = "stopped"
    CANCELED = "canceled"
    UNKNOWN = "unknown"

    @property
    def finished(self) -> bool:
        """Whether this outcome ends the transfer, successfully or not."""
        return self is not TransferOutcome.IN_PROGRESS


class PreviewKind(Enum):
    """Forms of image data a camera can return."""

    LIVE_VIEW = "live_view"
    POSTVIEW = "postview"
    THUMBNAIL = "thumbnail"
    SCREENNAIL = "screennail"

    @property
    def is_exact_still(self) -> bool:
        """Whether this form is guaranteed to depict the captured exposure."""
        return self is not PreviewKind.LIVE_VIEW


class PropertyAccess(Enum):
    UNKNOWN = "unknown"
    READ_ONLY = "read_only"
    WRITE_ONLY = "write_only"
    READ_WRITE = "read_write"

    @property
    def writable(self) -> bool:
        return self in (PropertyAccess.READ_WRITE, PropertyAccess.WRITE_ONLY)

    @property
    def readable(self) -> bool:
        return self in (PropertyAccess.READ_WRITE, PropertyAccess.READ_ONLY)


class PropertyValueType(Enum):
    UNKNOWN = "unknown"
    INT = "int"
    STRING = "string"
    BYTES = "bytes"
    INT_ARRAY = "int_array"
