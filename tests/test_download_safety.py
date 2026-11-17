"""Temporary synthetic originals; no camera or accepted runtime access."""

from dataclasses import replace
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.enums import TransferOutcome
from crsdkpy.events import TransferEvent, WarningEvent
from crsdkpy.simulator import Scenario


def content(session):
    return session.capture().wait_for_content()


def test_only_complete_validated_original_becomes_visible(tmp_path, transfer_session):
    source = content(transfer_session)
    target = tmp_path / "original.arw"
    transfer = transfer_session.content.download(source, target)
    assert not target.exists() and not transfer.successful
    assert transfer._staging_path.parent != target.parent
    assert transfer._staging_path.suffix == target.suffix
    assert transfer.wait() == target
    assert target.stat().st_size == source.file_size
    assert transfer.successful and not transfer._staging_dir.exists()


def test_competing_creator_is_never_replaced(tmp_path, transfer_session):
    target = tmp_path / "original.arw"
    transfer = transfer_session.content.download(content(transfer_session), target)
    target.write_bytes(b"race winner")
    with pytest.raises(FileExistsError):
        transfer.wait()
    assert target.read_bytes() == b"race winner"
    assert not transfer.successful and transfer.finished
    assert transfer.quarantine_path.exists() and transfer._staging_path.is_file()


@pytest.mark.parametrize("outcome", ["failed", "busy", "storage_full"])
def test_failed_overwrite_preserves_destination(tmp_path, outcome):
    with make_sdk(scenario=Scenario(transfer_outcome=outcome)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            target = tmp_path / "original.arw"
            target.write_bytes(b"existing original")
            transfer = session.content.download(
                content(session), target, overwrite=True
            )
            with pytest.raises((crsdkpy.BackendError, crsdkpy.CameraBusyError)):
                transfer.wait()
            assert target.read_bytes() == b"existing original"
            assert not transfer.successful


def test_overwrite_commits_only_after_validation(tmp_path, transfer_session):
    target = tmp_path / "original.arw"
    target.write_bytes(b"existing original")
    transfer = transfer_session.content.download(
        content(transfer_session), target, overwrite=True
    )
    assert target.read_bytes() == b"existing original"
    transfer.wait()
    assert target.read_bytes().startswith(b"II*") and transfer.successful


@pytest.mark.parametrize(
    "fault", ["none", "wrong", "missing", "partial", "empty", "directory", "hardlink"]
)
def test_invalid_completion_never_publishes(
    tmp_path, transfer_session, monkeypatch, fault
):
    session = transfer_session
    target, external = tmp_path / "original.arw", tmp_path / "other.arw"
    external.write_bytes(b"unrelated original")
    transfer = session.content.download(content(session), target)
    take = session._backend.take_transfer_path

    def corrupt(handle):
        reported = take(handle)
        path = Path(reported)
        if fault == "none":
            return None
        if fault == "wrong":
            return str(external)
        if fault in ("missing", "directory", "hardlink"):
            path.unlink()
            if fault == "directory":
                path.mkdir()
            elif fault == "hardlink":
                os.link(external, path)
        elif fault in ("partial", "empty"):
            path.write_bytes(b"short" if fault == "partial" else b"")
        return reported

    monkeypatch.setattr(session._backend, "take_transfer_path", corrupt)
    with pytest.raises((crsdkpy.BackendError, OSError)):
        transfer.wait()
    assert not target.exists() and not transfer.successful
    assert external.read_bytes() == b"unrelated original"
    assert transfer.quarantine_path.exists()


@pytest.mark.parametrize("expected", [None, 0])
def test_unknown_size_requires_nonempty_regular_file_not_fabricated_hash(
    tmp_path, transfer_session, expected
):
    source = replace(content(transfer_session), file_size=expected)
    transfer = transfer_session.content.download(source, tmp_path / "original.arw")
    assert transfer.wait().stat().st_size > 0


@pytest.mark.parametrize("overwrite", [False, True])
def test_publication_failure_is_not_success(
    tmp_path, transfer_session, monkeypatch, overwrite
):
    from crsdkpy import transfers

    target = tmp_path / "original.arw"
    if overwrite:
        target.write_bytes(b"existing")
    transfer = transfer_session.content.download(
        content(transfer_session), target, overwrite=overwrite
    )

    def fail(*args):
        raise PermissionError("publication rejected")

    name = "replace" if overwrite else "rename" if os.name == "nt" else "link"
    monkeypatch.setattr(transfers.os, name, fail)
    with pytest.raises(PermissionError):
        transfer.wait()
    assert not transfer.successful and transfer.finished
    assert target.read_bytes() == b"existing" if overwrite else not target.exists()
    assert transfer._staging_path.is_file()


@pytest.mark.parametrize("owned_old", [False, True])
def test_stale_terminal_cannot_complete_a_new_transfer(
    tmp_path, monkeypatch, owned_old
):
    with make_sdk(scenario=Scenario(transfer_delay_ms=2000)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            target = tmp_path / "original.arw"
            source = content(session)
            transfer = session.content.download(source, target)
            stale = TransferEvent(
                timestamp_ms=10**15,
                outcome=TransferOutcome.OK,
                percent=100,
                has_path=True,
            )
            if owned_old:
                object.__setattr__(stale, "_operation_id", transfer._operation_id + 999)
            warning = WarningEvent(code=123)
            poll = sdk.backend.poll_events
            delivered = False

            def events(*args, **kwargs):
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return [stale, warning]
                return poll(*args, **kwargs)

            monkeypatch.setattr(sdk.backend, "poll_events", events)
            with pytest.raises(crsdkpy.OperationTimeoutError):
                transfer.wait(timeout_ms=100)
            assert not target.exists() and not transfer.finished
            assert not transfer.successful and stale in session._op_events
            public = session.drain_events()
            assert stale in public and warning in public
            assert transfer.quarantine_path.exists()
            with pytest.raises(crsdkpy.CameraBusyError):
                session.content.download(source, tmp_path / "second.arw")
            assert transfer.wait(timeout_ms=3000) == target


def test_ambiguous_start_keeps_staging_and_blocks_new_writer(
    tmp_path, transfer_session, monkeypatch
):
    session = transfer_session
    target = tmp_path / "original.arw"
    calls = []

    def partial_start(handle, ref, destination, operation_id):
        calls.append(destination)
        Path(destination).write_bytes(b"partial writer")
        raise crsdkpy.BackendError("uncertain start")

    monkeypatch.setattr(session._backend, "start_owned_content_download", partial_start)
    source = content(session)
    with pytest.raises(crsdkpy.BackendError) as error:
        session.content.download(source, target)
    stage = error.value.staging_path
    with pytest.raises(crsdkpy.CameraBusyError):
        session.content.download(source, tmp_path / "second.arw")
    assert len(calls) == 1 and not target.exists()
    session.close()
    assert stage.read_bytes() == b"partial writer"
    stage.write_bytes(b"late writer")  # closure never unlinks an uncertain writer
    assert not target.exists()


@pytest.mark.parametrize("action", ["cancel", "close", "timeout"])
def test_uncertain_or_cancelled_transfer_retains_safe_staging(tmp_path, action):
    with make_sdk(scenario=Scenario(transfer_delay_ms=2000)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            target = tmp_path / "original.arw"
            target.write_bytes(b"existing")
            transfer = session.content.download(
                content(session), target, overwrite=True
            )
            transfer._staging_path.write_bytes(b"partial")
            if action == "cancel":
                transfer.cancel()
                error_type = crsdkpy.BackendError
            elif action == "close":
                session.close()
                error_type = crsdkpy.SessionClosedError
            else:
                error_type = crsdkpy.OperationTimeoutError
            with pytest.raises(error_type):
                transfer.wait(timeout_ms=100 if action == "timeout" else 3000)
            assert not transfer.successful and target.read_bytes() == b"existing"
            assert transfer.quarantine_path.exists()
            assert transfer._staging_path.is_file()


def test_concurrent_starts_have_one_owner(tmp_path, transfer_session):
    source = content(transfer_session)

    def start(index):
        try:
            return transfer_session.content.download(source, tmp_path / f"{index}.arw")
        except crsdkpy.CameraBusyError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        operations = list(executor.map(start, (1, 2)))
    assert sum(op is not None for op in operations) == 1
    next(op for op in operations if op is not None).wait()


def test_unsupported_driver_refuses_before_start_or_staging(
    tmp_path, transfer_session, monkeypatch
):
    source = content(transfer_session)
    monkeypatch.setattr(transfer_session._backend, "supports_owned_downloads", False)
    with pytest.raises(crsdkpy.UnsupportedOperationError):
        transfer_session.content.download(source, tmp_path / "original.arw")
    assert list(tmp_path.iterdir()) == []


def test_unknown_terminal_does_not_release_session_ownership(tmp_path):
    with make_sdk(scenario=Scenario(transfer_outcome="unknown")) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            source = content(session)
            operation = session.content.download(source, tmp_path / "original.arw")
            with pytest.raises(crsdkpy.BackendError, match="uncertain"):
                operation.wait()
            assert not operation.successful and not operation.finished
            with pytest.raises(crsdkpy.CameraBusyError):
                session.content.download(source, tmp_path / "next.arw")


def test_host_operation_identity_preserves_high_bits(tmp_path, monkeypatch):
    from crsdkpy import transfers
    from test_host_ipc import _host_download

    identity = 0x12345678ABCDEF01
    monkeypatch.setattr(transfers, "_ids", iter([identity]))
    sdk, session, source, target = _host_download("normal", tmp_path)
    try:
        operation = session.content.download(source, target)
        assert operation.wait() == target and operation.successful
        events = session.drain_events()
        terminals = [
            e
            for e in events
            if isinstance(e, TransferEvent) and e.outcome is TransferOutcome.OK
        ]
        assert terminals and terminals[0]._operation_id == identity
        assert terminals[0].native is not None
    finally:
        sdk.close()


def test_interrupted_wait_keeps_writer_and_resumes_same_request(
    tmp_path, transfer_session, monkeypatch
):
    session = transfer_session
    target = tmp_path / "original.arw"
    target.write_bytes(b"existing")
    operation = session.content.download(content(session), target, overwrite=True)
    poll = session._backend.poll_events

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(session._backend, "poll_events", interrupt)
    with pytest.raises(KeyboardInterrupt):
        operation.wait()
    assert not operation.finished and not operation.successful
    assert operation.quarantine_path.exists()
    assert target.read_bytes() == b"existing"
    monkeypatch.setattr(session._backend, "poll_events", poll)
    assert operation.wait() == target
    assert operation.successful and operation.quarantine_path is None


def test_symlink_completion_is_rejected_without_touching_target(
    tmp_path, transfer_session, monkeypatch
):
    session = transfer_session
    external = tmp_path / "external.arw"
    external.write_bytes(b"external")
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(external)
    except OSError:
        pytest.skip("filesystem symlinks unavailable")
    probe.unlink()
    operation = session.content.download(content(session), tmp_path / "original.arw")
    take = session._backend.take_transfer_path

    def redirect(handle):
        path = Path(take(handle))
        path.unlink()
        path.symlink_to(external)
        return str(path)

    monkeypatch.setattr(session._backend, "take_transfer_path", redirect)
    with pytest.raises(OSError, match="redirected"):
        operation.wait()
    assert not operation.successful and external.read_bytes() == b"external"
    assert not (tmp_path / "original.arw").exists()


def test_finished_does_not_release_owner_before_publication(
    tmp_path, transfer_session, monkeypatch
):
    operation = transfer_session.content.download(
        content(transfer_session), tmp_path / "original.arw"
    )
    publish = operation._publish

    def checked(path):
        assert not operation.finished
        with pytest.raises(crsdkpy.CameraBusyError):
            transfer_session.content.download(operation.content, tmp_path / "next.arw")
        return publish(path)

    monkeypatch.setattr(operation, "_publish", checked)
    operation.wait()
    assert operation.finished and operation.successful


def test_late_cancel_cannot_reach_next_download(
    tmp_path, transfer_session, monkeypatch
):
    import threading

    session = transfer_session
    source = content(session)
    operation = session.content.download(source, tmp_path / "first.arw")
    cancel = session._backend.cancel_content_download
    start = session._backend.start_owned_content_download
    cancel_entered, release_cancel = threading.Event(), threading.Event()
    next_attempted, next_started = threading.Event(), threading.Event()

    def delayed_cancel(handle):
        cancel_entered.set()
        assert release_cancel.wait(5)
        cancel(handle)

    def started(*args):
        next_started.set()
        return start(*args)

    def next_download():
        next_attempted.set()
        return session.content.download(source, tmp_path / "second.arw")

    monkeypatch.setattr(session._backend, "cancel_content_download", delayed_cancel)
    monkeypatch.setattr(session._backend, "start_owned_content_download", started)
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancellation = executor.submit(operation.cancel)
        assert cancel_entered.wait(5)
        try:
            operation.wait()
            following = executor.submit(next_download)
            assert next_attempted.wait(5)
            assert not next_started.wait(0.1)
        finally:
            release_cancel.set()
        cancellation.result(timeout=5)
        second = following.result(timeout=5)
        assert second.wait().read_bytes().startswith(b"II*")


def test_redirected_parent_cannot_create_external_directories(
    tmp_path, transfer_session
):
    external, link = tmp_path / "external", tmp_path / "link"
    external.mkdir()
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(OSError, match="redirected|reparse"):
        transfer_session.content.download(
            content(transfer_session), link / "new" / "original.arw"
        )
    assert list(external.iterdir()) == []
