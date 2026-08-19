"""Capability discovery.

The point of these tests is that capabilities come from the session, not from
a model name, and that they vary with both control mode and destination.
"""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk


def test_camera_level_capabilities(camera: crsdkpy.Camera) -> None:
    caps = camera.capabilities
    assert caps.still_capture
    assert caps.autofocus_s1
    assert caps.video
    assert caps.live_view_any_mode
    assert caps.content_index_any_mode
    assert caps.supports_mode(crsdkpy.SessionMode.REMOTE)
    assert not caps.supports_mode(crsdkpy.SessionMode.CONTENTS_TRANSFER)


def test_session_capabilities_differ_by_mode(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as remote:
        remote_caps = remote.capabilities
    with camera.open("remote_transfer") as transfer:
        transfer_caps = transfer.capabilities

    # Live view and the content index live in different modes on this body.
    assert remote_caps.live_view
    assert not remote_caps.content_index
    assert not transfer_caps.live_view
    assert transfer_caps.content_index
    assert transfer_caps.screennail
    assert not remote_caps.screennail


def test_postview_configuration_and_delivery_are_separate(
    camera: crsdkpy.Camera,
) -> None:
    """Being allowed to configure postview does not mean it delivers.

    Hardware accepted configuration without delivering, and delivered without
    accepting configuration. One boolean cannot express that.
    """
    with camera.open("remote") as remote:
        assert not remote.capabilities.postview_configuration
        assert not remote.capabilities.postview_delivery
        remote.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
        # Configuration still rejected, yet delivery now works.
        assert not remote.capabilities.postview_configuration
        assert remote.capabilities.postview_delivery

    with camera.open("remote_transfer") as transfer:
        assert transfer.capabilities.postview_configuration
        assert not transfer.capabilities.postview_delivery
        transfer.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
        assert transfer.capabilities.postview_configuration
        assert transfer.capabilities.postview_delivery


def test_capabilities_recomputed_after_destination_change(
    camera: crsdkpy.Camera,
) -> None:
    with camera.open("remote_transfer") as session:
        before = session.capabilities
        session.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
        after = session.capabilities
        assert before.postview_delivery is False
        assert after.postview_delivery is True
        assert before is not after


def test_unrecognised_capability_names_are_preserved() -> None:
    """A backend may describe a feature this release has never heard of."""
    with make_sdk(profile="future_unknown") as sdk:
        camera = sdk.discover()[0]
        assert camera.capabilities.get("quantum_stabilizer") is True
        assert "quantum_stabilizer" in camera.capabilities
        with camera.open("remote") as session:
            caps = session.capabilities
            assert caps.get("holographic_viewfinder") is True
            assert caps.get("neural_subject_lock") is True
            assert "holographic_viewfinder" in caps
            # An unknown name that was not advertised is simply False.
            assert caps.get("time_travel") is False


def test_missing_lists_absent_capabilities(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        missing = set(session.capabilities.missing())
        assert "content_index" in missing
        assert "screennail" in missing
        assert "live_view" not in missing


def test_minimal_camera_reports_absent_features() -> None:
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        assert not camera.capabilities.autofocus_s1
        assert not camera.capabilities.video
        assert not camera.capabilities.content_index_any_mode
        assert not camera.capabilities.supports_mode(
            crsdkpy.SessionMode.REMOTE_TRANSFER
        )
        with camera.open("remote") as session:
            caps = session.capabilities
            assert caps.still_capture
            assert caps.live_view
            assert not caps.autofocus_s1
            assert not caps.video


def test_unsupported_operations_name_the_capability(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
            session.live_view  # available here
            session._backend.list_content(session._id)
        assert excinfo.value.capability == "content_index"


# -- device status parity ---------------------------------------------------
def test_simulated_battery_is_readable(session: crsdkpy.Session) -> None:
    battery = session.battery
    assert battery.known
    assert 0 <= battery.percent <= 100
    assert repr(battery).startswith("BatteryStatus(")


def test_battery_moves_as_the_camera_is_used(session: crsdkpy.Session) -> None:
    """A constant reading would let a drain bug go unnoticed."""
    before = session.battery.percent
    session.autofocus_and_capture()
    assert session.battery.percent < before


def test_simulated_storage_reports_slots(session: crsdkpy.Session) -> None:
    slots = session.storage
    assert slots
    first = slots[0]
    assert first.slot == 1
    assert first.present and first.writable
    assert first.remaining_shots is not None
    assert repr(first).startswith("StorageSlot(")


def test_an_empty_slot_is_present_but_not_writable() -> None:
    from crsdkpy.status import StorageSlot

    empty = StorageSlot(slot=2, status="no_card")
    assert not empty.present
    assert not empty.writable

    faulty = StorageSlot(slot=1, status="card_error")
    assert faulty.present          # there is a card
    assert not faulty.writable     # but it cannot be used
