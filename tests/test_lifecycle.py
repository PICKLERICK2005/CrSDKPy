"""SDK, Camera and Session lifecycle."""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk


def test_import_and_version() -> None:
    """Shape, not a literal: a pinned string is edited, never checked."""
    import re

    assert re.fullmatch(r"\d+\.\d+\.\d+([ab]\d+|rc\d+)?", crsdkpy.__version__)
    # The packaging metadata reads the version from the module, so the two
    # cannot drift; this only guards the module attribute itself.
    assert crsdkpy.__version__ == crsdkpy.__dict__["__version__"]


def test_sdk_start_and_close_is_idempotent() -> None:
    sdk = make_sdk(autostart=False)
    assert not sdk.started
    sdk.start()
    sdk.start()
    assert sdk.started
    sdk.close()
    sdk.close()
    assert not sdk.started


def test_discover_before_start_raises() -> None:
    sdk = make_sdk(autostart=False)
    with pytest.raises(crsdkpy.SDKNotStartedError):
        sdk.discover()


def test_discovery_returns_stable_identity(sdk: crsdkpy.SDK) -> None:
    first = sdk.discover()
    second = sdk.discover()
    assert [c.device_key for c in first] == [c.device_key for c in second]
    # Rediscovery must not invalidate an existing Camera object.
    assert first[0] is second[0]


def test_camera_identity_is_not_a_list_index(sdk: crsdkpy.SDK) -> None:
    camera = sdk.discover()[0]
    assert camera.device_key
    assert sdk.camera_by_key(camera.device_key) is camera


def test_camera_info_fields(camera: crsdkpy.Camera) -> None:
    info = camera.info
    assert info.model == "ILME-FX3A"
    assert info.firmware == "2.02"
    assert info.transport == "usb"
    assert info.usb_pid == 0x0F52


def test_camera_survives_session_close_and_reopen(camera: crsdkpy.Camera) -> None:
    """A Camera is not one native connection; it outlives sessions."""
    with camera.open(crsdkpy.SessionMode.REMOTE) as first:
        assert first.mode is crsdkpy.SessionMode.REMOTE
        key = camera.device_key
    assert camera.sessions == []
    with camera.open(crsdkpy.SessionMode.REMOTE_TRANSFER) as second:
        assert second.mode is crsdkpy.SessionMode.REMOTE_TRANSFER
        assert second.camera is camera
        assert camera.device_key == key


def test_session_context_manager_closes(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        assert not session.closed
    assert session.closed
    assert session.state is crsdkpy.ConnectionState.CLOSED


def test_session_close_is_idempotent(camera: crsdkpy.Camera) -> None:
    session = camera.open("remote")
    session.close()
    session.close()
    assert session.closed


def test_operations_after_close_raise(camera: crsdkpy.Camera) -> None:
    session = camera.open("remote")
    session.close()
    with pytest.raises(crsdkpy.SessionClosedError):
        session.properties.snapshot()
    with pytest.raises(crsdkpy.SessionClosedError):
        session.capabilities


def test_mode_accepts_string_or_enum(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as a:
        assert a.mode is crsdkpy.SessionMode.REMOTE
    with camera.open(crsdkpy.SessionMode.REMOTE) as b:
        assert b.mode is crsdkpy.SessionMode.REMOTE


def test_unknown_mode_string_raises(camera: crsdkpy.Camera) -> None:
    with pytest.raises(ValueError, match="unknown control mode"):
        camera.open("teleportation")


def test_unsupported_mode_raises_capability_error(camera: crsdkpy.Camera) -> None:
    with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
        camera.open(crsdkpy.SessionMode.CONTENTS_TRANSFER)
    assert excinfo.value.capability == "mode.contents_transfer"


def test_sdk_close_closes_open_sessions() -> None:
    sdk = make_sdk()
    camera = sdk.discover()[0]
    session = camera.open("remote")
    assert not session.closed
    sdk.close()
    assert session.closed


def test_session_reports_connected_state(session: crsdkpy.Session) -> None:
    assert session.state is crsdkpy.ConnectionState.CONNECTED
    assert session.state.is_usable


def test_destination_defaults_and_changes(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        assert session.destination is crsdkpy.StillDestination.MEMORY_CARD
        session.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
        assert session.destination is crsdkpy.StillDestination.HOST_AND_MEMORY_CARD


def test_unsupported_destination_rejected(camera: crsdkpy.Camera) -> None:
    with pytest.raises(crsdkpy.UnsupportedOperationError):
        camera.open("remote", destination=crsdkpy.StillDestination.HOST)


def test_native_backend_selection_is_explicit() -> None:
    """Selecting the native backend must never silently fall back.

    Either a built bridge loads, or the failure explains how to build one.
    Both outcomes are valid depending on the machine; a simulator in disguise
    is not.
    """
    try:
        sdk = crsdkpy.SDK(backend="native")
    except crsdkpy.BackendUnavailableError as exc:
        message = str(exc)
        assert "cmake" in message.lower() or "not implemented" in message
        assert "not distributed" in message
        return
    try:
        assert sdk.backend_name == "native"
        assert not sdk.clock.is_virtual
    finally:
        sdk.close()


def test_unknown_backend_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        crsdkpy.SDK(backend="nikon")
