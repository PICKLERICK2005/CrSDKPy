"""Transport loss and recovery.

Hardware recovered without ever reporting a disconnect, and emitted a second
connected event. Nothing may require a disconnect before accepting recovery.
"""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import Scenario


def _reconnecting_session(**kwargs: object):
    scenario = Scenario(reconnect_after_ms=500, **kwargs)  # type: ignore[arg-type]
    sdk = make_sdk(scenario=scenario)
    camera = sdk.discover()[0]
    return sdk, camera, camera.open("remote")


def test_recovery_without_a_disconnect_event() -> None:
    sdk, _camera, session = _reconnecting_session()
    try:
        events = session.drain_events(timeout_ms=1_000)
        states = [e.state for e in events if isinstance(e, crsdkpy.ConnectionEvent)]
        assert crsdkpy.ConnectionState.RECONNECTING in states
        assert crsdkpy.ConnectionState.CLOSED not in states
        assert session.state is crsdkpy.ConnectionState.RECONNECTING
    finally:
        sdk.close()


def test_operations_fail_while_transport_is_absent() -> None:
    sdk, _camera, session = _reconnecting_session()
    try:
        session.drain_events(timeout_ms=1_000)
        assert session.state is crsdkpy.ConnectionState.RECONNECTING
        with pytest.raises(crsdkpy.CameraConnectionError):
            session.properties.snapshot()
    finally:
        sdk.close()


def test_automatic_recovery_restores_usability() -> None:
    sdk, _camera, session = _reconnecting_session()
    try:
        # Advance past the (deliberately long) recovery interval.
        assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
        assert len(session.properties.snapshot()) > 0
    finally:
        sdk.close()


def test_second_connected_event_is_emitted() -> None:
    sdk, _camera, session = _reconnecting_session()
    try:
        assert session.wait_for_state(crsdkpy.ConnectionState.RECONNECTING)
        assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
        events = session.drain_events()
        connected = [
            e
            for e in events
            if isinstance(e, crsdkpy.ConnectionEvent)
            and e.state is crsdkpy.ConnectionState.CONNECTED
        ]
        assert len(connected) >= 2
        assert any(e.recovered for e in connected)
    finally:
        sdk.close()


def test_camera_identity_persists_across_recovery() -> None:
    sdk, camera, session = _reconnecting_session()
    try:
        key_before = camera.device_key
        assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
        assert camera.device_key == key_before
        assert session.camera is camera
    finally:
        sdk.close()


def test_live_view_resumes_after_recovery() -> None:
    sdk, _camera, session = _reconnecting_session()
    try:
        assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
        frame = session.live_view.get_frame(timeout_ms=2_000)
        assert frame.byte_length > 0
    finally:
        sdk.close()


def test_disconnect_event_variant_is_also_supported() -> None:
    """Some transports may report a disconnect; both shapes must work."""
    sdk, _camera, session = _reconnecting_session(reconnect_without_disconnect=False)
    try:
        events = session.drain_events(timeout_ms=1_000)
        states = [e.state for e in events if isinstance(e, crsdkpy.ConnectionEvent)]
        assert crsdkpy.ConnectionState.CLOSED in states
        assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
    finally:
        sdk.close()


def test_manual_reconnect_trigger() -> None:
    with make_sdk() as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            sdk.backend.simulate_reconnect(session._id)
            assert session.wait_for_state(crsdkpy.ConnectionState.RECONNECTING)
            assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
