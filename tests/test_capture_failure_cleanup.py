"""Injected failures only: never exercise a physical shutter or backend."""

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.commands import Command, CommandParameter


@pytest.mark.parametrize("gated", [False, True])
@pytest.mark.parametrize("phase", ["down", "up", "poll"])
def test_release_errors_preserve_primary_and_never_retry(gated, phase, monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = sdk.backend
            send, poll = backend.send_command, backend.poll_events
            primary = crsdkpy.BackendError("injected release path error")
            attempts = []

            def command(handle, code, value):
                attempts.append((code, value))
                if (
                    phase == "down"
                    and value == CommandParameter.DOWN
                    or phase == "up"
                    and value == CommandParameter.UP
                ):
                    raise primary
                return send(handle, code, value)

            def events(handle, timeout_ms=0):
                if phase == "poll" and attempts:
                    raise primary
                return poll(handle, timeout_ms)

            monkeypatch.setattr(backend, "send_command", command)
            monkeypatch.setattr(backend, "poll_events", events)
            method = session.autofocus_and_capture if gated else session.capture
            with pytest.raises(crsdkpy.BackendError) as result:
                method()
            assert result.value is primary
            assert result.value.exposure_requested is True
            assert result.value.capture.exposed is False  # not confirmed, not no-shot
            assert result.value.release_command_completed is (phase == "poll")
            assert attempts == [(Command.RELEASE, CommandParameter.DOWN)] + (
                [] if phase == "down" else [(Command.RELEASE, CommandParameter.UP)]
            )
            if gated:
                assert backend.get_half_press(session._id) is False
                assert result.value.focus_cleanup_confirmed is True
                assert result.value.control_cleanup_confirmed is None


def test_secondary_cleanup_error_does_not_hide_release_error(monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = sdk.backend
            send, read = backend.send_command, backend.get_half_press
            primary, secondary = (
                crsdkpy.BackendError("release"),
                crsdkpy.BackendError("cleanup"),
            )
            attempted = False

            def command(*args):
                nonlocal attempted
                attempted = True
                raise primary

            def status(*args):
                if attempted:
                    raise secondary
                return read(*args)

            monkeypatch.setattr(backend, "send_command", command)
            monkeypatch.setattr(backend, "get_half_press", status)
            with pytest.raises(crsdkpy.BackendError) as result:
                session.autofocus_and_capture()
            assert result.value is primary
            assert result.value.cleanup_error is secondary
            assert result.value.control_cleanup_confirmed is False
            monkeypatch.setattr(backend, "send_command", send)


def test_post_release_autofocus_error_is_not_a_pre_shutter_rejection(monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = sdk.backend
            send, poll = backend.send_command, backend.poll_events
            attempted = False
            primary = crsdkpy.AutofocusFailedError("late fake status error")

            def command(*args):
                nonlocal attempted
                attempted = True
                return send(*args)

            def events(*args, **kwargs):
                if attempted:
                    raise primary
                return poll(*args, **kwargs)

            monkeypatch.setattr(backend, "send_command", command)
            monkeypatch.setattr(backend, "poll_events", events)
            with pytest.raises(crsdkpy.BackendError) as result:
                session.autofocus_and_capture()
            assert result.value.__cause__ is primary
            assert result.value.exposure_requested is True
            assert backend.get_half_press(session._id) is False


@pytest.mark.parametrize("mode", ["read_error", "unconfirmed"])
def test_cleanup_failure_retains_confirmed_exposure(monkeypatch, mode):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:

            def cleanup():
                if mode == "read_error":
                    raise crsdkpy.BackendError("cleanup status read failed")
                return False

            monkeypatch.setattr(session, "_release_half_press", cleanup)
            with pytest.raises(crsdkpy.BackendError) as result:
                session.autofocus_and_capture()
            assert result.value.capture.exposed is True
            assert result.value.control_cleanup_confirmed is False


@pytest.mark.parametrize(
    "error_type",
    [
        crsdkpy.UnsupportedOperationError,
        crsdkpy.CameraConnectionError,
        crsdkpy.OperationTimeoutError,
    ],
)
def test_typed_release_rejections_remain_uncertain(error_type, monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            primary = error_type("injected command failure")
            attempts = []

            def reject(*args):
                attempts.append(args)
                raise primary

            monkeypatch.setattr(sdk.backend, "send_command", reject)
            with pytest.raises(error_type) as result:
                session.autofocus_and_capture()
            assert result.value is primary and len(attempts) == 1
            assert result.value.exposure_requested is True
            assert result.value.exposure_confirmed is False
            assert result.value.focus_cleanup_confirmed is True
            assert result.value.control_cleanup_confirmed is None


def test_unsupported_cleanup_read_is_not_confirmed_release(monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            send, read = sdk.backend.send_command, sdk.backend.get_half_press
            attempted = False

            def command(*args):
                nonlocal attempted
                attempted = True
                return send(*args)

            def status(*args):
                if attempted:
                    raise crsdkpy.UnsupportedOperationError("temporarily restricted")
                return read(*args)

            monkeypatch.setattr(sdk.backend, "send_command", command)
            monkeypatch.setattr(sdk.backend, "get_half_press", status)
            with pytest.raises(crsdkpy.BackendError) as result:
                session.autofocus_and_capture()
            assert result.value.capture.exposed is True
            assert result.value.focus_cleanup_confirmed is False
            assert result.value.control_cleanup_confirmed is False
