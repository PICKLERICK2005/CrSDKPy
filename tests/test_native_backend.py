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
    assert version & 0xFFFF == _cabi.ABI_VERSION_MINOR


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


# -- ABI version negotiation ------------------------------------------------
#
# These need no built bridge: they stand a stub in for one so that the loader's
# own compatibility rules can be exercised in both directions, including the
# direction a real older library would fail in.

class _StubFunction:
    """A ctypes-style function object: settable restype/argtypes, callable."""

    def __init__(self, result: int = 0) -> None:
        self.restype = None
        self.argtypes = None
        self._result = result

    def __call__(self, *args: object) -> int:
        return self._result


class _StubBridge:
    """A stand-in library exporting a chosen set of symbols.

    A real bridge older than this build is missing symbols entirely, and
    ctypes answers an absent symbol with AttributeError -- which is exactly
    what the loader must not surface. ``exports`` defaults to the one symbol
    every bridge has ever had, so the default stub *is* the worst case.
    """

    def __init__(self, major: int, minor: int, *, complete: bool = False) -> None:
        self._version = ((major & 0xFFFF) << 16) | (minor & 0xFFFF)
        # A complete stub answers to any bridge symbol, standing in for a
        # library at least as new as this build. Deriving it this way rather
        # than listing names keeps the test from rotting as the ABI grows.
        self._complete = complete
        self._cache: dict = {}

    def __getattr__(self, name: str) -> _StubFunction:
        if name.startswith("_"):
            raise AttributeError(name)
        if not self._complete and name != "crsdkpy_abi_version":
            raise AttributeError(f"function {name!r} not found")
        if name not in self._cache:
            result = self._version if name == "crsdkpy_abi_version" else 0
            self._cache[name] = _StubFunction(result)
        return self._cache[name]


def _load_stub(monkeypatch, stub: _StubBridge):
    monkeypatch.setattr(_cabi, "find_library", lambda explicit=None: "/stub/bridge")
    monkeypatch.setattr(_cabi.ctypes, "CDLL", lambda path: stub)
    return _cabi.load_bridge()


def test_an_older_abi_minor_is_refused_with_an_actionable_message(monkeypatch) -> None:
    """The invariant: an older bridge is rejected at load, not at first use."""
    stub = _StubBridge(_cabi.ABI_VERSION_MAJOR, _cabi.ABI_VERSION_MINOR - 1)
    with pytest.raises(crsdkpy.NativeBackendError) as excinfo:
        _load_stub(monkeypatch, stub)
    message = str(excinfo.value)
    assert "older" in message
    assert "Rebuild the bridge" in message
    # The version is what it complains about, not a C symbol name.
    assert "crsdkpy_" not in message


def test_the_version_is_checked_before_any_symbol_is_declared(monkeypatch) -> None:
    """Declaring a missing symbol raises AttributeError naming a C function.

    That says nothing about what is wrong, so the version check has to come
    first. The stub exports only the version query, which is what an older
    bridge effectively is from this build's point of view.
    """
    stub = _StubBridge(_cabi.ABI_VERSION_MAJOR, _cabi.ABI_VERSION_MINOR - 1)
    with pytest.raises(crsdkpy.NativeBackendError):
        _load_stub(monkeypatch, stub)


def test_an_incompatible_abi_major_is_refused(monkeypatch) -> None:
    stub = _StubBridge(_cabi.ABI_VERSION_MAJOR + 1, 0)
    with pytest.raises(crsdkpy.NativeBackendError, match="ABI mismatch"):
        _load_stub(monkeypatch, stub)


def test_a_newer_abi_minor_loads(monkeypatch) -> None:
    """The minor only grows by addition, so a newer bridge still exports
    everything this build calls and must not be refused."""
    stub = _StubBridge(
        _cabi.ABI_VERSION_MAJOR, _cabi.ABI_VERSION_MINOR + 3, complete=True
    )
    assert _load_stub(monkeypatch, stub) is stub


def test_a_matching_bridge_loads(monkeypatch) -> None:
    stub = _StubBridge(
        _cabi.ABI_VERSION_MAJOR, _cabi.ABI_VERSION_MINOR, complete=True
    )
    assert _load_stub(monkeypatch, stub) is stub
