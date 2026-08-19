"""Generality guards.

If these pass only for the first characterized body, the architecture is not
generic enough. Each test here asserts behaviour that *contradicts* that body.
"""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import profile_names


def test_all_shipped_profiles_are_usable() -> None:
    for name in profile_names():
        with make_sdk(profile=name) as sdk:
            camera = sdk.discover()[0]
            mode = sorted(camera.capabilities.modes, key=lambda m: m.value)[0]
            with camera.open(mode) as session:
                assert session.state is crsdkpy.ConnectionState.CONNECTED
                assert len(session.properties.snapshot()) > 0
                assert session.capabilities.mode is mode


def test_inverted_profile_contradicts_the_fx3a_matrix() -> None:
    """Live view in transfer mode, content index in remote mode."""
    with make_sdk(profile="inverted_modes") as sdk:
        camera = sdk.discover()[0]

        with camera.open("remote") as remote:
            caps = remote.capabilities
            assert not caps.live_view
            assert caps.content_index
            assert caps.screennail

        with camera.open("remote_transfer") as transfer:
            caps = transfer.capabilities
            assert caps.live_view
            assert caps.content_index
            assert not caps.thumbnail


def test_inverted_profile_postview_ignores_destination() -> None:
    """Destination gates postview on one body and not on another."""
    with make_sdk(profile="inverted_modes") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            assert session.destination is crsdkpy.StillDestination.MEMORY_CARD
            assert session.capabilities.postview_delivery


def test_end_to_end_capture_on_a_contradicting_profile() -> None:
    with make_sdk(profile="inverted_modes") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            capture = session.autofocus_and_capture()
            assert capture.exposed
            content = capture.wait_for_content()
            preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
            assert preview.content_id == content.content_id


def test_camera_with_only_one_mode() -> None:
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        assert camera.capabilities.modes == {crsdkpy.SessionMode.REMOTE}
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            camera.open("remote_transfer")


def test_future_camera_remains_fully_usable() -> None:
    """Unknown property codes, unknown capabilities, unknown events."""
    with make_sdk(profile="future_unknown", scenario="unknown_events") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            snapshot = session.properties.snapshot()
            assert snapshot.unknown_codes()
            capture = session.autofocus_and_capture()
            assert capture.exposed
            events = session.drain_events(timeout_ms=500)
            unknown = [e for e in events if isinstance(e, crsdkpy.UnknownEvent)]
            assert unknown
            assert unknown[0].code == 0xDEAD


def test_timings_differ_between_profiles() -> None:
    """Latencies belong to the profile, never to the API contract."""
    with make_sdk(profile="fx3a") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            fx3a_focus = session.autofocus().elapsed_ms

    with make_sdk(profile="inverted_modes") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            other_focus = session.autofocus().elapsed_ms

    assert fx3a_focus != other_focus


def test_multiple_distinct_cameras_in_one_sdk() -> None:
    with make_sdk(cameras=["fx3a", "minimal_still", "inverted_modes"]) as sdk:
        cameras = sdk.discover()
        assert len(cameras) == 3
        models = {c.model for c in cameras}
        assert len(models) == 3
        # Feature present on one camera, absent on another.
        by_model = {c.model: c for c in cameras}
        assert by_model["ILME-FX3A"].capabilities.video
        assert not by_model["SIM-MinimalStill"].capabilities.video
        # Their keys are distinct and stable.
        assert len({c.device_key for c in cameras}) == 3
