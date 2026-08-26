import pytest

import crsdkpy
from crsdkpy.simulator import Scenario


def _open(*, profile="fx3a", scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile=profile, scenario=scenario)
    session = sdk.discover()[0].open("remote")
    return sdk, session


def test_range_comes_from_generic_property_metadata() -> None:
    sdk, session = _open()
    try:
        assert session.focus.available
        assert session.focus.range == crsdkpy.PropertyRange(0, 65535, 1)
    finally:
        sdk.close()


def test_unreported_range_remains_none() -> None:
    sdk, session = _open(scenario=Scenario(focus_range_available=False))
    try:
        assert session.focus.available
        assert session.focus.range is None
    finally:
        sdk.close()


def test_move_to_is_nonblocking_and_settles_at_exact_target() -> None:
    sdk, session = _open(scenario=Scenario(focus_move_delay_ms=205))
    try:
        started = session._backend.clock.now_ms()
        session.focus.move_to(30_600)
        assert session._backend.clock.now_ms() == started
        assert session.focus.position == 30_000
        assert session.focus.is_driving is True
        assert session.focus.wait_until_settled() == 30_600
        assert session.focus.is_driving is False
    finally:
        sdk.close()


def test_move_to_rejects_target_outside_advertised_range() -> None:
    sdk, session = _open()
    try:
        with pytest.raises(ValueError, match="advertised constraints"):
            session.focus.move_to(65_536)
        assert session.focus.position == 30_000
    finally:
        sdk.close()


def test_relative_move_uses_current_absolute_position() -> None:
    sdk, session = _open()
    try:
        session.focus.move_relative(125)
        assert session.focus.wait_until_settled() == 30_125
    finally:
        sdk.close()


def test_relative_move_enforces_absolute_boundary() -> None:
    sdk, session = _open()
    try:
        session.focus.move_to(65_500)
        session.focus.wait_until_settled()
        with pytest.raises(ValueError):
            session.focus.move_relative(100)
    finally:
        sdk.close()


def test_already_at_target_and_repeat_target_settle_exactly() -> None:
    sdk, session = _open()
    try:
        session.focus.move_to(30_000)
        assert session.focus.wait_until_settled(timeout_ms=0) == 30_000
        session.focus.move_to(30_400)
        assert session.focus.wait_until_settled() == 30_400
        session.focus.move_to(30_400)
        assert session.focus.wait_until_settled(timeout_ms=0) == 30_400
    finally:
        sdk.close()


def test_stuck_driving_times_out_deterministically() -> None:
    scenario = Scenario(focus_stuck_driving=True)
    sdk, session = _open(scenario=scenario)
    try:
        started = session._backend.clock.now_ms()
        session.focus.move_to(31_000)
        with pytest.raises(crsdkpy.OperationTimeoutError) as caught:
            session.focus.wait_until_settled(timeout_ms=275)
        assert caught.value.timeout_ms == 275
        assert session._backend.clock.now_ms() - started == 275
    finally:
        sdk.close()


def test_session_loss_releases_focus_waiter() -> None:
    scenario = Scenario(focus_stuck_driving=True, focus_session_loss_ms=100)
    sdk, session = _open(scenario=scenario)
    try:
        session.focus.move_to(31_000)
        with pytest.raises(crsdkpy.InvalidSessionStateError):
            session.focus.wait_until_settled(timeout_ms=5_000)
        assert session._backend.clock.now_ms() < 5_000
    finally:
        sdk.close()


def test_unknown_driving_status_preserves_raw_value() -> None:
    scenario = Scenario(focus_unknown_driving_status=99)
    sdk, session = _open(scenario=scenario)
    try:
        session.focus.move_to(31_000)
        status = session.focus.driving_status
        assert status == crsdkpy.FocusDrivingStatus(99)
        assert status.name is None
        assert status.is_driving is None
        assert session.focus.is_driving is None
    finally:
        sdk.close()


def test_unsupported_camera_and_read_only_lens_fail_cleanly() -> None:
    sdk, session = _open(scenario=Scenario(focus_position_available=False))
    try:
        assert not session.focus.available
        assert session.focus.range is None
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            session.focus.move_to(100)
    finally:
        sdk.close()

    sdk, session = _open(scenario=Scenario(focus_position_writable=False))
    try:
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="not writable"):
            session.focus.move_to(30_100)
    finally:
        sdk.close()
