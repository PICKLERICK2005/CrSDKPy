"""Wire protocol for the out-of-process host.

Mirrors ``native/host/ipc_protocol.h``. There is no JSON: metadata and payload
items are fixed-layout POD, so both sides describe the format once and neither
needs a parser.

    [ header 24 bytes ][ meta meta_len bytes ][ blob blob_len bytes ]

Keeping ``meta`` and ``blob`` lengths separate is what lets image buffers -
previews, postview, live-view frames - travel as themselves rather than encoded
into a text payload.
"""

from __future__ import annotations

import ctypes
import struct
from typing import Optional

__all__ = [
    "ContentArgsStruct",
    "HeaderStruct",
    "HelloAckStruct",
    "HelloStruct",
    "ProtocolError",
    "RequestStruct",
    "ResponseStruct",
    "decode",
    "encode",
    "read_frame",
]

MAGIC = 0x43525059  # 'CRPY'
VERSION_MAJOR = 1
VERSION_MINOR = 0

MAX_META = 1 << 16
MAX_BLOB = 64 << 20

MSG_HELLO = 1
MSG_HELLO_ACK = 2
MSG_REQUEST = 3
MSG_RESPONSE = 4
MSG_EVENT = 5
MSG_BYE = 6

OP_PING = 1
OP_INIT = 2
OP_SHUTDOWN = 3
OP_ENUMERATE = 4
OP_CAMERA_AT = 5
OP_OPEN_SESSION = 6
OP_CLOSE_SESSION = 7
OP_CONNECTION_STATE = 8
OP_POLL_EVENTS = 9
OP_LIST_PROPERTIES = 10
OP_GET_PROPERTY = 11
OP_SET_PROPERTY = 12
OP_SEND_COMMAND = 13
OP_LIST_CONTENT = 14
OP_CONTENT_PREVIEW = 15
OP_CONFIGURE_POSTVIEW = 16
OP_PULL_POSTVIEW = 17
OP_LIVE_VIEW_INFO = 18
OP_LIVE_VIEW_FRAME = 19
OP_TAKE_TRANSFER_PATH = 20
OP_PROPERTY_STRING = 21
OP_PROPERTY_VALUES = 22
OP_TEST_CRASH = 900

CAT_NONE = 0
CAT_VENDOR = 1
CAT_INVALID_ARG = 2
CAT_STALE_HANDLE = 3
CAT_NOT_STARTED = 4
CAT_UNSUPPORTED = 5
CAT_SDK_MISSING = 6
CAT_ADAPTER_PATH = 7
CAT_NOT_CONNECTED = 8
CAT_TIMEOUT = 9
CAT_NOT_FOUND = 10
CAT_BUSY = 11
CAT_CONNECT_TIMEOUT = 12

#: magic, version_major, message_type, request_id, meta_len, blob_len, reserved
_HEADER = struct.Struct("<IHHIIII")
HEADER_SIZE = _HEADER.size  # 24


class ProtocolError(Exception):
    """The stream is malformed, desynchronised or speaks another version."""


class HeaderStruct(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version_major", ctypes.c_uint16),
        ("message_type", ctypes.c_uint16),
        ("request_id", ctypes.c_uint32),
        ("meta_len", ctypes.c_uint32),
        ("blob_len", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class HelloStruct(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("version_major", ctypes.c_uint16),
        ("version_minor", ctypes.c_uint16),
        ("reserved", ctypes.c_uint32),
    ]


class HelloAckStruct(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("protocol_major", ctypes.c_uint16),
        ("protocol_minor", ctypes.c_uint16),
        ("abi_major", ctypes.c_uint16),
        ("abi_minor", ctypes.c_uint16),
        ("host_version", ctypes.c_uint32),
        ("sdk_available", ctypes.c_int32),
        ("host_build", ctypes.c_char * 64),
        ("sdk_note", ctypes.c_char * 192),
    ]


class RequestStruct(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("op", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("u32_arg", ctypes.c_uint32),
        ("i32_arg", ctypes.c_int32),
        ("i32_arg2", ctypes.c_int32),
        ("handle", ctypes.c_uint64),
        ("text", ctypes.c_char * 208),
    ]


class ResponseStruct(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("status", ctypes.c_int32),
        ("category", ctypes.c_int32),
        ("count", ctypes.c_uint32),
        ("item_size", ctypes.c_uint32),
        ("handle", ctypes.c_uint64),
        ("i32_result", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("message", ctypes.c_char * 512),
    ]


class ContentArgsStruct(ctypes.Structure):
    """Arguments the fixed request struct has no room for.

    Sent immediately after :class:`RequestStruct` in the same meta payload.
    Appending rather than widening keeps both struct layouts frozen, so this is
    an additive change and the protocol version does not move.
    """

    _pack_ = 1
    _fields_ = [
        ("slot", ctypes.c_uint32),
        ("content_id", ctypes.c_uint32),
        ("file_id", ctypes.c_uint32),
        ("after_content_id", ctypes.c_uint32),
        ("kind", ctypes.c_int32),
        ("timeout_ms", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
    ]


def encode(
    message_type: int,
    request_id: int,
    meta: bytes = b"",
    blob: bytes = b"",
) -> bytes:
    """Build one framed message."""
    if len(meta) > MAX_META:
        raise ProtocolError(f"metadata payload too large: {len(meta)}")
    if len(blob) > MAX_BLOB:
        raise ProtocolError(f"binary payload too large: {len(blob)}")
    header = _HEADER.pack(
        MAGIC, VERSION_MAJOR, message_type, request_id, len(meta), len(blob), 0
    )
    return header + meta + blob


def decode(header_bytes: bytes) -> tuple[int, int, int, int]:
    """Parse a header, returning ``(message_type, request_id, meta, blob)``.

    Raises :class:`ProtocolError` on a bad magic, a version mismatch, or a
    length that would demand an implausible allocation.
    """
    if len(header_bytes) != HEADER_SIZE:
        raise ProtocolError(
            f"header must be {HEADER_SIZE} bytes, got {len(header_bytes)}"
        )
    magic, version, message_type, request_id, meta_len, blob_len, _ = _HEADER.unpack(
        header_bytes
    )
    if magic != MAGIC:
        raise ProtocolError(
            f"bad frame magic 0x{magic:08X}; the stream is desynchronised or "
            "is not a CrSDKPy host"
        )
    if version != VERSION_MAJOR:
        raise ProtocolError(
            f"host speaks protocol major {version}, this build expects "
            f"{VERSION_MAJOR}"
        )
    if meta_len > MAX_META or blob_len > MAX_BLOB:
        raise ProtocolError(
            f"frame declares an implausible size (meta={meta_len}, blob={blob_len})"
        )
    return message_type, request_id, meta_len, blob_len


def read_frame(stream) -> Optional[tuple[int, int, bytes, bytes]]:
    """Read one whole frame, or ``None`` at a clean EOF.

    A partial frame is an error: a truncated stream means the peer died
    mid-write, which must not be mistaken for a graceful close.
    """
    header_bytes = _read_exact(stream, HEADER_SIZE, allow_eof=True)
    if header_bytes is None:
        return None
    message_type, request_id, meta_len, blob_len = decode(header_bytes)
    meta = _read_exact(stream, meta_len) if meta_len else b""
    blob = _read_exact(stream, blob_len) if blob_len else b""
    return message_type, request_id, meta, blob


def _read_exact(stream, count: int, *, allow_eof: bool = False) -> Optional[bytes]:
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            if allow_eof and remaining == count:
                return None  # nothing at all: a clean close
            raise ProtocolError(
                f"stream ended mid-frame: wanted {count} bytes, got "
                f"{count - remaining}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def as_bytes(structure: ctypes.Structure) -> bytes:
    return bytes(memoryview(structure).cast("B"))


def from_bytes(structure_type, data: bytes):
    if len(data) < ctypes.sizeof(structure_type):
        raise ProtocolError(
            f"payload too short for {structure_type.__name__}: {len(data)} bytes"
        )
    instance = structure_type()
    ctypes.memmove(ctypes.byref(instance), data, ctypes.sizeof(structure_type))
    return instance


def unpack_int64_array(blob: bytes, count: int) -> list:
    """Read *count* little-endian int64 values out of a response blob."""
    width = 8
    usable = min(count, len(blob) // width)
    return list(struct.unpack_from(f"<{usable}q", blob)) if usable else []


def unpack_array(structure_type, blob: bytes, count: int):
    """Split a blob into *count* fixed-size POD items."""
    size = ctypes.sizeof(structure_type)
    if count and len(blob) < size * count:
        raise ProtocolError(
            f"blob holds {len(blob)} bytes, expected {size * count} for "
            f"{count} x {structure_type.__name__}"
        )
    return [
        from_bytes(structure_type, blob[i * size : (i + 1) * size])
        for i in range(count)
    ]
