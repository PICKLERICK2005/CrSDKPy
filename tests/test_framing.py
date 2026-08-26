"""Electronic framing.

Framing is the one family here with real operations and a documented order
between them: an absolute execute, and then relative updates that the vendor
states are "enabled after ExecuteEframing has been successfully executed" and
cannot move a frame on their own. These tests pin that ordering, the vendor's
fixed x1024 scaling, and the parameter widths the scaled values have to fit.

Simulated. No framing operation has been exercised on hardware.
"""

from __future__ import annotations

import pytest

import crsdkpy
from crsdkpy.framing import EFRAMING_SCALE, FramingArea, FramingRectangle, FramingType
from crsdkpy.simulator import Scenario

EFRAMING_TYPE = 0x07D1
EFRAMING_COMMAND_VERSION = 0x07D2


def _open(scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario)
    session = sdk.discover()[0].open("remote")
    return sdk, session


def _execute(session, **overrides):
    request = {
        "horizontal_denominator": 640,
        "vertical_denominator": 480,
        "framing_type": FramingType.AUTO,
        "input_areas": {1: FramingRectangle(1, 1, 640, 480)},
        "output_areas": {1: FramingRectangle(0, 0, 640, 480)},
    }
    request.update(overrides)
    session.framing.execute(**request)


def test_availability_comes_from_the_cameras_command_version() -> None:
    """The vendor gates the feature there: disabled flag means not executable."""
    sdk, session = _open()
    try:
        assert session.framing.available
        assert session.framing.command_version == 100
    finally:
        sdk.close()


def test_a_camera_without_the_command_version_reports_unavailable() -> None:
    scenario = Scenario(unavailable_property_codes=(EFRAMING_COMMAND_VERSION,))
    sdk, session = _open(scenario)
    try:
        assert not session.framing.available
        assert session.framing.command_version is None
        with pytest.raises(
            crsdkpy.UnsupportedOperationError, match="electronic framing"
        ):
            _execute(session)
    finally:
        sdk.close()


def test_an_update_before_an_execute_is_refused() -> None:
    """The vendor's ordering, enforced rather than sent for the camera to drop."""
    sdk, session = _open()
    try:
        assert not session.framing.executed
        with pytest.raises(
            crsdkpy.UnsupportedOperationError, match="after execute"
        ):
            session.framing.update_area(1, FramingArea.INPUT, x=1)
    finally:
        sdk.close()


def test_an_execute_then_an_update_is_the_documented_sequence() -> None:
    sdk, session = _open()
    try:
        _execute(session)
        assert session.framing.executed
        session.framing.update_area(1, FramingArea.INPUT, x=10, y=-10, width=2)

        applied = session._backend._sessions[session._id].framing_executions
        assert len(applied) == 1
        # Denominators and rectangles all carry the vendor's fixed scaling.
        assert applied[0]["horizontal_denominator"] == 640 * EFRAMING_SCALE
        assert applied[0]["vertical_denominator"] == 480 * EFRAMING_SCALE
        assert applied[0]["input_areas"] == {
            1: (1 * EFRAMING_SCALE, 1 * EFRAMING_SCALE,
                640 * EFRAMING_SCALE, 480 * EFRAMING_SCALE)
        }
        assert applied[0]["output_areas"] == {
            1: (0, 0, 640 * EFRAMING_SCALE, 480 * EFRAMING_SCALE)
        }

        updates = session._backend._sessions[session._id].framing_updates
        assert len(updates) == 1
        assert updates[0]["area"] == 1
        assert updates[0]["group"] == FramingArea.INPUT
        assert updates[0]["x"] == 10 * EFRAMING_SCALE
        assert updates[0]["y"] == -10 * EFRAMING_SCALE
        assert updates[0]["width"] == 2 * EFRAMING_SCALE
        assert updates[0]["height"] == 0
    finally:
        sdk.close()


def test_the_camera_reports_the_applied_type_back() -> None:
    sdk, session = _open()
    try:
        assert session.framing.type_name == "none"
        _execute(session, framing_type=FramingType.PTZ)
        assert session.framing.type == FramingType.PTZ
        assert session.framing.type_name == "ptz"
    finally:
        sdk.close()


def test_an_unrecognised_framing_type_passes_through_unchanged() -> None:
    """A firmware may add one; a number this release has no name for is still
    a legal thing to send and to read back."""
    sdk, session = _open()
    try:
        _execute(session, framing_type=0x42)
        assert session.framing.type == 0x42
        assert session.framing.type_name is None
    finally:
        sdk.close()


def test_a_refused_execute_leaves_the_update_gate_shut() -> None:
    scenario = Scenario(framing_execute_fails=True)
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.CameraConnectionError):
            _execute(session)
        assert not session.framing.executed
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            session.framing.update_area(1, FramingArea.INPUT, x=1)
    finally:
        sdk.close()


def test_area_numbers_are_bounded_by_the_vendors_maximum() -> None:
    sdk, session = _open()
    try:
        _execute(session, input_areas={4: FramingRectangle(0, 0, 10, 10)})
        with pytest.raises(ValueError, match="area number must be 1 to 4"):
            _execute(session, input_areas={5: FramingRectangle(0, 0, 10, 10)})
        with pytest.raises(ValueError, match="area number must be 1 to 4"):
            _execute(session, output_areas={0: FramingRectangle(0, 0, 10, 10)})
        with pytest.raises(ValueError, match="area number must be 1 to 4"):
            session.framing.update_area(9, FramingArea.INPUT, x=1)
    finally:
        sdk.close()


def test_the_area_group_must_be_one_the_vendor_defines() -> None:
    sdk, session = _open()
    try:
        _execute(session)
        session.framing.update_area(1, FramingArea.OUTPUT, y=1)
        with pytest.raises(ValueError, match="INPUT or"):
            session.framing.update_area(1, 3, y=1)
    finally:
        sdk.close()


def test_a_value_that_will_not_fit_once_scaled_is_refused() -> None:
    """The vendor's sample truncates into a 16-bit parameter without checking.

    Sending a different rectangle than the caller asked for is worse than
    refusing, so the relative update refuses instead.
    """
    sdk, session = _open()
    try:
        _execute(session)
        session.framing.update_area(1, FramingArea.INPUT, x=31)
        session.framing.update_area(1, FramingArea.INPUT, x=-32)
        with pytest.raises(ValueError, match="does not fit"):
            session.framing.update_area(1, FramingArea.INPUT, x=32)
        with pytest.raises(ValueError, match="does not fit"):
            session.framing.update_area(1, FramingArea.INPUT, height=-33)
    finally:
        sdk.close()


def test_an_execute_coordinate_uses_the_wider_vendor_parameter() -> None:
    sdk, session = _open()
    try:
        # Comfortably past the 16-bit limit the relative update has.
        _execute(session, input_areas={1: FramingRectangle(0, 0, 100_000, 100_000)})
        with pytest.raises(ValueError, match="does not fit"):
            _execute(session, horizontal_denominator=3_000_000)
    finally:
        sdk.close()


def test_coordinates_must_be_integers() -> None:
    sdk, session = _open()
    try:
        with pytest.raises(TypeError):
            _execute(session, horizontal_denominator=640.5)
        with pytest.raises(TypeError):
            _execute(session, framing_type="auto")
        _execute(session)
        with pytest.raises(TypeError):
            session.framing.update_area(1, FramingArea.INPUT, x=1.5)
    finally:
        sdk.close()


def test_an_execute_with_no_areas_is_still_a_complete_request() -> None:
    """A description can name no areas; that is the caller's call, not ours."""
    sdk, session = _open()
    try:
        session.framing.execute(
            horizontal_denominator=1920,
            vertical_denominator=1080,
            framing_type=FramingType.NONE,
        )
        applied = session._backend._sessions[session._id].framing_executions
        assert applied[0]["input_areas"] == {}
        assert applied[0]["output_areas"] == {}
    finally:
        sdk.close()


def test_framing_settings_reach_the_generic_setting_layer() -> None:
    sdk, session = _open()
    try:
        assert session.framing.camera_framing.available
        assert not session.framing.camera_framing.writable  # camera says so
        assert session.framing.recording_image_crop.writable
        assert session.framing.recording_image_crop.allowed_values == (1, 2)
        session.framing.hdmi_crop.set(2)
        assert session.framing.hdmi_crop.value == 2
    finally:
        sdk.close()


def test_framing_operations_fail_explicitly_on_a_closed_session() -> None:
    sdk, session = _open()
    try:
        _execute(session)
        session.close()
        with pytest.raises(crsdkpy.SessionClosedError):
            _execute(session)
    finally:
        sdk.close()


def test_documented_framing_types_are_named_and_others_are_not() -> None:
    for value, name in (
        (1, "none"), (2, "auto"), (3, "single"), (5, "ptz"),
        (8, "hold_current_position"), (9, "force_zoom_out"),
    ):
        assert FramingType.name_for(value) == name
    for value in (0, 4, 6, 7, 10, 0x42):
        assert FramingType.name_for(value) is None
