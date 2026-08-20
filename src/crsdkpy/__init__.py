"""CrSDKPy - an independent Python interface for the Sony Camera Remote SDK.

Beta. Discovery, sessions, properties, events, still capture, gated autofocus,
the content index with thumbnails and screennails, RAM postview, live view and
movie recording are implemented against both a deterministic simulator and a
native out-of-process backend.

Hardware validation is tracked separately from implementation, in
``docs/FEATURE_MATRIX.md``. An implemented feature and a validated one are
different claims and this package does not conflate them.

The Sony Camera Remote SDK is **not** distributed with CrSDKPy. Obtain it from
Sony under its own terms.

Quick start against the simulator, which needs neither::

    import crsdkpy

    with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            if session.capabilities.live_view:
                frame = session.live_view.get_frame()

Capabilities are always discovered, never assumed from a model name: the same
body exposes different capabilities in different control modes and with
different still destinations.
"""

from __future__ import annotations

__version__ = "0.1.0b2"

from .camera import Camera, CameraInfo
from .capabilities import CameraCapabilities, SessionCapabilities
from .capture import Capture, CapturedContent, FocusResult
from .clock import Clock, RealClock, VirtualClock
from .commands import Command, CommandParameter
from .content import Content
from .enums import (
    CaptureState,
    ConnectionState,
    FocusState,
    PreviewKind,
    PropertyAccess,
    PropertyValueType,
    ReconnectPolicy,
    RecordingState,
    SessionMode,
    StillDestination,
    TransferOutcome,
)
from .errors import (
    AutofocusFailedError,
    BackendError,
    BackendUnavailableError,
    CameraBusyError,
    CameraConnectionError,
    CrSDKPyError,
    InvalidSessionStateError,
    NativeBackendError,
    OperationTimeoutError,
    PropertyNotSupportedError,
    SDKNotFoundError,
    SDKNotStartedError,
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
    PropertyChangedEvent,
    RecordingEvent,
    TransferEvent,
    UnknownEvent,
    WarningEvent,
)
from .liveview import LiveView, LiveViewStats, LiveViewStatus
from .previews import LiveViewFrame, Preview
from .properties import (
    Property,
    PropertyCode,
    PropertyRange,
    PropertySnapshot,
    register_property_name,
)
from .sdk import SDK
from .session import Session
from .status import BatteryStatus, StorageSlot
from .video import Recording, Video

__all__ = [
    "__version__",
    # entry point
    "SDK",
    # identity and lifecycle
    "Camera",
    "CameraInfo",
    "Session",
    "SessionMode",
    "StillDestination",
    "ConnectionState",
    # capabilities
    "CameraCapabilities",
    "SessionCapabilities",
    # properties
    "Property",
    "PropertyCode",
    "PropertyRange",
    "PropertySnapshot",
    "PropertyAccess",
    "PropertyValueType",
    "ReconnectPolicy",
    "register_property_name",
    # events
    "Event",
    "ConnectionEvent",
    "PropertyChangedEvent",
    "FocusEvent",
    "FocusSource",
    "CaptureEvent",
    "ContentEvent",
    "RecordingEvent",
    "TransferEvent",
    "WarningEvent",
    "UnknownEvent",
    # capture and focus
    "Capture",
    "CapturedContent",
    "Content",
    "CaptureState",
    "FocusResult",
    "FocusState",
    # imaging
    "LiveView",
    "LiveViewFrame",
    "LiveViewStats",
    "LiveViewStatus",
    "Preview",
    "PreviewKind",
    # device status
    "BatteryStatus",
    "StorageSlot",
    # video
    "Video",
    "Recording",
    "RecordingState",
    "TransferOutcome",
    # commands and clocks
    "Command",
    "CommandParameter",
    "Clock",
    "RealClock",
    "VirtualClock",
    # errors
    "CrSDKPyError",
    "BackendError",
    "BackendUnavailableError",
    "NativeBackendError",
    "SDKNotFoundError",
    "SDKNotStartedError",
    "CameraConnectionError",
    "CameraBusyError",
    "SessionClosedError",
    "InvalidSessionStateError",
    "UnsupportedOperationError",
    "PropertyNotSupportedError",
    "OperationTimeoutError",
    "AutofocusFailedError",
]
