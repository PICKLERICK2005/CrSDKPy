"""Zoom controls.

Every documented zoom control is a property, so these tests are about what
the camera says rather than about a native call: whether the family is
exposed, whether a drive is currently allowed, and what happens when it is
not. Support is never inferred from a model name -- the reference body
exposed this entire family with a lens that could not motorise anything.

Simulated. No zoom motion has been exercised on hardware.
"""

from __future__ import annotations

import pytest

import crsdkpy
from crsdkpy.simulator import Scenario

ZOOM_OPERATION = 0x0126
ZOOM_OPERATION_STATUS = 0x071E
ZOOM_TYPE_STATUS = 0x0720
ZOOM_POSITION_SETTING = 0x02BC
ZOOM_POSITION_CURRENT = 0x07A1
ZOOM_DRIVING_STATUS = 0x0792
ZOOM_SPEED_RANGE = 0x0724
ZOOM_BAR_INFORMATION = 0x071F


def _open(scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario)
    session = sdk.discover()[0].open("remote")
    return sdk, session


def test_the_family_is_reported_by_the_camera_not_by_the_model() -> None:
    sdk, session = _open()
    try:
        assert session.zoom.available
        assert session.zoom.operable
    finally:
        sdk.close()


def test_a_camera_that_does_not_expose_zoom_says_so() -> None:
    scenario = Scenario(unavailable_property_codes=(ZOOM_OPERATION_STATUS,))
    sdk, session = _open(scenario)
    try:
        assert not session.zoom.available
        assert session.zoom.operation_status is None
        assert not session.zoom.operable
    finally:
        sdk.close()


def test_a_drive_needs_the_documented_precondition() -> None:
    """The vendor allows the operation only while the status is Enable, and
    its own sample refuses the write otherwise."""
    scenario = Scenario(zoom_operation_enabled=False)
    sdk, session = _open(scenario)
    try:
        assert session.zoom.available          # exposed but not drivable
        assert not session.zoom.operable
        with pytest.raises(
            crsdkpy.UnsupportedOperationError, match="does not currently allow"
        ):
            session.zoom.operate(1)
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            session.zoom.stop()
    finally:
        sdk.close()


def test_a_drive_is_accepted_when_the_camera_allows_it() -> None:
    sdk, session = _open()
    try:
        session.zoom.operate(1)
        assert session.properties.get(ZOOM_OPERATION).value == 1
        session.zoom.operate(-1)
        assert session.properties.get(ZOOM_OPERATION).value == -1
        session.zoom.stop()
        assert session.properties.get(ZOOM_OPERATION).value == 0
    finally:
        sdk.close()


def test_the_drive_speed_is_bounded_by_what_the_camera_advertises() -> None:
    sdk, session = _open()
    try:
        advertised = session.zoom.speed_range
        assert advertised == crsdkpy.PropertyRange(-8, 8, 1)
        session.zoom.operate(advertised.maximum)
        with pytest.raises(ValueError):
            session.zoom.operate(advertised.maximum + 1)
        with pytest.raises(ValueError):
            session.zoom.operate(advertised.minimum - 1)
    finally:
        sdk.close()


def test_a_drive_speed_must_be_an_integer() -> None:
    sdk, session = _open()
    try:
        with pytest.raises(TypeError):
            session.zoom.operate(1.5)
        with pytest.raises(TypeError):
            session.zoom.operate(True)
    finally:
        sdk.close()


def test_an_absolute_move_reports_progress_through_driving_status() -> None:
    """The vendor documents the driving status as reflecting exactly this."""
    sdk, session = _open()
    try:
        start = session.zoom.position
        assert start == 16_384
        assert session.zoom.is_driving is False
        session.zoom.move_to(20_000)
        assert session.zoom.is_driving is True
        deadline = 3_000
        while session.zoom.is_driving and deadline > 0:
            session.drain_events(timeout_ms=25)
            deadline -= 25
        assert session.zoom.position == 20_000
        assert session.zoom.is_driving is False
    finally:
        sdk.close()


def test_an_absolute_move_is_bounded_by_the_advertised_range() -> None:
    sdk, session = _open()
    try:
        assert session.zoom.position_range == crsdkpy.PropertyRange(0, 65535, 1)
        with pytest.raises(ValueError):
            session.zoom.move_to(70_000)
        with pytest.raises(TypeError):
            session.zoom.move_to("wide")
    finally:
        sdk.close()


def test_an_absolute_move_is_refused_when_not_exposed() -> None:
    scenario = Scenario(unavailable_property_codes=(ZOOM_POSITION_SETTING,))
    sdk, session = _open(scenario)
    try:
        with pytest.raises(
            crsdkpy.UnsupportedOperationError, match="absolute zoom positioning"
        ):
            session.zoom.move_to(20_000)
    finally:
        sdk.close()


def test_zoom_type_status_keeps_a_value_this_release_does_not_know() -> None:
    scenario = Scenario(property_value_overrides=((ZOOM_TYPE_STATUS, 9),))
    sdk, session = _open(scenario)
    try:
        status = session.zoom.type_status
        assert status.value == 9
        assert status.name is None
        assert status.known is False
        assert status.is_optical is None
    finally:
        sdk.close()


def test_documented_zoom_type_values_are_named() -> None:
    for value, name in ((1, "optical"), (2, "smart"), (3, "clear_image"),
                        (4, "digital")):
        status = crsdkpy.ZoomTypeStatus(value)
        assert status.name == name
        assert status.known
    assert crsdkpy.ZoomTypeStatus(1).is_optical is True
    assert crsdkpy.ZoomTypeStatus(4).is_optical is False


def test_operation_status_keeps_an_unrecognised_value() -> None:
    scenario = Scenario(property_value_overrides=((ZOOM_OPERATION_STATUS, 5),))
    sdk, session = _open(scenario)
    try:
        status = session.zoom.operation_status
        assert status.value == 5
        assert status.enabled is None
        # Unknown is not permission: the drive is still refused.
        assert not session.zoom.operable
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="5"):
            session.zoom.operate(1)
    finally:
        sdk.close()


def test_driving_status_keeps_an_unrecognised_value() -> None:
    scenario = Scenario(property_value_overrides=((ZOOM_DRIVING_STATUS, 7),))
    sdk, session = _open(scenario)
    try:
        status = session.zoom.driving_status
        assert status.value == 7
        assert status.name is None
        assert status.is_driving is None
        assert session.zoom.is_driving is None
    finally:
        sdk.close()


def test_the_zoom_bar_splits_the_way_the_vendor_documents_it() -> None:
    """One number carrying three fields: total boxes, current box, position."""
    sdk, session = _open()
    try:
        bar = session.zoom.bar_information
        assert bar.total_boxes == 8
        assert bar.current_box == 2
        assert bar.position_in_box == 50
        # The number itself survives the split.
        assert bar.value == (8 << 24) | (2 << 16) | 50
    finally:
        sdk.close()


def test_the_zoom_bar_handles_the_documented_extremes() -> None:
    bar = crsdkpy.ZoomBarInformation(0xFFFFFFFF)
    assert bar.total_boxes == 0xFF
    assert bar.current_box == 0xFF
    assert bar.position_in_box == 0xFFFF
    assert crsdkpy.ZoomBarInformation(0).total_boxes == 0


def test_a_body_without_a_speed_range_reports_none() -> None:
    scenario = Scenario(unavailable_property_codes=(ZOOM_SPEED_RANGE,))
    sdk, session = _open(scenario)
    try:
        assert session.zoom.speed_range is None
        # A drive is still allowed; the documented floor is the fixed triple.
        session.zoom.operate(crsdkpy.zoom.ZOOM_TELE)
    finally:
        sdk.close()


def test_scale_and_distance_keep_the_cameras_own_units() -> None:
    sdk, session = _open()
    try:
        assert session.zoom.scale.value == 1000        # x1.0 in 0.001 units
        assert session.zoom.scale.value_range == crsdkpy.PropertyRange(
            1000, 8000, 200
        )
        # No conversion is applied, so x1.2 is the camera's 1200.
        session.zoom.scale.set(1200)
        assert session.zoom.scale.value == 1200
    finally:
        sdk.close()


def test_zoom_settings_are_read_only_where_the_camera_says_so() -> None:
    scenario = Scenario(read_only_property_codes=(ZOOM_POSITION_SETTING,))
    sdk, session = _open(scenario)
    try:
        assert not session.zoom.position_setting.writable
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="read-only"):
            session.zoom.move_to(20_000)
    finally:
        sdk.close()
