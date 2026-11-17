from pathlib import Path

import pytest

import crsdkpy
from crsdkpy.simulator import Scenario


def _captured_content(session):
    capture = session.capture()
    return capture.wait_for_content()


def test_original_download_has_exact_identity_and_explicit_destination(tmp_path: Path):
    with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            content = _captured_content(session)
            target = tmp_path / "original.arw"
            transfer = session.content.download(content, target)
            assert transfer.wait() == target
            assert transfer.successful
            assert transfer.percent == 100
            assert target.read_bytes()[:4] == b"II*\x00"
            assert target.stat().st_size == content.file_size


def test_original_download_refuses_overwrite(tmp_path: Path):
    target = tmp_path / "existing.arw"
    target.write_bytes(b"keep")
    with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            with pytest.raises(FileExistsError):
                session.content.download(_captured_content(session), target)
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize(
    ("outcome", "error"),
    [("busy", crsdkpy.CameraBusyError), ("failed", crsdkpy.BackendError)],
)
def test_original_download_terminal_failures(tmp_path: Path, outcome, error):
    scenario = Scenario(transfer_outcome=outcome)
    with crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            transfer = session.content.download(
                _captured_content(session), tmp_path / "never.arw"
            )
            with pytest.raises(error):
                transfer.wait()


def test_original_download_cancellation(tmp_path: Path):
    scenario = Scenario(transfer_delay_ms=2_000)
    with crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            transfer = session.content.download(
                _captured_content(session), tmp_path / "canceled.arw"
            )
            transfer.cancel()
            with pytest.raises(crsdkpy.BackendError, match="canceled"):
                transfer.wait()


def test_wait_timeout_does_not_cancel_transfer(tmp_path: Path):
    scenario = Scenario(transfer_delay_ms=2_000)
    with crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            target = tmp_path / "later.arw"
            transfer = session.content.download(_captured_content(session), target)
            with pytest.raises(crsdkpy.OperationTimeoutError):
                transfer.wait(timeout_ms=100)
            assert not transfer.finished
            assert transfer.wait(timeout_ms=3_000) == target


def test_transfer_wait_after_close_fails_promptly(tmp_path):
    with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
        session = sdk.discover()[0].open("remote_transfer")
        transfer = session.content.download(
            _captured_content(session), tmp_path / "closed.arw"
        )
        session.close()
        with pytest.raises(crsdkpy.SessionClosedError):
            transfer.wait()


def test_transfer_wait_detects_close_during_event_pump(tmp_path, monkeypatch):
    with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            transfer = session.content.download(
                _captured_content(session), tmp_path / "closed.arw"
            )
            monkeypatch.setattr(session, "_pump", lambda **kwargs: session.close())
            with pytest.raises(crsdkpy.SessionClosedError):
                transfer.wait()


def test_rejected_cancel_remains_explicitly_cancellable(tmp_path, monkeypatch):
    with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            transfer = session.content.download(
                _captured_content(session), tmp_path / "cancel.arw"
            )
            cancel = sdk.backend.cancel_content_download
            calls = []

            def reject_once(handle):
                calls.append(handle)
                if len(calls) == 1:
                    raise crsdkpy.CameraBusyError("cancel rejected")
                cancel(handle)

            monkeypatch.setattr(sdk.backend, "cancel_content_download", reject_once)
            with pytest.raises(crsdkpy.CameraBusyError):
                transfer.cancel()
            assert transfer.quarantine_path.exists()
            assert len(calls) == 1  # no automatic retry
            transfer.cancel()
            transfer.cancel()
            assert len(calls) == 2
            with pytest.raises(crsdkpy.BackendError, match="canceled"):
                transfer.wait()
