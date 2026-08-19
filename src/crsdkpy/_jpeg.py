"""Minimal JPEG inspection.

Only what CrSDKPy needs to answer two questions about bytes a camera handed
back: is this actually a JPEG, and what size is the image really?

Dimensions are read from the frame header rather than taken from whatever the
camera said alongside the bytes. Those are different claims: the content index
reports the dimensions of the *original still*, which are not the dimensions of
its screennail. Parsing is also the only way to notice a truncated transfer.

No third-party imaging dependency: the parse is a few dozen lines and CrSDKPy
stays installable with nothing but the standard library.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import NamedTuple, Optional

__all__ = [
    "JPEG_MAGIC",
    "SCAN_MARKER",
    "Segment",
    "is_jpeg",
    "jpeg_dimensions",
    "jpeg_segments",
]

#: Start of image. Every JPEG begins with these two bytes.
JPEG_MAGIC = b"\xff\xd8"

#: End of image.
_EOI = b"\xff\xd9"

#: Frame headers that carry the image geometry. Excludes 0xC4 (Huffman
#: tables), 0xC8 (reserved) and 0xCC (arithmetic coding conditioning), which
#: share the 0xCn range but are not frames.
_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)

#: Markers that stand alone, with no length field following them.
_STANDALONE = frozenset({0x01, 0xD8, 0xD9}) | frozenset(range(0xD0, 0xD8))


def is_jpeg(data: bytes) -> bool:
    """Whether *data* starts with the JPEG signature."""
    return len(data) >= 2 and data[:2] == JPEG_MAGIC


def jpeg_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """Return ``(width, height)`` from the frame header, or ``None``.

    ``None`` means the bytes are not a JPEG, or are truncated before the frame
    header. It never means "zero by zero": a caller checking for exact-still
    bytes needs to tell an unreadable image from a real one.
    """
    if not is_jpeg(data):
        return None

    offset = 2
    length = len(data)
    while offset + 1 < length:
        # Segments are separated by 0xFF; a run of them is legal padding.
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        offset += 2
        if marker in _STANDALONE:
            continue
        if offset + 2 > length:
            return None
        (segment_length,) = struct.unpack_from(">H", data, offset)
        if segment_length < 2:
            return None  # malformed: a segment always counts its own length
        if marker in _SOF_MARKERS:
            # precision (1 byte), height (2), width (2)
            if offset + 7 > length:
                return None
            height, width = struct.unpack_from(">HH", data, offset + 3)
            if width == 0 or height == 0:
                return None
            return width, height
        offset += segment_length
    return None


#: Pseudo-marker for the entropy-coded image data that follows a start-of-scan
#: segment. Not a real JPEG marker; it names the region that actually holds the
#: picture, as opposed to the metadata around it.
SCAN_MARKER = 0x100

#: Start of scan.
_SOS = 0xDA


class Segment(NamedTuple):
    """One region of a JPEG file.

    ``marker`` is the byte after ``0xFF``, or :data:`SCAN_MARKER` for the
    entropy-coded data. ``start`` and ``end`` bound the whole region including
    its two marker bytes.
    """

    marker: int
    start: int
    end: int

    @property
    def is_scan(self) -> bool:
        return self.marker == SCAN_MARKER

    @property
    def is_metadata(self) -> bool:
        """Application and comment segments: everything but the picture."""
        return 0xE0 <= self.marker <= 0xEF or self.marker == 0xFE

    @property
    def name(self) -> str:
        if self.marker == SCAN_MARKER:
            return "SCAN"
        return f"FF{self.marker:02X}"

    @property
    def length(self) -> int:
        return self.end - self.start


def jpeg_segments(data: bytes) -> Iterator[Segment]:
    """Walk the file's structure.

    Stops after the entropy-coded data, which is yielded as one region under
    :data:`SCAN_MARKER`. Scanning past it would mean interpreting compressed
    image bytes as markers.
    """
    if not is_jpeg(data):
        return
    offset = 2
    length = len(data)
    while offset + 1 < length:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        start = offset
        offset += 2
        if marker in _STANDALONE:
            yield Segment(marker, start, offset)
            continue
        if offset + 2 > length:
            return
        (segment_length,) = struct.unpack_from(">H", data, offset)
        if segment_length < 2:
            return
        end = min(offset + segment_length, length)
        yield Segment(marker, start, end)
        if marker == _SOS:
            # Everything to the end is compressed picture data.
            yield Segment(SCAN_MARKER, end, length)
            return
        offset = end


def looks_complete(data: bytes) -> bool:
    """Whether the bytes begin and end like a whole JPEG.

    A transfer cut short usually keeps a valid header, so the trailing marker
    is the cheap check that catches it.
    """
    return is_jpeg(data) and len(data) >= 4 and data[-2:] == _EOI
