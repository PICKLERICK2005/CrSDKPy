"""Event model.

Events are a stream of typed facts. Nothing in CrSDKPy may depend on two
channels arriving in a particular order, on a notification arriving at all, or
on a value still being current when it is read.

Property changes arrive as coalesced batches, mirroring the vendor callback,
with the caveat that a related property may straggle into a later batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .enums import ConnectionState, FocusState, RecordingState
from .properties import PropertyCode

__all__ = [
    "CaptureEvent",
    "ConnectionEvent",
    "ContentEvent",
    "Event",
    "FocusEvent",
    "FocusSource",
    "PropertyChangedEvent",
    "RecordingEvent",
    "UnknownEvent",
    "WarningEvent",
]


class FocusSource:
    """Which asynchronous channel reported an autofocus state.

    The characterized body exposed two independent channels using different
    vendor enumerations, with no fixed ordering and observed transient
    disagreement. Both are decoded separately and both are authoritative for
    "a focused state was seen".
    """

    PROPERTY = "property"
    STATUS_WARNING = "status_warning"
    DIRECT_READ = "direct_read"


@dataclass(frozen=True)
class Event:
    """Base class for everything delivered on the session event stream."""

    timestamp_ms: int = 0

    @property
    def kind(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class ConnectionEvent(Event):
    state: ConnectionState = ConnectionState.CLOSED
    #: Set when the transport recovered without an intervening disconnect.
    recovered: bool = False
    backend_code: Optional[int] = None
    #: The vendor's connection version, reported only on a first connect.
    connection_version: Optional[int] = None


@dataclass(frozen=True)
class PropertyChangedEvent(Event):
    """One coalesced batch of changed property codes.

    ``codes`` mirrors a single vendor callback. A property related to the same
    physical change may still arrive in a later event.
    """

    codes: tuple[PropertyCode, ...] = ()

    def __contains__(self, code: object) -> bool:
        if isinstance(code, (int, PropertyCode)):
            return any(int(c) == int(code) for c in self.codes)
        return False


@dataclass(frozen=True)
class FocusEvent(Event):
    state: FocusState = FocusState.UNKNOWN
    source: str = FocusSource.PROPERTY
    #: The vendor value before normalization, for diagnostics only.
    raw_value: Optional[int] = None

    @property
    def is_focused(self) -> bool:
        return self.state.is_focused


@dataclass(frozen=True)
class CaptureEvent(Event):
    """The camera reported that an exposure completed.

    This is the first trustworthy evidence of an exposure. Command acceptance
    is not.
    """

    sequence: int = 0


@dataclass(frozen=True)
class ContentEvent(Event):
    """New durable content appeared on the camera's media."""

    content_id: Optional[int] = None
    file_number: Optional[int] = None
    path: Optional[str] = None


@dataclass(frozen=True)
class RecordingEvent(Event):
    state: RecordingState = RecordingState.IDLE


@dataclass(frozen=True)
class WarningEvent(Event):
    """A vendor warning that CrSDKPy recognises but does not model further."""

    code: int = 0
    message: str = ""


@dataclass(frozen=True)
class UnknownEvent(Event):
    """A backend event CrSDKPy has no typed representation for.

    Emitted rather than dropped, so an application can react to vendor
    functionality newer than this release.
    """

    code: int = 0
    payload: Any = field(default=None)
