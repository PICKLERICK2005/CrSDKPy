"""Native backend behaviour that does not require a camera.

These run everywhere. When no bridge is built they assert the failure is clear
and actionable; when one is present they additionally exercise the loader, the
ABI handshake and the lifecycle guards. Hardware-only behaviour is not covered
here.
"""

from __future__ import annotations

import pytest

import crsdkpy
from crsdkpy.backend import _cabi
from crsdkpy.backend.native import NativeBackend, native_backend_available

bridge_path = _cabi.find_library()
requires_bridge = pytest.mark.skipif(
    bridge_path is None, reason="native bridge is not built"
)


def test_availability_probe_never_raises() -> None:
    assert isinstance(native_backend_available(), bool)


def test_missing_bridge_reports_actionable_error() -> None:
    """A wrong path must explain how to build, not just fail to import."""
    with pytest.raises(crsdkpy.SDKNotFoundError) as excinfo:
        _cabi.load_bridge("/definitely/not/a/real/bridge.dll")
    message = str(excinfo.value)
    assert "cmake" in message.lower()
    assert "CRSDKPY_BRIDGE" in message
    assert "not distributed" in message


def test_sdk_reports_clearly_when_bridge_absent() -> None:
    if bridge_path is not None:
        pytest.skip("bridge is built, so this path cannot be exercised")
    with pytest.raises(crsdkpy.BackendUnavailableError):
        crsdkpy.SDK(backend="native")


def test_error_types_are_catchable_as_one_family() -> None:
    assert issubclass(crsdkpy.SDKNotFoundError, crsdkpy.BackendUnavailableError)
    assert issubclass(crsdkpy.NativeBackendError, crsdkpy.BackendUnavailableError)


@requires_bridge
def test_bridge_loads_and_abi_matches() -> None:
    lib = _cabi.load_bridge()
    version = lib.crsdkpy_abi_version()
    assert (version >> 16) & 0xFFFF == _cabi.ABI_VERSION_MAJOR


@requires_bridge
def test_backend_uses_a_real_clock() -> None:
    backend = NativeBackend()
    assert backend.name == "native"
    assert not backend.clock.is_virtual


@requires_bridge
def test_operations_before_start_are_refused() -> None:
    backend = NativeBackend()
    with pytest.raises(crsdkpy.NativeBackendError):
        backend.enumerate_cameras()


@requires_bridge
def test_unknown_session_is_rejected_not_aliased() -> None:
    backend = NativeBackend()
    backend.start()
    try:
        with pytest.raises(crsdkpy.SessionClosedError):
            backend.poll_events("native-session-does-not-exist")
        # Closing something unknown is a no-op, not an error.
        backend.close_session("native-session-does-not-exist")
        assert (
            backend.connection_state("native-session-does-not-exist")
            is crsdkpy.ConnectionState.CLOSED
        )
    finally:
        backend.shutdown()


@requires_bridge
def test_start_and_shutdown_are_idempotent() -> None:
    backend = NativeBackend()
    backend.start()
    backend.start()
    backend.shutdown()
    backend.shutdown()


@requires_bridge
def test_operations_on_an_unknown_session_are_refused() -> None:
    """Every feature refuses an unknown session rather than half-working."""
    backend = NativeBackend()
    for call in (
        lambda: backend.get_live_view_frame("s"),
        lambda: backend.recording_state("s"),
        lambda: backend.latest_content("s"),
        lambda: backend.pull_postview("s"),
        lambda: backend.battery("s"),
    ):
        with pytest.raises(crsdkpy.CrSDKPyError):
            call()
