import pytest

import crsdkpy
from crsdkpy.simulator import Scenario


def _open(scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario)
    session = sdk.discover()[0].open("remote_transfer")
    return sdk, session


def test_successful_multi_position_bracket() -> None:
    sdk, session = _open()
    try:
        result = session.focus.capture_bracket([30_000, 30_200, 30_400])
        assert result.successful
        assert [frame.commanded_position for frame in result.frames] == [
            30_000,
            30_200,
            30_400,
        ]
        assert [frame.settled_position for frame in result.frames] == [
            30_000,
            30_200,
            30_400,
        ]
        assert all(frame.exposed for frame in result.frames)
        assert all(frame.identity_resolved for frame in result.frames)
        assert all(frame.capture.focus is None for frame in result.frames)
        assert len({frame.capture.content.content_id for frame in result.frames}) == 3
    finally:
        sdk.close()


def test_repeated_position_still_gets_exactly_one_capture_per_entry() -> None:
    sdk, session = _open()
    try:
        result = session.focus.capture_bracket([30_100, 30_100])
        assert len(result.frames) == 2
        assert all(frame.settled_position == 30_100 for frame in result.frames)
        assert len({frame.capture.content.content_id for frame in result.frames}) == 2
    finally:
        sdk.close()


def test_entire_sequence_is_validated_before_first_move() -> None:
    sdk, session = _open()
    try:
        with pytest.raises(ValueError, match="advertised constraints"):
            session.focus.capture_bracket([30_100, 70_000])
        assert session.focus.position == 30_000
        assert session.content.latest() is None
    finally:
        sdk.close()


def test_movement_timeout_mid_bracket_carries_partial_result() -> None:
    scenario = Scenario(focus_stuck_after_moves=1)
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.FocusBracketError) as caught:
            session.focus.capture_bracket(
                [30_100, 30_200, 30_300], settle_timeout_ms=250
            )
        result = caught.value.result
        assert len(result.frames) == 1
        assert result.frames[0].commanded_position == 30_100
        assert isinstance(result.failure, crsdkpy.OperationTimeoutError)
    finally:
        sdk.close()


def test_no_exposure_is_recorded_without_retry() -> None:
    scenario = Scenario(capture_without_exposure=True)
    sdk, session = _open(scenario)
    try:
        result = session.focus.capture_bracket(
            [30_100, 30_200], exposure_timeout_ms=200
        )
        assert len(result.frames) == 2
        assert all(not frame.exposed for frame in result.frames)
        assert all(not frame.identity_resolved for frame in result.frames)
        assert all(
            frame.capture.state is crsdkpy.CaptureState.FAILED
            for frame in result.frames
        )
    finally:
        sdk.close()


def test_exposed_but_unresolved_identity_is_not_retried() -> None:
    scenario = Scenario(content_extra_delay_ms=30_000)
    sdk, session = _open(scenario)
    try:
        result = session.focus.capture_bracket(
            [30_100, 30_200], content_timeout_ms=100
        )
        assert len(result.frames) == 2
        assert all(frame.exposed for frame in result.frames)
        assert all(not frame.identity_resolved for frame in result.frames)
        assert all(
            frame.capture.state is crsdkpy.CaptureState.EXPOSED
            for frame in result.frames
        )
    finally:
        sdk.close()


def test_session_loss_terminates_bracket_cleanly() -> None:
    scenario = Scenario(focus_stuck_driving=True, focus_session_loss_ms=100)
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.FocusBracketError) as caught:
            session.focus.capture_bracket([30_100, 30_200])
        assert caught.value.result.frames == ()
        assert isinstance(
            caught.value.result.failure, crsdkpy.InvalidSessionStateError
        )
    finally:
        sdk.close()


def test_optional_starting_position_restoration_succeeds() -> None:
    sdk, session = _open()
    try:
        result = session.focus.capture_bracket([30_300], restore_start=True)
        assert result.restoration_requested
        assert result.starting_position == 30_000
        assert result.restored_position == 30_000
        assert result.restoration_failure is None
        assert session.focus.position == 30_000
    finally:
        sdk.close()


def test_restoration_failure_is_reported_with_completed_frames() -> None:
    scenario = Scenario(focus_stuck_after_moves=1)
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.FocusBracketError) as caught:
            session.focus.capture_bracket(
                [30_300], restore_start=True, settle_timeout_ms=250
            )
        result = caught.value.result
        assert len(result.frames) == 1
        assert result.failure is None
        assert isinstance(result.restoration_failure, crsdkpy.OperationTimeoutError)
        assert result.restored_position is None
    finally:
        sdk.close()
