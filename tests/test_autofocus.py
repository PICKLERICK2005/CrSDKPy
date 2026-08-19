"""Autofocus semantics.

Each test here corresponds to a behaviour observed on hardware. The gate must
accept a focused state from either asynchronous channel, tolerate them
disagreeing, survive a value that never notifies, refuse to treat tracking as
success, and clean up the half-press stage correctly on both paths.
"""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import AfOutcome, FocusChannel, Scenario
from crsdkpy.simulator import profiles as P


def _session(scenario: Scenario, profile: str = "fx3a", mode: str = "remote"):
    sdk = make_sdk(profile=profile, scenario=scenario)
    camera = sdk.discover()[0]
    return sdk, camera.open(mode)


def test_af_s_success(session: crsdkpy.Session) -> None:
    result = session.autofocus()
    assert result.confirmed
    assert result is not None and bool(result)
    assert result.state is crsdkpy.FocusState.FOCUSED_AF_S
    assert result.elapsed_ms > 0


def test_focus_state_helpers() -> None:
    assert crsdkpy.FocusState.FOCUSED_AF_S.is_focused
    assert crsdkpy.FocusState.FOCUSED_AF_C.is_focused
    assert not crsdkpy.FocusState.TRACKING_AF_C.is_focused
    assert not crsdkpy.FocusState.UNLOCKED.is_focused
    assert crsdkpy.FocusState.NOT_FOCUSED_AF_S.is_failure


def test_no_lock_times_out_without_confirming() -> None:
    sdk, session = _session(Scenario(af_outcome=AfOutcome.NO_LOCK))
    try:
        result = session.autofocus(timeout_ms=3_000)
        assert not result.confirmed
        assert result.state is crsdkpy.FocusState.NOT_FOCUSED_AF_S
    finally:
        sdk.close()


def test_silent_af_times_out() -> None:
    sdk, session = _session(Scenario(af_outcome=AfOutcome.SILENT))
    try:
        result = session.autofocus(timeout_ms=1_000)
        assert not result.confirmed
    finally:
        sdk.close()


def test_property_channel_may_lead() -> None:
    sdk, session = _session(Scenario(af_leading_channel=FocusChannel.PROPERTY))
    try:
        result = session.autofocus()
        assert result.confirmed
        assert result.source == crsdkpy.FocusSource.PROPERTY
    finally:
        sdk.close()


def test_status_channel_may_lead() -> None:
    sdk, session = _session(Scenario(af_leading_channel=FocusChannel.STATUS))
    try:
        result = session.autofocus()
        assert result.confirmed
        assert result.source == crsdkpy.FocusSource.STATUS_WARNING
    finally:
        sdk.close()


def test_transient_channel_disagreement_still_confirms() -> None:
    """The status channel reported focus while the property still said tracking.

    Gating on the property's latest value alone would have stalled here.
    """
    scenario = Scenario(
        af_outcome=AfOutcome.TRACKING_THEN_FOCUS,
        af_leading_channel=FocusChannel.STATUS,
        af_channel_skew_ms=100,
    )
    sdk, session = _session(scenario)
    try:
        result = session.autofocus()
        assert result.confirmed
        assert result.source == crsdkpy.FocusSource.STATUS_WARNING
        # At the moment of confirmation the property channel had not caught up:
        # it still reports a non-focused value. A gate reading only the
        # property would have kept waiting on an already-focused camera.
        lagging = session.properties.get(P.CODE_FOCUS_INDICATION).value
        assert lagging != P.FOCUS_FOCUSED_AF_C
        assert session._backend.focus_state(session._id).is_focused is False
    finally:
        sdk.close()


def test_tracking_is_not_treated_as_focus() -> None:
    """TrackingSubject_AF_C precedes real focus and must not gate a release."""
    scenario = Scenario(af_outcome=AfOutcome.TRACKING_THEN_FOCUS)
    sdk, session = _session(scenario)
    try:
        result = session.autofocus()
        assert result.confirmed
        assert result.state is crsdkpy.FocusState.FOCUSED_AF_C
        # Confirmation happened at the focus timing, not the earlier tracking one.
        timings = P.FX3A.timings
        assert result.elapsed_ms >= timings.focus_tracking
    finally:
        sdk.close()


def test_tracking_only_never_confirms() -> None:
    """If AF never gets past tracking, the gate must not fire."""
    sdk = make_sdk(scenario=Scenario(af_outcome=AfOutcome.TRACKING_THEN_FOCUS))
    camera = sdk.discover()[0]
    session = camera.open("remote")
    try:
        # A timeout shorter than the focus point but longer than tracking.
        result = session.autofocus(timeout_ms=140)
        assert not result.confirmed
        assert result.state is crsdkpy.FocusState.TRACKING_AF_C
    finally:
        sdk.close()


def test_sticky_focus_value_found_by_direct_read() -> None:
    """Already-focused values emit no change event; only a read finds them."""
    sdk, session = _session(Scenario(af_sticky=True))
    try:
        result = session.autofocus()
        assert result.confirmed
        assert result.source == crsdkpy.FocusSource.DIRECT_READ
        assert result.elapsed_ms == 0
    finally:
        sdk.close()


def test_half_press_released_after_failed_focus() -> None:
    """A failed autofocus leaves the half-press engaged; we must release it."""
    sdk, session = _session(Scenario(af_outcome=AfOutcome.NO_LOCK))
    try:
        assert not session.autofocus(timeout_ms=2_000).confirmed
        assert session.raw.half_press is False
    finally:
        sdk.close()


def test_half_press_left_engaged_after_success_for_release(
    session: crsdkpy.Session,
) -> None:
    """Success leaves it engaged so a release can follow immediately."""
    result = session.autofocus()
    assert result.confirmed
    assert session.raw.half_press is True


def test_stale_half_press_cleared_before_new_attempt() -> None:
    sdk, session = _session(Scenario(af_outcome=AfOutcome.NO_LOCK))
    try:
        session.raw.set_half_press(True)
        assert session.raw.half_press
        session.autofocus(timeout_ms=1_000)
        assert not session.raw.half_press
    finally:
        sdk.close()


def test_camera_without_half_press_rejects_autofocus() -> None:
    with make_sdk(profile="minimal_still") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                session.autofocus()
            assert excinfo.value.capability == "autofocus_s1"


def test_af_c_reaches_focused_af_c(camera: crsdkpy.Camera) -> None:
    with camera.open("remote") as session:
        # Physically switch the body into AF-C.
        session._backend.simulate_physical_property_change(
            session._id, [P.CODE_FOCUS_MODE], values={P.CODE_FOCUS_MODE: 0x0003}
        )
        result = session.autofocus()
        assert result.confirmed
        assert result.state is crsdkpy.FocusState.FOCUSED_AF_C
