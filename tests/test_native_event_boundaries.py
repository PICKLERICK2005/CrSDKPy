"""Native provenance must not be replaced with host delivery-time freshness."""

import ctypes
import inspect

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.backend import _cabi
from crsdkpy.backend.native import decode_event
from crsdkpy.events import CaptureEvent, ContentEvent, FocusEvent, NativeEventInfo
from crsdkpy.session import Session
from crsdkpy.simulator import AfOutcome, Scenario
from test_focus_event_queue import install_batch
from test_host_ipc import make_backend


def native(event, timestamp, sequence):
    object.__setattr__(event, "native", NativeEventInfo(timestamp, sequence))
    return event


def test_host_exposes_native_operation_watermark():
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        with sdk.discover()[0].open("remote") as session:
            session.properties.set(1, 2)
            assert backend.operation_boundary(session._id, "property", 1) is not None
            events = backend.poll_events(session._id)
            assert events and all(e.native is not None for e in events)


@pytest.mark.parametrize(
    "sequence,timestamp,expected",
    [
        (9, 101, False),
        (10, 100, False),
        (11, 99, False),
        (11, 100, True),
        (12, 105, True),
        (0x8000000A, 101, False),
    ],
)
def test_order_and_time_are_both_native(sequence, timestamp, expected):
    event = native(FocusEvent(timestamp_ms=999999), timestamp, sequence)
    assert Session._after_boundary(event, NativeEventInfo(100, 10)) is expected


def test_sequence_wrap_and_missing_provenance():
    assert Session._after_boundary(
        native(CaptureEvent(), 101, 0), NativeEventInfo(100, 0xFFFFFFFF)
    )
    assert not Session._after_boundary(CaptureEvent(), NativeEventInfo(100, 10))


def test_decoder_preserves_raw_time_sequence_and_vendor_code():
    raw = _cabi.EventStruct(
        kind=_cabi.EVENT_FOCUS,
        reserved=-1,
        timestamp_ms=42,
        code=0x1234,
        i0=2,
        i1=_cabi.FOCUS_SRC_WARNING,
    )
    event = decode_event(raw, timestamp_ms=999)
    assert event.timestamp_ms == 999
    assert event.native == NativeEventInfo(42, 0xFFFFFFFF, raw.kind, 0x1234)
    assert event.state is crsdkpy.FocusState.FOCUSED_AF_S
    assert ctypes.sizeof(_cabi.EventBoundaryStruct) == 16


def test_positional_event_api_and_equality_are_unchanged():
    assert "native" not in inspect.signature(CaptureEvent).parameters
    assert CaptureEvent(123, 7).sequence == 7
    assert native(CaptureEvent(123, 7), 42, 3) == CaptureEvent(123, 7)


def test_old_native_focus_delivered_after_s1_does_not_authorize_s2(monkeypatch):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            old = native(FocusEvent(state=crsdkpy.FocusState.FOCUSED_AF_S), 99, 9)
            backend = install_batch(session, monkeypatch, [old])
            monkeypatch.setattr(
                backend,
                "operation_boundary",
                lambda *args: NativeEventInfo(100, 10),
                raising=False,
            )
            sent = []
            monkeypatch.setattr(
                backend, "send_command", lambda *args: sent.append(args)
            )
            with pytest.raises(crsdkpy.AutofocusFailedError):
                session.autofocus_and_capture(focus_timeout_ms=200)
            assert sent == []
            assert old in session._user_events


@pytest.mark.parametrize("new_confirmation", [False, True])
def test_callback_arrival_across_s2_boundary(monkeypatch, new_confirmation):
    with make_sdk(scenario=Scenario(capture_without_exposure=True)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = session._backend
            poll, send = backend.poll_events, backend.send_command
            attempts = []
            delivered = []
            old = native(CaptureEvent(sequence=123), 99, 9)
            current = native(CaptureEvent(sequence=124), 101, 11)
            tail = native(ContentEvent(), 102, 12)

            def command(*args):
                attempts.append(args[-1])
                return send(*args)

            def events(*args, **kwargs):
                if len(attempts) == 2 and not delivered:
                    delivered.append(True)
                    return [old, current, tail] if new_confirmation else [old]
                return poll(*args, **kwargs)

            monkeypatch.setattr(backend, "send_command", command)
            monkeypatch.setattr(backend, "poll_events", events)
            monkeypatch.setattr(
                backend,
                "operation_boundary",
                lambda sid, op, code: (
                    NativeEventInfo(100, 10) if op == "command" else None
                ),
                raising=False,
            )
            capture = session.autofocus_and_capture(timeout_ms=200)
            assert capture.exposed is new_confirmation
            assert len(attempts) == 2
            assert old in session._user_events
            if new_confirmation:
                assert tail in session._op_events
