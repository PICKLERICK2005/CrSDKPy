"""ctypes binding for the native bridge's C ABI.

This module is the only place that knows the binary layout of
``native/include/crsdkpy_abi.h``. It deliberately does nothing clever: load the
library, declare the signatures, check the ABI version, translate status codes
into exceptions. All policy lives above it.

The bridge is a plain shared library, not a Python extension module, so one
build works for every supported interpreter.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import (
    POINTER,
    c_char,
    c_char_p,
    c_int32,
    c_int64,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
)
from typing import Optional

from ..errors import (
    CameraConnectionError,
    NativeBackendError,
    OperationTimeoutError,
    PropertyNotSupportedError,
    SDKNotFoundError,
    SessionClosedError,
    UnsupportedOperationError,
)

__all__ = [
    "ABI_VERSION_MAJOR",
    "CameraInfoStruct",
    "ContentStruct",
    "EventStruct",
    "FrameInfoStruct",
    "LiveViewInfoStruct",
    "PostviewInfoStruct",
    "PreviewInfoStruct",
    "PropertyStruct",
    "check",
    "find_library",
    "load_bridge",
]

ABI_VERSION_MAJOR = 1

#: Environment variable holding an explicit path to the built bridge.
LIBRARY_ENV_VAR = "CRSDKPY_BRIDGE"

OK = 0
ERR_UNKNOWN = -1
ERR_NOT_INITIALIZED = -2
ERR_ALREADY_INIT = -3
ERR_INVALID_ARG = -4
ERR_INVALID_HANDLE = -5
ERR_BUFFER_TOO_SMALL = -6
ERR_NOT_FOUND = -7
ERR_SDK_INIT_FAILED = -8
ERR_CONNECT_FAILED = -9
ERR_TIMEOUT = -10
ERR_UNSUPPORTED = -11
ERR_NOT_CONNECTED = -12

# Event kinds, mirroring the header.
EVENT_CONNECTION = 0
EVENT_PROPERTY_CHANGED = 1
EVENT_FOCUS = 2
EVENT_CAPTURE = 3
EVENT_CONTENT = 4
EVENT_WARNING = 5
EVENT_ERROR = 6
EVENT_RAW = 7
EVENT_TRANSFER = 8

# Normalized transfer outcomes, mirroring the ABI header.
TRANSFER_IN_PROGRESS = 0
TRANSFER_OK = 1
TRANSFER_FAILED = 2
TRANSFER_BUSY = 3
TRANSFER_STORAGE_FULL = 4
TRANSFER_STOPPED = 5
TRANSFER_CANCELED = 6
TRANSFER_UNKNOWN = 7

# Shape of a property's advertised value set, mirroring the ABI header.
VALUES_NONE = 0
VALUES_ENUM = 1
VALUES_RANGE = 2
VALUES_RAW = 3

FOCUS_SRC_PROPERTY = 0
FOCUS_SRC_WARNING = 1

# Compressed preview forms, mirroring the header.
PREVIEW_THUMBNAIL = 1
PREVIEW_SCREENNAIL = 2

SLOT_1 = 1
SLOT_2 = 2


class CameraInfoStruct(ctypes.Structure):
    _fields_ = [
        ("device_key", c_char * 192),
        ("model", c_char * 64),
        ("serial", c_char * 64),
        ("firmware", c_char * 32),
        ("transport", c_char * 32),
        ("adapter", c_char * 64),
        ("usb_pid", c_int32),
        ("reserved", c_int32),
    ]


class PropertyStruct(ctypes.Structure):
    _fields_ = [
        ("code", c_uint32),
        ("value_type", c_int32),
        ("access", c_int32),
        ("reserved", c_int32),
        ("value", c_int64),
        ("allowed_count", c_uint32),
        ("reserved2", c_uint32),
    ]


class ContentStruct(ctypes.Structure):
    _fields_ = [
        ("content_id", c_uint32),
        ("file_id", c_uint32),
        ("file_number", c_uint32),
        ("dir_number", c_uint32),
        ("content_type", c_uint32),
        ("file_format", c_uint32),
        ("image_width", c_uint32),
        ("image_height", c_uint32),
        ("file_size", c_int64),
        ("slot", c_uint32),
        ("file_count", c_uint32),
        ("created_year", c_uint16),
        ("created_month", c_uint16),
        ("created_day", c_uint16),
        ("created_hour", c_uint16),
        ("created_minute", c_uint16),
        ("created_second", c_uint16),
        ("created_millisecond", c_uint16),
        ("reserved", c_uint16),
        ("path", c_char * 256),
    ]


class PreviewInfoStruct(ctypes.Structure):
    _fields_ = [
        ("content_id", c_uint32),
        ("file_id", c_uint32),
        ("kind", c_int32),
        ("vendor_notify", c_int32),
        ("byte_length", c_uint32),
        ("slot", c_uint32),
        ("deliveries", c_uint32),
        ("last_percent", c_uint32),
        ("requested_ms", c_int64),
        ("completed_ms", c_int64),
    ]


class PostviewInfoStruct(ctypes.Structure):
    _fields_ = [
        ("byte_length", c_uint32),
        ("reserved", c_uint32),
        ("notified_ms", c_int64),
        ("pulled_ms", c_int64),
        ("filename", c_char * 256),
    ]


class LiveViewInfoStruct(ctypes.Structure):
    _fields_ = [
        ("info_ok", c_int32),
        ("vendor_error", c_int32),
        ("width", c_uint32),
        ("height", c_uint32),
        ("buffer_size", c_uint32),
        ("reserved", c_uint32),
    ]


class FrameInfoStruct(ctypes.Structure):
    _fields_ = [
        ("byte_length", c_uint32),
        ("frame_number", c_uint32),
        ("width", c_uint32),
        ("height", c_uint32),
        ("time_code", c_uint32),
        ("reserved", c_uint32),
        ("fetched_ms", c_int64),
    ]


class EventStruct(ctypes.Structure):
    _fields_ = [
        ("kind", c_int32),
        ("reserved", c_int32),
        ("timestamp_ms", c_int64),
        ("code", c_uint32),
        ("i0", c_int32),
        ("i1", c_int32),
        ("i2", c_int32),
        ("i3", c_int64),
    ]


def _library_filename() -> str:
    if sys.platform == "win32":
        return "crsdkpy_bridge.dll"
    if sys.platform == "darwin":
        return "crsdkpy_bridge.dylib"
    return "crsdkpy_bridge.so"


def find_library(explicit: Optional[str] = None) -> Optional[str]:
    """Locate the built bridge, or return ``None``.

    Search order: an explicit path, then ``CRSDKPY_BRIDGE``, then the usual
    build output directories next to the repository.
    """
    # An explicit path is exactly that: if it is wrong, say so rather than
    # quietly loading some other library that happens to be lying around.
    if explicit:
        return os.path.abspath(explicit) if os.path.isfile(explicit) else None

    candidates = []
    from_env = os.environ.get(LIBRARY_ENV_VAR)
    if from_env:
        candidates.append(from_env)

    name = _library_filename()
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    for relative in (
        os.path.join("native", "build", "Release", name),
        os.path.join("native", "build", "Debug", name),
        os.path.join("native", "build", name),
    ):
        candidates.append(os.path.join(repo_root, relative))

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _declare(lib: ctypes.CDLL) -> None:
    lib.crsdkpy_abi_version.restype = c_int32
    lib.crsdkpy_abi_version.argtypes = []

    lib.crsdkpy_last_error.restype = c_int32
    lib.crsdkpy_last_error.argtypes = [c_char_p, c_uint32]

    lib.crsdkpy_init.restype = c_int32
    lib.crsdkpy_init.argtypes = [c_char_p]

    lib.crsdkpy_shutdown.restype = c_int32
    lib.crsdkpy_shutdown.argtypes = []

    lib.crsdkpy_enumerate.restype = c_int32
    lib.crsdkpy_enumerate.argtypes = [c_int32, POINTER(c_uint32)]

    lib.crsdkpy_camera_at.restype = c_int32
    lib.crsdkpy_camera_at.argtypes = [c_uint32, POINTER(CameraInfoStruct)]

    lib.crsdkpy_open_session.restype = c_int32
    lib.crsdkpy_open_session.argtypes = [c_char_p, c_int32, POINTER(c_uint64)]

    lib.crsdkpy_close_session.restype = c_int32
    lib.crsdkpy_close_session.argtypes = [c_uint64]

    lib.crsdkpy_connection_state.restype = c_int32
    lib.crsdkpy_connection_state.argtypes = [c_uint64, POINTER(c_int32)]

    lib.crsdkpy_poll_events.restype = c_int32
    lib.crsdkpy_poll_events.argtypes = [
        c_uint64,
        POINTER(EventStruct),
        c_uint32,
        POINTER(c_uint32),
        c_int32,
    ]

    lib.crsdkpy_property_count.restype = c_int32
    lib.crsdkpy_property_count.argtypes = [c_uint64, POINTER(c_uint32)]

    lib.crsdkpy_list_properties.restype = c_int32
    lib.crsdkpy_list_properties.argtypes = [
        c_uint64,
        POINTER(PropertyStruct),
        c_uint32,
        POINTER(c_uint32),
    ]

    lib.crsdkpy_get_property.restype = c_int32
    lib.crsdkpy_get_property.argtypes = [c_uint64, c_uint32, POINTER(PropertyStruct)]

    lib.crsdkpy_set_property.restype = c_int32
    lib.crsdkpy_set_property.argtypes = [c_uint64, c_uint32, c_int64]

    lib.crsdkpy_send_command.restype = c_int32
    lib.crsdkpy_send_command.argtypes = [c_uint64, c_uint32, c_int32]

    lib.crsdkpy_list_content.restype = c_int32
    lib.crsdkpy_list_content.argtypes = [
        c_uint64,
        c_uint32,
        c_uint32,
        POINTER(ContentStruct),
        c_uint32,
        POINTER(c_uint32),
    ]

    lib.crsdkpy_fetch_content_preview.restype = c_int32
    lib.crsdkpy_fetch_content_preview.argtypes = [
        c_uint64,
        c_uint32,
        c_uint32,
        c_uint32,
        c_int32,
        c_int32,
        POINTER(PreviewInfoStruct),
    ]

    lib.crsdkpy_take_content_preview.restype = c_int32
    lib.crsdkpy_take_content_preview.argtypes = [
        c_uint64,
        POINTER(c_uint8),
        c_uint32,
        POINTER(c_uint32),
    ]

    lib.crsdkpy_configure_postview.restype = c_int32
    lib.crsdkpy_configure_postview.argtypes = [c_uint64, c_int32, c_int32]

    lib.crsdkpy_pull_postview.restype = c_int32
    lib.crsdkpy_pull_postview.argtypes = [c_uint64, POINTER(PostviewInfoStruct)]

    lib.crsdkpy_take_postview.restype = c_int32
    lib.crsdkpy_take_postview.argtypes = [
        c_uint64,
        POINTER(c_uint8),
        c_uint32,
        POINTER(c_uint32),
    ]

    lib.crsdkpy_get_live_view_info.restype = c_int32
    lib.crsdkpy_get_live_view_info.argtypes = [c_uint64, POINTER(LiveViewInfoStruct)]

    lib.crsdkpy_get_live_view_frame.restype = c_int32
    lib.crsdkpy_get_live_view_frame.argtypes = [
        c_uint64,
        POINTER(c_uint8),
        c_uint32,
        POINTER(FrameInfoStruct),
    ]


def load_bridge(path: Optional[str] = None) -> ctypes.CDLL:
    """Load and validate the native bridge.

    Raises :class:`~crsdkpy.errors.SDKNotFoundError` when it is not built, and
    :class:`~crsdkpy.errors.NativeBackendError` on an ABI mismatch.
    """
    resolved = find_library(path)
    if resolved is None:
        raise SDKNotFoundError(
            "The CrSDKPy native bridge library was not found. Build it with:\n"
            "  cmake -S native -B native/build -DCRSDK_ROOT=/path/to/CrSDK/RemoteCli\n"
            "  cmake --build native/build --config Release\n"
            f"or set {LIBRARY_ENV_VAR} to the built "
            f"{_library_filename()}. The Sony Camera Remote SDK is not "
            "distributed with CrSDKPy and must be obtained from Sony.",
            operation="native.load",
        )

    # The vendor runtime sits next to the bridge; on Windows it must be on the
    # DLL search path before loading.
    directory = os.path.dirname(resolved)
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(directory)
        except OSError:  # pragma: no cover - directory always exists here
            pass

    try:
        lib = ctypes.CDLL(resolved)
    except OSError as exc:
        raise NativeBackendError(
            f"Failed to load the native bridge at {resolved}: {exc}. "
            "The Sony runtime libraries usually need to sit alongside it.",
            operation="native.load",
        ) from exc

    _declare(lib)

    version = lib.crsdkpy_abi_version()
    major = (version >> 16) & 0xFFFF
    minor = version & 0xFFFF
    if major != ABI_VERSION_MAJOR:
        raise NativeBackendError(
            f"Native bridge ABI mismatch: library reports {major}.{minor}, "
            f"this CrSDKPy expects major version {ABI_VERSION_MAJOR}. "
            "Rebuild the bridge from this checkout.",
            operation="native.load",
        )
    return lib


def last_error(lib: ctypes.CDLL) -> str:
    buffer = ctypes.create_string_buffer(512)
    lib.crsdkpy_last_error(buffer, c_uint32(len(buffer)))
    return buffer.value.decode("utf-8", "replace")


def check(lib: ctypes.CDLL, status: int, operation: str) -> None:
    """Translate a status code into the appropriate exception.

    Positive values are vendor error codes and are preserved on the exception
    so a caller can inspect the original failure.
    """
    if status == OK:
        return

    detail = last_error(lib)
    suffix = f": {detail}" if detail else ""

    if status > 0:
        raise CameraConnectionError(
            f"the camera SDK rejected {operation}{suffix}",
            operation=operation,
            backend_code=status,
        )

    if status == ERR_INVALID_HANDLE:
        raise SessionClosedError(
            "the session handle is closed or stale", operation=operation
        )
    if status == ERR_NOT_CONNECTED:
        raise CameraConnectionError(
            "the session is not connected", operation=operation
        )
    if status == ERR_NOT_INITIALIZED:
        raise NativeBackendError(
            "the native bridge has not been initialised", operation=operation
        )
    if status == ERR_NOT_FOUND:
        raise PropertyNotSupportedError(
            f"{operation} found no matching item{suffix}"
        )
    if status == ERR_TIMEOUT:
        raise OperationTimeoutError(
            f"{operation} timed out{suffix}", operation=operation
        )
    if status == ERR_UNSUPPORTED:
        raise UnsupportedOperationError(
            f"{operation} is not supported by this camera{suffix}",
            operation=operation,
        )
    if status == ERR_CONNECT_FAILED:
        raise CameraConnectionError(
            f"the camera connection could not be established{suffix}",
            operation=operation,
        )
    if status == ERR_SDK_INIT_FAILED:
        raise NativeBackendError(
            f"the Sony SDK failed to initialise{suffix}", operation=operation
        )

    raise NativeBackendError(
        f"{operation} failed with bridge status {status}{suffix}",
        operation=operation,
        backend_code=status,
    )
