"""Owned staged downloads. Local validation is not a camera-side hash."""

from __future__ import annotations

import itertools
import os
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .backend.contract import ContentRef
from .enums import TransferOutcome
from .errors import BackendError, CameraBusyError, OperationTimeoutError
from .events import TransferEvent

if TYPE_CHECKING:
    from .capture import CapturedContent
    from .session import Session

__all__ = ["ContentDownload"]
_ids = itertools.count(1)
_WINDOWS_DOWNLOAD_PATHS = os.name == "nt"
# Conservative policy, not a claimed universal Sony/Win32 maximum.
_WINDOWS_DOWNLOAD_PATH_LIMIT = 239


def _validate_download_path(path):
    if not _WINDOWS_DOWNLOAD_PATHS:
        return
    units = len(os.fspath(path).encode("utf-16-le")) // 2
    if units > _WINDOWS_DOWNLOAD_PATH_LIMIT:
        raise ValueError(
            f"Sony original transfer needs a shorter destination directory: "
            f"the complete generated staging path is {units} UTF-16 units; "
            f"the Windows safety budget is {_WINDOWS_DOWNLOAD_PATH_LIMIT}. "
            "Choose a shorter host directory; no transfer was started."
        )


def _check_redirects(path):
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise OSError(f"download path is redirected: {item}")
        if os.name == "nt":
            try:
                attributes = item.lstat().st_file_attributes
            except FileNotFoundError:
                continue
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(f"download path contains a reparse point: {item}")


class ContentDownload:
    """One owned transfer; uncertain writers retain quarantined staging."""

    def __init__(
        self,
        session: Session,
        content: CapturedContent,
        destination: Path,
        *,
        overwrite: bool = False,
    ):
        self._session, self.content, self.destination = session, content, destination
        self._target, self._overwrite = destination.absolute(), overwrite
        self._operation_id = next(_ids)
        if self._operation_id >= 1 << 63:
            raise OverflowError("download identities exhausted")
        _check_redirects(self._target)
        self._staging_dir = Path(
            tempfile.mkdtemp(prefix=".crsdkpy-download-", dir=self._target.parent)
        ).absolute()
        self._staging_path = self._staging_dir / (
            f"{self._operation_id:016x}-{uuid.uuid4().hex}" + self._target.suffix
        )
        try:
            _validate_download_path(self._staging_path)
        except ValueError as error:
            # No backend call has occurred. Only remove our empty allocation;
            # never apply the uncertain-writer quarantine policy to a refusal.
            try:
                self._staging_dir.rmdir()
            except OSError as cleanup_error:
                error.preflight_cleanup_error = cleanup_error
                error.staging_path = self._staging_dir
            raise
        self.outcome: Optional[TransferOutcome] = None
        self.percent = 0
        self.backend_code: Optional[int] = None
        self.completed_path: Optional[Path] = None
        self.quarantine_path: Optional[Path] = None
        self._terminal = self._cancel_requested = False
        self._failure = None
        self._wait_lock, self._cancel_lock = threading.Lock(), threading.Lock()

    @property
    def finished(self):
        return self._terminal

    @property
    def successful(self):
        return self.completed_path is not None and self.outcome is TransferOutcome.OK

    def _quarantine(self, error=None):
        self.quarantine_path = self._staging_dir
        if error is not None:
            error.staging_path, error.transfer = self._staging_path, self

    def _start(self):
        try:
            self._session._backend.start_owned_content_download(
                self._session._id,
                ContentRef(
                    content_id=self.content.content_id,
                    file_id=self.content.file_id,
                    path=self.content.path,
                    file_size=self.content.file_size,
                    slot=self.content.slot,
                ),
                str(self._staging_path),
                self._operation_id,
            )
        except BaseException as error:
            # A request error can follow an actual writer start. Keep ownership
            # and staging, and block a new transfer until the session is closed.
            self._failure = error
            self._quarantine(error)
            raise

    def cancel(self):
        # The native cancellation API acts on the active session transfer.
        # Hold the start lock through its response, so a stale canceller cannot
        # reach a new request after this operation finishes in another thread.
        with self._session.content._download_lock, self._cancel_lock:
            if not self.finished and not self._cancel_requested:
                try:
                    self._session._check_usable("content.download.cancel")
                    self._session._backend.cancel_content_download(self._session._id)
                except BaseException as error:
                    self._quarantine(error)
                    raise
                self._cancel_requested = True

    def _publish(self, reported):
        if not reported or Path(reported).absolute() != self._staging_path:
            raise BackendError(
                "completion path does not identify this staging file",
                operation="content.download",
            )
        _check_redirects(self._staging_path)
        before = self._staging_path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BackendError("completed original is not a private regular file")
        size = self.content.file_size
        if before.st_size <= 0 or (
            type(size) is int and size > 0 and before.st_size != size
        ):
            raise BackendError("completed original size differs from camera identity")
        with self._staging_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                raise BackendError("original changed during validation")
            if os.name != "nt":
                os.fsync(handle.fileno())
        after = self._staging_path.lstat()
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise BackendError("original changed before publication")
        _check_redirects(self._target)
        if self._overwrite:
            os.replace(self._staging_path, self._target)
        elif os.name == "nt":
            os.rename(self._staging_path, self._target)
        else:
            os.link(self._staging_path, self._target)
        # Publication committed. Cleanup failure must not report a failed copy.
        try:
            self._staging_path.unlink(missing_ok=True)
            self._staging_dir.rmdir()
            self.quarantine_path = None
        except OSError:
            self._quarantine()
        return self.destination

    def wait(self, *, timeout_ms=60_000):
        if not self._wait_lock.acquire(blocking=False):
            raise CameraBusyError("another waiter owns this transfer")
        try:
            return self._wait(timeout_ms)
        except BaseException as error:
            if not self.successful:
                self._quarantine(error)
            raise
        finally:
            self._wait_lock.release()

    def _wait(self, timeout_ms):
        if self.completed_path is not None:
            return self.completed_path
        if self._failure is not None:
            raise self._failure
        session, clock = self._session, self._session._backend.clock
        deadline = clock.now_ms() + max(0, int(timeout_ms))
        while not self._terminal:
            session._check_usable("content.download.wait")
            remaining = deadline - clock.now_ms()
            if remaining <= 0:
                raise OperationTimeoutError(
                    "transfer timeout; staging retained",
                    operation="content.download",
                    timeout_ms=timeout_ms,
                )
            session._pump(timeout_ms=min(remaining, 250))
            retained = []
            while session._op_events:
                event = session._op_events.popleft()
                if (
                    isinstance(event, TransferEvent)
                    and event._operation_id == self._operation_id
                ):
                    if not self._terminal and self._failure is None:
                        self.percent, self.backend_code = (
                            event.percent,
                            event.notify_code,
                        )
                        self.outcome = event.outcome
                        if event.outcome is TransferOutcome.UNKNOWN:
                            self._failure = BackendError(
                                "unknown transfer result; writer state is uncertain"
                            )
                        elif event.outcome.finished:
                            if event.outcome is TransferOutcome.OK:
                                try:
                                    self.completed_path = self._publish(
                                        session._backend.take_transfer_path(session._id)
                                    )
                                except BaseException as error:
                                    self.outcome, self._failure = (
                                        TransferOutcome.FAILED,
                                        error,
                                    )
                            # Do not release the next start until the owned
                            # path is collected and publication is resolved.
                            self._terminal = True
                else:
                    retained.append(event)
            session._op_events.extendleft(reversed(retained))
            if self._failure is not None:
                raise self._failure
        if self._failure is not None:
            raise self._failure
        if self.successful:
            return self.completed_path
        error_type = (
            CameraBusyError if self.outcome is TransferOutcome.BUSY else BackendError
        )
        raise error_type(
            f"original-file transfer ended with {self.outcome.value}",
            operation="content.download",
            backend_code=self.backend_code,
        )
