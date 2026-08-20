"""Who recovers a dropped link, and what that costs when opening a session.

The vendor can watch the transport and re-establish it itself. That monitor
keeps trying for five minutes before declaring the session disconnected, which
is what a long-lived session wants and exactly wrong for opening one: the same
five minutes becomes the worst case for a single call, and nothing above the
vendor can shorten it. It was previously always on, and no caller could see that
or choose otherwise.
"""

from __future__ import annotations

import os
import sys

import pytest

import crsdkpy
from crsdkpy.backend import _ipc
from crsdkpy.backend.host import HostBackend

FAKE_HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_host.py")

#: How the ABI names the policies. A test asserting the wire value has to know
#: this, because the whole point is that the right value reaches the vendor.
ABI_BOUNDED = 0
ABI_VENDOR = 1


def make_backend(behaviour: str = "normal") -> HostBackend:
    return HostBackend(
        command=[sys.executable, FAKE_HOST, behaviour],
        adapter_dir=os.path.dirname(FAKE_HOST),
    )


def policies_seen(backend: HostBackend) -> list:
    """The reconnection policy the host received, per open request."""
    response, _ = backend._call(_ipc.OP_GET_COUNTERS, operation="counters")
    if not response.count:
        return []
    return [int(v) for v in response.message.decode().split(",") if v]


def test_the_default_does_not_ask_the_vendor_to_reconnect() -> None:
    """An ordinary open must not silently inherit the five-minute monitor."""
    backend = make_backend()
    try:
        backend.start()
        backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        assert policies_seen(backend) == [ABI_BOUNDED]
    finally:
        backend.shutdown()


def test_the_vendor_monitor_is_available_when_asked_for() -> None:
    """Callers who want a session to survive a cable event can still have it."""
    backend = make_backend()
    try:
        backend.start()
        backend.open_session(
            "cam-0",
            crsdkpy.SessionMode.REMOTE,
            reconnect=crsdkpy.ReconnectPolicy.VENDOR,
        )
        assert policies_seen(backend) == [ABI_VENDOR]
    finally:
        backend.shutdown()


def test_camera_open_defaults_to_the_bounded_policy() -> None:
    """The public entry point, not just the backend, has to default safely."""
    import inspect

    default = inspect.signature(crsdkpy.Camera.open).parameters["reconnect"].default
    assert default is crsdkpy.ReconnectPolicy.BOUNDED


def test_the_bounded_policy_still_retries_a_connection_callback_timeout() -> None:
    """The retry is worth making only when the vendor is not already waiting.

    Under the bounded policy a failed attempt has not spent five minutes in a
    monitor, so a second attempt against a camera the first one just cleaned up
    is still the right move.
    """
    backend = make_backend("connect_timeout_once")
    try:
        backend.start()
        backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        # Two attempts, both bounded.
        assert policies_seen(backend) == [ABI_BOUNDED, ABI_BOUNDED]
    finally:
        backend.shutdown()


def test_the_vendor_policy_does_not_retry() -> None:
    """Retrying under the vendor policy would spend a second five minutes.

    A failure there means the monitor already ran to its own timeout, so the
    time went on the monitor rather than on a stale session and repeating the
    attempt changes nothing except how long the caller waits.
    """
    backend = make_backend("connect_timeout_always")
    try:
        backend.start()
        with pytest.raises(crsdkpy.CameraConnectionError):
            backend.open_session(
                "cam-0",
                crsdkpy.SessionMode.REMOTE,
                reconnect=crsdkpy.ReconnectPolicy.VENDOR,
            )
        assert policies_seen(backend) == [ABI_VENDOR]  # exactly one attempt
    finally:
        backend.shutdown()


def test_the_bounded_policy_bounds_its_retries() -> None:
    """One retry, not a loop, even when every attempt fails."""
    backend = make_backend("connect_timeout_always")
    try:
        backend.start()
        with pytest.raises(crsdkpy.CameraConnectionError):
            backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        assert policies_seen(backend) == [ABI_BOUNDED, ABI_BOUNDED]
    finally:
        backend.shutdown()
