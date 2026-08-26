"""Original-file transfer lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .backend.contract import ContentRef
from .enums import TransferOutcome
from .errors import BackendError, CameraBusyError, OperationTimeoutError
from .events import TransferEvent

if TYPE_CHECKING:  # pragma: no cover
    from .capture import CapturedContent
    from .session import Session

__all__ = ["ContentDownload"]


class ContentDownload:
    """One original-file transfer started by :meth:`Content.download`."""

    def __init__(self, session: Session, content: CapturedContent, destination: Path):
        self._session = session
        self.content = content
        self.destination = destination
        self.outcome: Optional[TransferOutcome] = None
        self.percent = 0
        self.backend_code: Optional[int] = None
        self.completed_path: Optional[Path] = None
        self._cancel_requested = False

    @property
    def finished(self) -> bool:
        return self.outcome is not None and self.outcome.finished

    @property
    def successful(self) -> bool:
        return self.outcome is TransferOutcome.OK

    def _start(self) -> None:
        session = self._session
        session._backend.start_content_download(
            session._id,
            ContentRef(
                content_id=self.content.content_id,
                file_id=self.content.file_id,
                path=self.content.path,
                file_size=self.content.file_size,
                slot=self.content.slot,
            ),
            str(self.destination),
        )

    def cancel(self) -> None:
        if not self.finished and not self._cancel_requested:
            self._cancel_requested = True
            self._session._backend.cancel_content_download(self._session._id)

    def wait(self, *, timeout_ms: int = 60_000) -> Path:
        """Wait for terminal transfer state and return the reported local path."""
        if self.completed_path is not None:
            return self.completed_path
        session = self._session
        clock = session._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while not self.finished:
            remaining = deadline - clock.now_ms()
            if remaining <= 0:
                raise OperationTimeoutError(
                    "original-file transfer did not finish before the deadline",
                    operation="content.download",
                    timeout_ms=timeout_ms,
                )
            session._pump(timeout_ms=min(remaining, 250))
            retained = []
            while session._op_events:
                event = session._op_events.popleft()
                if isinstance(event, TransferEvent):
                    if not self.finished:
                        self.outcome = event.outcome
                        self.percent = event.percent
                        self.backend_code = event.notify_code
                else:
                    retained.append(event)
            session._op_events.extendleft(reversed(retained))
        if self.outcome is TransferOutcome.OK:
            reported = session._backend.take_transfer_path(session._id)
            self.completed_path = Path(reported) if reported else self.destination
            return self.completed_path
        if self.outcome is TransferOutcome.BUSY:
            raise CameraBusyError(
                "camera was busy during original-file transfer",
                operation="content.download",
                backend_code=self.backend_code,
            )
        raise BackendError(
            f"original-file transfer ended with {self.outcome.value}",
            operation="content.download",
            backend_code=self.backend_code,
        )
