"""Postview, live view, video and device status through the host backend.

Runs against the pure-Python fake host, so no native build, no vendor SDK and
no camera are required. The behaviours modelled are the ones real hardware
showed: postview configuration and delivery disagreeing, a control mode that
answers the live-view query and still cannot produce a frame, a stream that
pauses around an exposure, and a movie-record control that is a toggle.
"""

from __future__ import annotations

import os
import sys

import pytest

import crsdkpy
from crsdkpy._jpeg import jpeg_dimensions, looks_complete
from crsdkpy.backend.host import HostBackend

FAKE_HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_host.py")

HOST_AND_CARD = crsdkpy.StillDestination.HOST_AND_MEMORY_CARD
MEMORY_CARD = crsdkpy.StillDestination.MEMORY_CARD
POSTVIEW = crsdkpy.PreviewKind.POSTVIEW


def make_backend(behaviour: str = "normal", **kwargs) -> HostBackend:
    return HostBackend(
        command=[sys.executable, FAKE_HOST, behaviour],
        adapter_dir=os.path.dirname(FAKE_HOST),
        **kwargs,
    )


def open_session(sdk, mode="remote", destination=None):
    camera = sdk.discover()[0]
    return camera.open(mode, destination=destination)


# -- device status ----------------------------------------------------------
def test_battery_is_readable_without_knowing_a_property_code() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            battery = session.battery
            assert battery.known
            assert battery.percent == 87
            assert battery.level == pytest.approx(0.75)
            assert not battery.usb_power


def test_storage_reports_slots_and_room() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            slots = session.storage
            assert len(slots) == 1        # the fake body has one slot
            first = slots[0]
            assert first.slot == 1
            assert first.status == "ok"
            assert first.present and first.writable
            assert first.remaining_shots == 1234


# -- destination ------------------------------------------------------------
def test_destination_round_trips() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert session.destination is MEMORY_CARD
            session.set_destination(HOST_AND_CARD)
            assert session.destination is HOST_AND_CARD
            session.set_destination(MEMORY_CARD)
            assert session.destination is MEMORY_CARD


def test_destination_requested_at_open_is_applied() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            assert session.destination is HOST_AND_CARD


def test_changing_destination_changes_what_the_session_can_do() -> None:
    """Postview delivery follows the destination, so capabilities move too."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert not session.capabilities.postview_delivery
            session.set_destination(HOST_AND_CARD)
            assert session.capabilities.postview_delivery


# -- postview ---------------------------------------------------------------
def test_postview_is_delivered_when_the_host_is_a_destination() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            capture = session.capture()
            assert capture.exposed
            preview = capture.preview(POSTVIEW, timeout_ms=2_000)
            assert preview.is_exact_still
            assert looks_complete(preview.data)
            assert jpeg_dimensions(preview.data) == (preview.width, preview.height)
            # Full resolution, unlike any content preview.
            assert preview.width == 4240
            assert preview.metadata["exact_still_association"] == (
                "announced_per_capture"
            )
            assert preview.metadata["filename"] == "DSC03400.JPG"


def test_postview_is_not_delivered_to_the_card_alone() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                session.capture().preview(POSTVIEW, timeout_ms=200)
            assert excinfo.value.capability == "postview_delivery"


def test_nothing_pending_is_not_an_error() -> None:
    """Polling before the camera announces anything must return nothing."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            assert sdk.backend.pull_postview(session._id) is None


def test_a_postview_that_never_arrives_times_out() -> None:
    with crsdkpy.SDK(backend=make_backend("no_postview")) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            capture = session.capture()
            with pytest.raises(crsdkpy.OperationTimeoutError):
                capture.preview(POSTVIEW, timeout_ms=300)


def test_an_announced_but_empty_postview_is_reported() -> None:
    with crsdkpy.SDK(backend=make_backend("empty_postview")) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            session.capture()
            with pytest.raises(crsdkpy.CrSDKPyError):
                sdk.backend.pull_postview(session._id)


def test_unreadable_postview_bytes_are_refused() -> None:
    with crsdkpy.SDK(backend=make_backend("torn_postview")) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            session.capture()
            with pytest.raises(crsdkpy.CameraConnectionError) as excinfo:
                sdk.backend.pull_postview(session._id)
            assert "truncated" in str(excinfo.value)


def test_configuration_and_delivery_are_independent() -> None:
    """The headline hardware finding, end to end.

    The camera refuses to be configured for postview and delivers one anyway.
    A client that inferred delivery from configuration would give up here.
    """
    with crsdkpy.SDK(backend=make_backend("postview_config_refused")) as sdk:
        with open_session(sdk, destination=HOST_AND_CARD) as session:
            with pytest.raises(crsdkpy.UnsupportedOperationError):
                session.configure_postview(enabled=True)

            caps = session.capabilities
            assert not caps.postview_configuration   # learned by being refused
            assert caps.postview_delivery            # and still delivered

            preview = session.capture().preview(POSTVIEW, timeout_ms=2_000)
            assert preview.byte_length > 0


def test_configuration_is_offered_before_it_is_known_to_fail() -> None:
    """Reporting False up front would hide the call that discovers the answer."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert session.capabilities.postview_configuration
            session.configure_postview(enabled=True)
            assert session.capabilities.postview_configuration


# -- live view --------------------------------------------------------------
def test_live_view_delivers_frames_with_varying_sizes() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            sizes = set()
            for frame in session.live_view.frames(limit=6):
                assert looks_complete(frame.data)
                assert jpeg_dimensions(frame.data) == (frame.width, frame.height)
                assert not frame.is_exact_still
                sizes.add(frame.byte_length)
            assert len(sizes) > 1, "a constant frame size is not realistic"


def test_frame_numbers_advance() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            numbers = [f.frame_number for f in session.live_view.frames(limit=5)]
            assert numbers == sorted(numbers)
            assert len(set(numbers)) == len(numbers)


def test_a_repeated_frame_is_reported_as_nothing_new() -> None:
    """Polling faster than the camera produces must not duplicate frames."""
    with crsdkpy.SDK(backend=make_backend("stuck_live_view")) as sdk:
        with open_session(sdk) as session:
            first = session.live_view.try_get_frame()
            assert first is not None
            # The camera keeps handing back the same frame; it is not new.
            assert session.live_view.try_get_frame() is None
            assert session.live_view.try_get_frame() is None


def test_live_view_pauses_around_an_exposure_and_resumes() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert session.live_view.try_get_frame() is not None
            session.capture()
            # The gap: the stream stops briefly.
            assert session.live_view.try_get_frame() is None
            # And comes back on its own, with nothing restarted.
            assert session.live_view.get_frame(timeout_ms=1_000) is not None


def test_live_view_status_separates_answering_from_working() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, "remote_transfer") as session:
            status = session.live_view.status()
            assert status.info_ok        # the camera answered
            assert not status.usable     # and still cannot deliver
            assert not status.available


def test_live_view_is_refused_where_it_cannot_work() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, "remote_transfer") as session:
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                session.live_view.get_frame(timeout_ms=100)
            assert excinfo.value.capability == "live_view"


def test_measurement_reports_what_the_transport_managed() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            stats = session.live_view.measure(duration_ms=400)
            assert stats.frames > 0
            assert stats.elapsed_ms > 0
            assert stats.fps > 0
            assert stats.min_bytes and stats.max_bytes
            assert stats.min_bytes < stats.max_bytes
            assert stats.throughput_mib_s > 0
            assert repr(stats).startswith("LiveViewStats(")


def test_measurement_counts_empty_polls_separately_from_frames() -> None:
    with crsdkpy.SDK(backend=make_backend("stuck_live_view")) as sdk:
        with open_session(sdk) as session:
            stats = session.live_view.measure(duration_ms=200)
            assert stats.frames == 1        # only the first is new
            assert stats.empty_polls > 0    # the rest were repeats


# -- video ------------------------------------------------------------------
def test_recording_starts_and_stops() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert session.capabilities.video
            assert session.video.state is crsdkpy.RecordingState.IDLE

            recording = session.video.start()
            assert recording.active
            assert session.video.recording
            assert session.video.state is crsdkpy.RecordingState.RECORDING

            recording.stop()
            assert session.video.state is crsdkpy.RecordingState.IDLE
            assert not recording.active


def test_a_second_start_does_not_stop_the_recording() -> None:
    """The vendor control is a toggle, so a blind second press would stop it."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            session.video.start()
            session.video.start()
            assert session.video.state is crsdkpy.RecordingState.RECORDING
            session.video.stop()


def test_a_second_stop_does_not_start_a_recording() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            session.video.start()
            session.video.stop()
            session.video.stop()
            assert session.video.state is crsdkpy.RecordingState.IDLE


def test_recording_is_usable_as_a_context_manager() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            with session.video.start() as recording:
                assert recording.state is crsdkpy.RecordingState.RECORDING
            assert session.video.state is crsdkpy.RecordingState.IDLE


def test_recording_state_changes_are_observable_as_events() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            session.drain_events(timeout_ms=100)
            session.video.start()
            events = session.drain_events(timeout_ms=200)
            assert any(
                isinstance(e, crsdkpy.PropertyChangedEvent) and 0x0705 in e
                for e in events
            )
            session.video.stop()


def test_host_death_during_recording_is_reported_not_hidden() -> None:
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        session = open_session(sdk)
        session.video.start()
        with pytest.raises(crsdkpy.BackendError):
            backend._provoke_host_exit()
        # The interpreter is fine and the failure names the process, not the
        # camera: the recording state is now simply unknown.
        with pytest.raises(crsdkpy.CrSDKPyError):
            session.video.state
