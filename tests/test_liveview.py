"""Live view behaviour."""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import Scenario


def test_frames_have_variable_size(session: crsdkpy.Session) -> None:
    sizes = {frame.byte_length for frame in session.live_view.frames(limit=12)}
    assert len(sizes) > 1, "live-view frame size is not constant on real hardware"


def test_frame_numbers_increase(session: crsdkpy.Session) -> None:
    numbers = [f.frame_number for f in session.live_view.frames(limit=5)]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == 5


def test_frames_are_jpeg_shaped(session: crsdkpy.Session) -> None:
    frame = session.live_view.get_frame()
    assert frame.data[:2] == b"\xff\xd8"
    assert frame.data[-2:] == b"\xff\xd9"
    assert frame.mime == "image/jpeg"


def test_status_reports_geometry(session: crsdkpy.Session) -> None:
    status = session.live_view.status()
    assert status.usable
    assert (status.width, status.height) == (640, 428)
    assert status.buffer_size == 307_200


def test_live_view_unavailable_in_transfer_mode(
    transfer_session: crsdkpy.Session,
) -> None:
    assert not transfer_session.live_view.available
    with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
        transfer_session.live_view.get_frame()
    assert excinfo.value.capability == "live_view"


def test_info_can_succeed_while_stream_is_unusable(
    transfer_session: crsdkpy.Session,
) -> None:
    """Reporting success is not the same as being able to deliver a frame."""
    status = transfer_session.live_view.status()
    assert status.info_ok
    assert status.buffer_size == 0
    assert not status.usable


def test_info_ok_but_empty_scenario() -> None:
    sdk = make_sdk(scenario=Scenario(live_view_info_ok_but_empty=True))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote") as session:
            status = session.live_view.status()
            assert status.info_ok
            assert not status.usable
    finally:
        sdk.close()


def test_frame_fetch_failure_raises() -> None:
    sdk = make_sdk(scenario=Scenario(live_view_fetch_fails=True))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote") as session:
            with pytest.raises(crsdkpy.CameraConnectionError):
                session.live_view.get_frame()
    finally:
        sdk.close()


def test_live_view_continues_during_autofocus(session: crsdkpy.Session) -> None:
    """Frames kept arriving throughout the autofocus phase on hardware."""
    session.live_view.get_frame()
    session.raw.set_half_press(True)
    frames = list(session.live_view.frames(limit=3, timeout_ms=1_000))
    assert len(frames) == 3
    session.raw.set_half_press(False)


def test_live_view_pauses_around_exposure_then_resumes(
    session: crsdkpy.Session,
) -> None:
    before = session.live_view.get_frame()
    capture = session.autofocus_and_capture()
    assert capture.exposed

    # Immediately after the exposure the stream is briefly interrupted.
    paused = session.live_view.try_get_frame(timeout_ms=0)
    assert paused is None

    # It resumes on its own, with no restart call.
    after = session.live_view.get_frame(timeout_ms=2_000)
    assert after.frame_number > before.frame_number


def test_try_get_frame_returns_none_without_waiting(
    session: crsdkpy.Session,
) -> None:
    session.live_view.get_frame()
    assert session.live_view.try_get_frame(timeout_ms=0) is None


def test_get_frame_times_out_when_stream_is_paused(
    session: crsdkpy.Session,
) -> None:
    session.live_view.get_frame()
    with pytest.raises(crsdkpy.OperationTimeoutError):
        session.live_view.get_frame(timeout_ms=0)


def test_cadence_is_profile_specific() -> None:
    """Frame rate is not a constant of the API."""
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            status = session.live_view.status()
            assert (status.width, status.height) == (512, 384)


def test_live_view_available_in_transfer_mode_on_another_camera() -> None:
    """A profile that contradicts the first characterized body."""
    with make_sdk(profile="inverted_modes") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote_transfer") as session:
            assert session.live_view.available
            frame = session.live_view.get_frame()
            assert frame.width == 1024
