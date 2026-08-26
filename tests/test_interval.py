"""Interval (still) shooting, and the still transfer size.

The camera owns the interval sequence: arming is a property write, every
parameter is a property, and progress is a read-only status the camera
updates. There is no start or stop operation in the vendor SDK and there is no
scheduler here, so these tests are about the settings, the arming, and the
status -- including a status value this release has never seen.

Ranges come from the camera at runtime. Nothing here asserts the reference
body's numbers as though they were the contract.

Simulated. No interval sequence has been run on hardware.
"""

from __future__ import annotations

import pytest

import crsdkpy
from crsdkpy.settings import STILL_TRANSFER_ORIGINAL, STILL_TRANSFER_SMALL
from crsdkpy.simulator import Scenario

INTERVAL_REC_MODE = 0x0505
INTERVAL_REC_STATUS = 0x0718
INTERVAL_NUMBER_OF_SHOTS = 0x01FF
INTERVAL_SHOOTING_INTERVAL = 0x01FD
STILL_IMAGE_TRANS_SIZE = 0x0506


def _open(scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario)
    session = sdk.discover()[0].open("remote")
    return sdk, session


def test_the_family_is_exposed_and_starts_disarmed() -> None:
    sdk, session = _open()
    try:
        assert session.interval.available
        assert session.interval.enabled is False
        assert session.interval.status.name == "waiting_start"
        assert session.interval.shooting is False
    finally:
        sdk.close()


def test_a_camera_without_interval_shooting_says_so() -> None:
    scenario = Scenario(unavailable_property_codes=(INTERVAL_REC_MODE,))
    sdk, session = _open(scenario)
    try:
        assert not session.interval.available
        assert session.interval.enabled is None
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            session.interval.enable()
    finally:
        sdk.close()


def test_arming_and_disarming_is_a_property_write() -> None:
    """The camera runs the sequence; CrSDKPy only arms it."""
    sdk, session = _open()
    try:
        session.interval.enable()
        assert session.interval.enabled is True
        assert session.properties.get(INTERVAL_REC_MODE).value == 2
        session.interval.disable()
        assert session.interval.enabled is False
        assert session.properties.get(INTERVAL_REC_MODE).value == 1
    finally:
        sdk.close()


def test_the_mode_only_accepts_what_the_camera_advertises() -> None:
    sdk, session = _open()
    try:
        assert session.interval.mode.allowed_values == (1, 2)
        with pytest.raises(ValueError):
            session.interval.mode.set(3)
    finally:
        sdk.close()


def test_an_unrecognised_mode_value_is_neither_armed_nor_disarmed() -> None:
    scenario = Scenario(property_value_overrides=((INTERVAL_REC_MODE, 9),))
    sdk, session = _open(scenario)
    try:
        assert session.interval.mode.value == 9
        assert session.interval.enabled is None
    finally:
        sdk.close()


def test_the_status_is_read_only_and_reports_the_cameras_progress() -> None:
    sdk, session = _open()
    try:
        status = session.interval.status
        assert status == crsdkpy.IntervalStatus(1)
        assert status.known
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="read-only"):
            session.properties.set(INTERVAL_REC_STATUS, 2)
    finally:
        sdk.close()


def test_the_status_keeps_a_value_this_release_does_not_know() -> None:
    scenario = Scenario(property_value_overrides=((INTERVAL_REC_STATUS, 4),))
    sdk, session = _open(scenario)
    try:
        status = session.interval.status
        assert status.value == 4
        assert status.name is None
        assert status.known is False
        assert status.shooting is None
        assert session.interval.shooting is None
    finally:
        sdk.close()


def test_the_status_is_absent_rather_than_invented() -> None:
    scenario = Scenario(unavailable_property_codes=(INTERVAL_REC_STATUS,))
    sdk, session = _open(scenario)
    try:
        assert session.interval.status is None
        assert session.interval.shooting is None
    finally:
        sdk.close()


def test_documented_status_values_are_named() -> None:
    assert crsdkpy.IntervalStatus(1).name == "waiting_start"
    assert crsdkpy.IntervalStatus(1).shooting is False
    assert crsdkpy.IntervalStatus(2).name == "shooting"
    assert crsdkpy.IntervalStatus(2).shooting is True


def test_parameters_use_the_ranges_the_camera_reports() -> None:
    """Not a table here: the camera's advertised range is what constrains."""
    sdk, session = _open()
    try:
        shots = session.interval.number_of_shots
        assert shots.value_range == crsdkpy.PropertyRange(1, 9999, 1)
        shots.set(120)
        assert shots.value == 120
        with pytest.raises(ValueError):
            shots.set(10_000)
        with pytest.raises(ValueError):
            shots.set(0)
    finally:
        sdk.close()


def test_the_shooting_interval_keeps_the_cameras_own_units() -> None:
    """The vendor documents it as ten times the seconds; no scaling happens."""
    sdk, session = _open()
    try:
        interval = session.interval.shooting_interval
        assert interval.value == 300           # 30.0 s, in tenths
        interval.set(15)                       # 1.5 s, in tenths
        assert interval.value == 15
        assert interval.value_range == crsdkpy.PropertyRange(10, 600, 1)
        with pytest.raises(ValueError):
            interval.set(5)
    finally:
        sdk.close()


def test_the_enumerated_parameters_expose_their_documented_values() -> None:
    sdk, session = _open()
    try:
        assert session.interval.interval_priority.allowed_values == (1, 2)
        assert session.interval.ae_tracking_sensitivity.allowed_values == (1, 2, 3, 4)
        assert session.interval.shutter_type.allowed_values == (1, 2, 3)
        session.interval.shutter_type.set(3)   # electronic
        assert session.interval.shutter_type.value == 3
    finally:
        sdk.close()


def test_a_read_only_parameter_is_reported_as_such() -> None:
    scenario = Scenario(read_only_property_codes=(INTERVAL_SHOOTING_INTERVAL,))
    sdk, session = _open(scenario)
    try:
        assert not session.interval.shooting_interval.writable
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="read-only"):
            session.interval.shooting_interval.set(100)
    finally:
        sdk.close()


# -- still transfer size ----------------------------------------------------

def test_the_still_transfer_size_exposes_the_documented_pair() -> None:
    sdk, session = _open()
    try:
        assert session.still_transfer.size.allowed_values == (0, 1)
        # The reference body reported SmallSize, which is why a host-bound
        # still arrived downscaled rather than as the captured file.
        assert session.still_transfer.size.value == STILL_TRANSFER_SMALL
        assert session.still_transfer.is_original is False
        session.still_transfer.size.set(STILL_TRANSFER_ORIGINAL)
        assert session.still_transfer.is_original is True
    finally:
        sdk.close()


def test_an_unrecognised_transfer_size_is_neither_original_nor_small() -> None:
    scenario = Scenario(property_value_overrides=((STILL_IMAGE_TRANS_SIZE, 5),))
    sdk, session = _open(scenario)
    try:
        assert session.still_transfer.size.value == 5
        assert session.still_transfer.is_original is None
    finally:
        sdk.close()


def test_an_absent_transfer_size_says_nothing_about_delivery() -> None:
    scenario = Scenario(unavailable_property_codes=(STILL_IMAGE_TRANS_SIZE,))
    sdk, session = _open(scenario)
    try:
        assert not session.still_transfer.size.available
        assert session.still_transfer.is_original is None
    finally:
        sdk.close()
