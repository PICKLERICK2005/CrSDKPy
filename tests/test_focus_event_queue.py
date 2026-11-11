"""Regression for queued prior captures and contradicted focus batches."""

from collections import deque

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.events import CaptureEvent, ContentEvent, FocusEvent, WarningEvent
from crsdkpy.simulator import AfOutcome, Scenario


def focus(state, source="status_warning"):
    return FocusEvent(state=state, source=source)


def install_batch(session, monkeypatch, batch, *, before_s1=False):
    backend = session._backend
    original_set = backend.set_half_press
    original_poll = backend.poll_events
    asserted = False
    delivered = False

    def asserted_set(session_id, engaged):
        nonlocal asserted
        original_set(session_id, engaged)
        asserted = engaged

    def poll(session_id, timeout_ms=0):
        nonlocal delivered
        if not delivered and (before_s1 or asserted):
            delivered = True
            return batch
        return original_poll(session_id, timeout_ms)

    monkeypatch.setattr(backend, "set_half_press", asserted_set)
    monkeypatch.setattr(backend, "poll_events", poll)
    # This channel remains unlocked: only injected asynchronous evidence can
    # authorize release, independently of simulator property scheduling.
    monkeypatch.setattr(backend, "focus_state", lambda _: crsdkpy.FocusState.UNLOCKED)
    return backend


@pytest.mark.parametrize("queued_in_session", [False, True])
def test_prior_focused_events_cannot_authorize_new_shutter(
    monkeypatch, queued_in_session
):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            old = focus(crsdkpy.FocusState.FOCUSED_AF_S)
            backend = install_batch(
                session, monkeypatch, [] if queued_in_session else [old], before_s1=True
            )
            if queued_in_session:
                session._op_events.append(old)
                session._user_events.append(old)
            sent = []
            monkeypatch.setattr(
                backend, "send_command", lambda *args: sent.append(args)
            )
            with pytest.raises(crsdkpy.AutofocusFailedError):
                session.autofocus_and_capture(focus_timeout_ms=200)
            assert not sent
            assert old in session._user_events


def test_later_unlock_in_same_channel_rejects_earlier_focus(monkeypatch):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = install_batch(
                session,
                monkeypatch,
                [
                    focus(crsdkpy.FocusState.FOCUSED_AF_S),
                    focus(crsdkpy.FocusState.UNLOCKED),
                ],
            )
            sent = []
            monkeypatch.setattr(
                backend, "send_command", lambda *args: sent.append(args)
            )
            with pytest.raises(crsdkpy.AutofocusFailedError):
                session.autofocus_and_capture(focus_timeout_ms=200)
            assert not sent


def test_independent_lagging_channel_does_not_veto_current_focus(monkeypatch):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote") as session:
            install_batch(
                session,
                monkeypatch,
                [
                    focus(crsdkpy.FocusState.FOCUSED_AF_S),
                    focus(crsdkpy.FocusState.UNLOCKED, source="property"),
                ],
            )
            assert session.autofocus(timeout_ms=200).confirmed


def test_focus_gate_keeps_unrelated_events_and_order(monkeypatch):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote") as session:
            warning, captured, content = (
                WarningEvent(code=123),
                CaptureEvent(),
                ContentEvent(),
            )
            install_batch(
                session,
                monkeypatch,
                [warning, focus(crsdkpy.FocusState.FOCUSED_AF_S), captured, content],
            )
            assert session.autofocus(timeout_ms=200).confirmed
            retained = list(session._op_events)
            positions = [retained.index(e) for e in (warning, captured, content)]
            assert positions == sorted(positions)
            assert all(
                event in session._user_events for event in (warning, captured, content)
            )


@pytest.mark.parametrize("before_s1", [False, True])
def test_prior_capture_is_preserved_but_never_confirms_new_s2(monkeypatch, before_s1):
    scenario = Scenario(af_outcome=AfOutcome.NO_LOCK, capture_without_exposure=True)
    with make_sdk(scenario=scenario) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            old = CaptureEvent(sequence=123)
            batch = [focus(crsdkpy.FocusState.FOCUSED_AF_S), old]
            backend = install_batch(
                session,
                monkeypatch,
                batch if not before_s1 else [old],
                before_s1=before_s1,
            )
            if before_s1:
                # A current sticky property value is a valid independent gate.
                monkeypatch.setattr(
                    backend, "focus_state", lambda _: crsdkpy.FocusState.FOCUSED_AF_S
                )
            sent = []
            original = backend.send_command

            def observe(*args):
                sent.append(args)
                return original(*args)

            monkeypatch.setattr(backend, "send_command", observe)
            result = session.autofocus_and_capture(timeout_ms=200)
            assert not result.exposed
            assert old in session._user_events
            assert len(sent) == 2


def test_backlog_across_polls_is_drained_before_s1(monkeypatch):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote") as session:
            backend = session._backend
            old = focus(crsdkpy.FocusState.FOCUSED_AF_S)
            polls = deque([[WarningEvent(code=1)], [old], []])
            original = backend.poll_events
            monkeypatch.setattr(
                backend,
                "poll_events",
                lambda *a, **k: polls.popleft() if polls else original(*a, **k),
            )
            assert not session.autofocus(timeout_ms=200).confirmed
            assert old in session._user_events


def test_nonquiescing_queue_fails_before_s1_without_unbounded_polling(monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote") as session:
            calls = []
            backend = session._backend
            monkeypatch.setattr(
                backend, "poll_events", lambda *a, **k: [WarningEvent(code=1)]
            )
            monkeypatch.setattr(backend, "set_half_press", lambda *a: calls.append(a))
            with pytest.raises(crsdkpy.AutofocusFailedError, match="backlog"):
                session.autofocus(timeout_ms=200)
            assert calls == []
            assert sum(isinstance(e, WarningEvent) for e in session._user_events) == 32


def test_prior_observed_capture_cannot_confirm_s2_only_release():
    with make_sdk(scenario=Scenario(capture_without_exposure=True)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            old = CaptureEvent(sequence=123)
            session._op_events.append(old)
            session._user_events.append(old)
            assert not session.capture(timeout_ms=200).exposed
            assert old in session._user_events


def test_contradiction_already_available_in_next_ipc_page_blocks_release(monkeypatch):
    with make_sdk(scenario=Scenario(af_outcome=AfOutcome.NO_LOCK)) as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = session._backend
            original_poll, original_set = backend.poll_events, backend.set_half_press
            asserted = False
            pages = deque(
                [
                    [focus(crsdkpy.FocusState.FOCUSED_AF_S)],
                    [focus(crsdkpy.FocusState.UNLOCKED)],
                    [],
                ]
            )

            def set_s1(sid, engaged):
                nonlocal asserted
                asserted = engaged
                original_set(sid, engaged)

            def poll(*args, **kwargs):
                return (
                    pages.popleft()
                    if asserted and pages
                    else original_poll(*args, **kwargs)
                )

            monkeypatch.setattr(backend, "set_half_press", set_s1)
            monkeypatch.setattr(backend, "poll_events", poll)
            monkeypatch.setattr(
                backend, "focus_state", lambda _: crsdkpy.FocusState.UNLOCKED
            )
            sent = []
            monkeypatch.setattr(
                backend, "send_command", lambda *args: sent.append(args)
            )
            with pytest.raises(crsdkpy.AutofocusFailedError):
                session.autofocus_and_capture(focus_timeout_ms=200)
            assert sent == []


def test_continuous_post_s1_events_reject_gate_and_clean_up(monkeypatch):
    with make_sdk() as sdk:
        with sdk.discover()[0].open("remote_transfer") as session:
            backend = session._backend
            original_poll, original_set = backend.poll_events, backend.set_half_press
            asserted = False

            def set_s1(sid, engaged):
                nonlocal asserted
                asserted = engaged
                original_set(sid, engaged)

            monkeypatch.setattr(backend, "set_half_press", set_s1)
            monkeypatch.setattr(
                backend,
                "poll_events",
                lambda *a, **k: (
                    [WarningEvent(code=1)] if asserted else original_poll(*a, **k)
                ),
            )
            sent = []
            monkeypatch.setattr(
                backend, "send_command", lambda *args: sent.append(args)
            )
            with pytest.raises(crsdkpy.AutofocusFailedError):
                session.autofocus_and_capture(focus_timeout_ms=200)
            assert sent == []
            assert session.raw.half_press is False
