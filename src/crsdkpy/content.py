"""Durable content on the camera's media.

Most callers never need this: a :class:`~crsdkpy.capture.Capture` resolves its
own content and previews. This exists for the cases that do — establishing a
baseline before shooting, re-fetching a preview later, checking what is already
on the card — so that doing so never means reaching into a backend.

Identifiers are monotonic but **not** contiguous. Detect new content with
``id > baseline``, never ``baseline + 1``.
"""

from __future__ import annotations

from collections.abc import Sequence
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

from .capture import CapturedContent
from .enums import PreviewKind
from .errors import CameraBusyError, UnsupportedOperationError
from .previews import Preview
from .transfers import ContentDownload

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = ["Content"]


class Content:
    """Content index access for one session."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._active_download: Optional[ContentDownload] = None

    @property
    def available(self) -> bool:
        """Whether this session can reach the content index at all."""
        return self._session.capabilities.content_index

    def _require(self, operation: str) -> None:
        session = self._session
        session._check_usable(operation)
        if not session.capabilities.content_index:
            raise UnsupportedOperationError(
                "the content index is not available in control mode "
                f"{session.mode.value!r}",
                capability="content_index",
                operation=operation,
            )

    def latest(self) -> Optional[CapturedContent]:
        """Newest item the camera reports, or ``None`` when there is none.

        The usual way to take a baseline before shooting.
        """
        self._require("content.latest")
        session = self._session
        ref = session._backend.latest_content(session._id)
        return _project(ref) if ref is not None else None

    def since(
        self, baseline: Optional[Union[int, CapturedContent]] = None
    ) -> Sequence[CapturedContent]:
        """Items newer than *baseline*, oldest first.

        ``None`` means everything the camera currently reports.
        """
        self._require("content.since")
        session = self._session
        if isinstance(baseline, CapturedContent):
            baseline = baseline.content_id
        refs = session._backend.list_content(session._id, newer_than=baseline)
        return tuple(_project(ref) for ref in refs)

    def preview(
        self,
        content: Union[int, CapturedContent],
        kind: PreviewKind = PreviewKind.SCREENNAIL,
    ) -> Preview:
        """Fetch an exact-still preview of one item.

        Identity is checked against what was asked for. Note that preview bytes
        are not a stable fingerprint: at least one camera regenerates an
        embedded identifier on every transfer, so two fetches of the same still
        differ. Compare :attr:`~crsdkpy.previews.Preview.content_id`, never a
        hash of the image.
        """
        self._require("content.preview")
        session = self._session
        content_id = (
            content.content_id if isinstance(content, CapturedContent) else int(content)
        )
        return session._backend.get_preview(session._id, content_id, kind)

    def download(
        self,
        content: CapturedContent,
        destination: Union[str, PathLike[str]],
        *,
        overwrite: bool = False,
    ) -> ContentDownload:
        """Start downloading the exact original file represented by *content*.

        *destination* may be a filename or an existing directory. Existing
        files are refused unless ``overwrite=True``. Only one original-file
        transfer may be active per session because Sony's completion callback
        does not carry a caller-defined request identifier.
        """
        self._require("content.download")
        if self._active_download is not None and not self._active_download.finished:
            raise CameraBusyError(
                "one original-file download is already active for this session",
                operation="content.download",
            )
        if not isinstance(content, CapturedContent):
            raise TypeError(
                "content.download requires a CapturedContent with file identity"
            )
        target = Path(destination)
        if target.is_dir():
            if not content.filename:
                raise ValueError("a directory destination requires content.filename")
            target = target / content.filename
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        session = self._session
        operation = ContentDownload(session, content, target)
        operation._start()
        self._active_download = operation
        return operation

    def __iter__(self):
        return iter(self.since())

    def __repr__(self) -> str:
        if not self.available:
            return "Content(unavailable in this control mode)"
        return f"Content(latest={self.latest()})"


def _project(ref) -> CapturedContent:
    return CapturedContent(
        content_id=ref.content_id,
        file_number=ref.file_number,
        path=ref.path,
        created_ms=ref.created_ms,
        file_id=ref.file_id,
        captured_at=ref.captured_at,
        width=ref.width,
        height=ref.height,
        file_size=ref.file_size,
        slot=ref.slot,
    )
