"""Movie recording."""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk


def test_recording_start_and_stop(session: crsdkpy.Session) -> None:
    assert session.video.available
    assert session.video.state is crsdkpy.RecordingState.IDLE

    recording = session.video.start()
    assert session.video.recording
    assert recording.active
    assert recording.state is crsdkpy.RecordingState.RECORDING

    recording.stop()
    assert session.video.state is crsdkpy.RecordingState.IDLE
    assert not recording.active


def test_recording_context_manager_stops(session: crsdkpy.Session) -> None:
    with session.video.start() as recording:
        assert recording.active
    assert session.video.state is crsdkpy.RecordingState.IDLE


def test_recording_emits_state_events(session: crsdkpy.Session) -> None:
    session.video.start()
    events = session.drain_events()
    states = [e.state for e in events if isinstance(e, crsdkpy.RecordingEvent)]
    assert crsdkpy.RecordingState.STARTING in states
    assert crsdkpy.RecordingState.RECORDING in states
    session.video.stop()


def test_stop_when_idle_is_harmless(session: crsdkpy.Session) -> None:
    session.video.stop()
    assert session.video.state is crsdkpy.RecordingState.IDLE


def test_double_start_is_harmless(session: crsdkpy.Session) -> None:
    session.video.start()
    session.video.start()
    assert session.video.recording
    session.video.stop()


def test_recording_state_property_reflects_camera(
    session: crsdkpy.Session,
) -> None:
    from crsdkpy.simulator import profiles as P

    session.video.start()
    assert session.properties.get(P.CODE_RECORDING_STATE).value == 0x0001
    session.video.stop()
    assert session.properties.get(P.CODE_RECORDING_STATE).value == 0x0000


def test_camera_without_video_reports_unavailable() -> None:
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        assert not camera.capabilities.video
        with camera.open("remote") as session:
            assert not session.video.available
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                session.video.start()
            assert excinfo.value.capability == "video"


def test_camera_without_video_still_works_normally() -> None:
    """Absent video must not degrade anything else."""
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            assert session.live_view.get_frame().byte_length > 0
            assert session.capture().exposed
            assert len(session.properties.snapshot()) > 0
