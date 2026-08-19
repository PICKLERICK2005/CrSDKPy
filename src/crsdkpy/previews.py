"""Image representations returned by a camera.

Preview forms differ in whether they are guaranteed to depict the captured
exposure. A live-view frame near a capture is *not* the captured still; the
postview, thumbnail and screennail are. Sizes and dimensions vary by camera,
file type and mode, so nothing here asserts a fixed geometry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from .enums import PreviewKind

__all__ = ["LiveViewFrame", "Preview"]


@dataclass(frozen=True)
class Preview:
    """Encoded image bytes plus enough context to know what they depict."""

    kind: PreviewKind
    data: bytes
    mime: str = "image/jpeg"
    width: Optional[int] = None
    height: Optional[int] = None
    timestamp_ms: int = 0
    #: Content this preview belongs to, when the backend can associate it.
    content_id: Optional[int] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def byte_length(self) -> int:
        return len(self.data)

    @property
    def is_exact_still(self) -> bool:
        """Whether these bytes are guaranteed to be the captured exposure."""
        return self.kind.is_exact_still

    def __repr__(self) -> str:
        return (
            f"Preview({self.kind.value}, {self.byte_length} bytes, "
            f"{self.width}x{self.height}, exact={self.is_exact_still})"
        )


@dataclass(frozen=True)
class LiveViewFrame(Preview):
    """One live-view frame.

    Never an exact still, even when it arrives immediately after a capture.
    """

    kind: PreviewKind = PreviewKind.LIVE_VIEW
    data: bytes = b""
    frame_number: int = 0

    @property
    def is_exact_still(self) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            f"LiveViewFrame(#{self.frame_number}, {self.byte_length} bytes, "
            f"{self.width}x{self.height})"
        )
