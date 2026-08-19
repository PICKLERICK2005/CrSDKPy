"""Raw escape hatch, error hierarchy, event stream and the clock."""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import Scenario
from crsdkpy.simulator import profiles as P


# -- raw access -----------------------------------------------------------
def test_raw_reads_unknown_property_code(session: crsdkpy.Session) -> None:
    prop = session.raw.get_property(0x0581)
    assert prop.code == 0x0581
    assert prop.name is None


def test_raw_lists_property_codes(session: crsdkpy.Session) -> None:
    codes = session.raw.property_codes()
    assert crsdkpy.PropertyCode(0x0581) in codes


def test_raw_set_property(session: crsdkpy.Session) -> None:
    session.raw.set_property(P.CODE_ISO, 3200)
    assert session.raw.get_property(P.CODE_ISO).value == 3200


def test_raw_send_command_accepts_unknown_integers(
    session: crsdkpy.Session,
) -> None:
    """A command CrSDKPy has never heard of must still be sendable."""
    session.raw.send_command(0xD2FF, crsdkpy.CommandParameter.DOWN)
    session.raw.send_command(0xD2FF, crsdkpy.CommandParameter.UP)


def test_raw_press_sends_down_then_up(session: crsdkpy.Session) -> None:
    session.raw.press(crsdkpy.Command.RELEASE)
    assert session.wait_for_event(crsdkpy.CaptureEvent) is not None


def test_raw_s1_and_release_is_ungated(camera: crsdkpy.Camera) -> None:
    """The vendor's combined command exposes no focus gate.

    It is deliberately only reachable through the raw layer, never behind a
    name that implies focus was checked.
    """
    with camera.open("remote_transfer") as session:
        session.raw.s1_and_release()
        assert session.wait_for_event(crsdkpy.CaptureEvent) is not None


def test_s1_and_release_fires_even_when_focus_fails() -> None:
    """Proves why it is not the default capture path."""
    from crsdkpy.simulator import AfOutcome

    sdk = make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            # An exposure happens despite autofocus never confirming.
            session.raw.s1_and_release()
            assert session.wait_for_event(crsdkpy.CaptureEvent) is not None
    finally:
        sdk.close()


def test_raw_call_extension_point(session: crsdkpy.Session) -> None:
    echoed = session.raw.call("echo", value=42)
    assert echoed == {"value": 42}
    lens = session.raw.call("lens_information")
    assert isinstance(lens, list) and lens[0]["model"] == "ILME-FX3A"


def test_raw_call_unknown_operation_raises(session: crsdkpy.Session) -> None:
    with pytest.raises(crsdkpy.UnsupportedOperationError):
        session.raw.call("teleport")


def test_raw_respects_session_lifetime(camera: crsdkpy.Camera) -> None:
    session = camera.open("remote")
    session.close()
    with pytest.raises(crsdkpy.SessionClosedError):
        session.raw.get_property(P.CODE_ISO)


# -- errors ---------------------------------------------------------------
def test_error_hierarchy() -> None:
    assert issubclass(crsdkpy.SessionClosedError, crsdkpy.CrSDKPyError)
    assert issubclass(crsdkpy.NativeBackendError, crsdkpy.BackendUnavailableError)
    assert issubclass(crsdkpy.SDKNotFoundError, crsdkpy.BackendUnavailableError)
    assert issubclass(crsdkpy.BackendUnavailableError, crsdkpy.CrSDKPyError)
    assert issubclass(crsdkpy.AutofocusFailedError, crsdkpy.CrSDKPyError)


def test_error_preserves_context() -> None:
    error = crsdkpy.CrSDKPyError(
        "something failed", operation="capture", backend_code=0x8402
    )
    text = str(error)
    assert "something failed" in text
    assert "capture" in text
    assert "0x8402" in text


def test_unsupported_operation_names_capability(
    transfer_session: crsdkpy.Session,
) -> None:
    with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
        transfer_session.live_view.get_frame()
    assert excinfo.value.capability == "live_view"
    assert excinfo.value.operation


def test_busy_scenario_raises_busy_error() -> None:
    sdk = make_sdk(scenario=Scenario(busy=True))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote") as session:
            with pytest.raises(crsdkpy.CameraBusyError):
                session.properties.snapshot()
    finally:
        sdk.close()


# -- events ---------------------------------------------------------------
def test_event_stream_and_internal_ops_do_not_starve_each_other(
    transfer_session: crsdkpy.Session,
) -> None:
    """An internal wait must not consume the caller's event stream."""
    capture = transfer_session.autofocus_and_capture()
    assert capture.exposed
    events = transfer_session.drain_events()
    assert any(isinstance(e, crsdkpy.CaptureEvent) for e in events)
    assert any(isinstance(e, crsdkpy.FocusEvent) for e in events)


def test_events_generator_stops_when_idle(session: crsdkpy.Session) -> None:
    session.drain_events()
    assert list(session.events(timeout_ms=0)) == []


def test_events_generator_respects_limit(session: crsdkpy.Session) -> None:
    session.autofocus()
    collected = list(session.events(timeout_ms=500, limit=2))
    assert len(collected) <= 2


def test_connection_events_carry_state(session: crsdkpy.Session) -> None:
    events = session.drain_events()
    connection = [e for e in events if isinstance(e, crsdkpy.ConnectionEvent)]
    assert connection
    assert connection[-1].state is crsdkpy.ConnectionState.CONNECTED


def test_property_changed_event_membership(session: crsdkpy.Session) -> None:
    session.properties.set(P.CODE_ISO, 200)
    events = session.drain_events()
    batches = [e for e in events if isinstance(e, crsdkpy.PropertyChangedEvent)]
    assert any(P.CODE_ISO in batch for batch in batches)


# -- clock ----------------------------------------------------------------
def test_virtual_clock_is_deterministic() -> None:
    clock = crsdkpy.VirtualClock()
    assert clock.now_ms() == 0
    clock.sleep_ms(28_000)
    assert clock.now_ms() == 28_000
    assert clock.is_virtual


def test_virtual_clock_notifies_listeners() -> None:
    clock = crsdkpy.VirtualClock()
    seen = []
    clock.add_listener(seen.append)
    clock.advance(100)
    assert seen == [100]
    clock.remove_listener(seen.append)


def test_real_clock_moves_forward() -> None:
    clock = crsdkpy.RealClock()
    assert clock.now_ms() >= 0
    assert not clock.is_virtual


def test_simulator_defaults_to_virtual_clock(sdk: crsdkpy.SDK) -> None:
    assert sdk.clock.is_virtual


def test_long_latency_costs_no_wall_clock_time() -> None:
    """A 28 second reconnect resolves instantly under the virtual clock."""
    import time

    started = time.monotonic()
    sdk = make_sdk(scenario=Scenario(reconnect_after_ms=100))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote") as session:
            # The session starts connected, so wait for the loss first.
            assert session.wait_for_state(crsdkpy.ConnectionState.RECONNECTING)
            assert session.wait_for_state(crsdkpy.ConnectionState.CONNECTED)
            assert sdk.clock.now_ms() > 28_000
    finally:
        sdk.close()
    assert time.monotonic() - started < 5.0
