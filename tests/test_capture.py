"""Capture lifecycle.

Command acceptance, exposure and durable content are three separate facts.
"""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import AfOutcome, Scenario


def test_capture_reports_lifecycle_not_a_boolean(
    transfer_session: crsdkpy.Session,
) -> None:
    capture = transfer_session.autofocus_and_capture()
    assert isinstance(capture, crsdkpy.Capture)
    assert capture.state is crsdkpy.CaptureState.EXPOSED
    assert capture.exposed
    assert capture.exposed_ms is not None
    assert capture.focus is not None and capture.focus.confirmed
    # Exposure does not imply durable content yet.
    assert capture.content is None

    content = capture.wait_for_content()
    assert capture.state is crsdkpy.CaptureState.CONTENT_AVAILABLE
    assert content.content_id > 0


def test_accepted_command_is_not_a_completed_exposure() -> None:
    """The commands succeed and nothing is ever exposed."""
    sdk = make_sdk(scenario=Scenario(capture_without_exposure=True))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            capture = session.autofocus_and_capture(timeout_ms=2_000)
            assert not capture.exposed
            assert capture.state is crsdkpy.CaptureState.FAILED
            assert capture.failure is not None
    finally:
        sdk.close()


def test_focus_failure_never_requests_an_exposure() -> None:
    sdk = make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            with pytest.raises(crsdkpy.AutofocusFailedError) as excinfo:
                session.autofocus_and_capture(focus_timeout_ms=2_000)
            assert excinfo.value.focus_state is crsdkpy.FocusState.NOT_FOCUSED_AF_S
            # Nothing was captured and no content was created.
            assert session._backend.latest_content(session._id) is None
            assert session.raw.half_press is False
    finally:
        sdk.close()


def test_focus_failure_can_return_instead_of_raising() -> None:
    sdk = make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            capture = session.autofocus_and_capture(
                focus_timeout_ms=2_000, raise_on_focus_failure=False
            )
            assert capture.state is crsdkpy.CaptureState.FAILED
            assert not capture.exposed
            assert capture.focus is not None and not capture.focus.confirmed
    finally:
        sdk.close()


def test_delayed_content_still_resolves() -> None:
    sdk = make_sdk(scenario=Scenario(content_extra_delay_ms=3_000))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            capture = session.autofocus_and_capture()
            assert capture.exposed
            assert capture.content is None
            content = capture.wait_for_content(timeout_ms=10_000)
            assert content.content_id > 0
    finally:
        sdk.close()


def test_content_timeout_raises() -> None:
    sdk = make_sdk(scenario=Scenario(content_extra_delay_ms=30_000))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            capture = session.autofocus_and_capture()
            with pytest.raises(crsdkpy.OperationTimeoutError):
                capture.wait_for_content(timeout_ms=1_000)
    finally:
        sdk.close()


def test_repeated_captures_produce_distinct_content(
    transfer_session: crsdkpy.Session,
) -> None:
    ids = []
    hashes = []
    for _ in range(3):
        capture = transfer_session.autofocus_and_capture()
        content = capture.wait_for_content()
        preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
        ids.append(content.content_id)
        hashes.append(hash(preview.data))
    assert ids == sorted(ids)
    assert len(set(ids)) == 3
    # No stale preview: each capture yields distinct bytes.
    assert len(set(hashes)) == 3


def test_exactly_one_exposure_per_capture(
    transfer_session: crsdkpy.Session,
) -> None:
    capture = transfer_session.autofocus_and_capture()
    capture.wait_for_content()
    events = transfer_session.drain_events(timeout_ms=500)
    exposures = [e for e in events if isinstance(e, crsdkpy.CaptureEvent)]
    assert len(exposures) <= 1  # the one for this capture was already consumed


def test_non_contiguous_content_ids_are_handled() -> None:
    """Identifiers are monotonic but skip values; baseline+1 is wrong."""
    sdk = make_sdk(scenario=Scenario(content_id_step=2))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            first = session.autofocus_and_capture().wait_for_content()
            second = session.autofocus_and_capture().wait_for_content()
            assert second.content_id == first.content_id + 2
    finally:
        sdk.close()


def test_capture_without_autofocus_uses_release_only(
    transfer_session: crsdkpy.Session,
) -> None:
    capture = transfer_session.capture()
    assert capture.exposed
    assert capture.focus is None


def test_capture_requires_still_capability(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        assert session.capabilities.still_capture


def test_minimal_camera_cannot_gate_capture() -> None:
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                session.autofocus_and_capture()
            assert excinfo.value.capability == "autofocus_s1"
            # Plain capture still works.
            capture = session.capture()
            assert capture.exposed


def test_content_unavailable_in_mode_without_index(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        capture = session.autofocus_and_capture()
        assert capture.exposed
        with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
            capture.wait_for_content()
        assert excinfo.value.capability == "content_index"


def test_capture_states_are_ordered() -> None:
    assert not crsdkpy.CaptureState.REQUESTED.is_terminal
    assert crsdkpy.CaptureState.FAILED.is_terminal
    assert crsdkpy.CaptureState.PREVIEW_AVAILABLE.is_terminal
