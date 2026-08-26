"""Lens identity and the lens's focus-distance table.

Identity and focus-distance information are separate features that happen to
share a word. Identity is three string properties and needs no request; the
distance table is a three-call protocol with a documented precondition, an
outcome that only arrives as a warning, and a read the vendor permits exactly
once per request. These tests pin that separation and that protocol.

Simulated throughout. The first characterized body reported the precondition
as Disable with a prime attached, so the enabled path has never met hardware.
"""

from __future__ import annotations

import pytest

import crsdkpy
from crsdkpy.simulator import Scenario

LENS_MODEL_NAME = 0x0765
LENS_INFORMATION_ENABLE_STATUS = 0x0756


def _open(scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario)
    session = sdk.discover()[0].open("remote")
    return sdk, session


def test_identity_reads_without_any_request() -> None:
    """Model, version and serial are plain string properties."""
    sdk, session = _open()
    try:
        assert session.lens.model_name == "SIM 24-70mm F2.8"
        assert session.lens.version == "02"
        assert session.lens.serial_number == "SIMLENS0001"
        assert session.lens.attached
    finally:
        sdk.close()


def test_identity_absent_when_the_camera_names_no_lens() -> None:
    scenario = Scenario(unavailable_property_codes=(LENS_MODEL_NAME,))
    sdk, session = _open(scenario)
    try:
        assert session.lens.model_name is None
        assert not session.lens.attached
        # The other identity strings are independent and still readable.
        assert session.lens.version == "02"
    finally:
        sdk.close()


def test_information_precondition_is_the_cameras_own_status() -> None:
    sdk, session = _open()
    try:
        status = session.lens.information_status
        assert status == crsdkpy.LensInformationStatus(0)
        assert status.enabled is False
        assert status.name == "disable"
        assert not session.lens.information_available
    finally:
        sdk.close()


def test_information_status_preserves_a_value_this_release_does_not_know() -> None:
    """A future firmware value stays representable rather than becoming False."""
    scenario = Scenario(property_value_overrides=((LENS_INFORMATION_ENABLE_STATUS, 7),))
    sdk, session = _open(scenario)
    try:
        status = session.lens.information_status
        assert status.value == 7
        assert status.name is None
        assert status.known is False
        # Unknown is not the same as enabled, and not the same as disabled.
        assert status.enabled is None
        assert not session.lens.information_available
    finally:
        sdk.close()


def test_information_status_absent_when_the_property_is_not_exposed() -> None:
    scenario = Scenario(
        unavailable_property_codes=(LENS_INFORMATION_ENABLE_STATUS,)
    )
    sdk, session = _open(scenario)
    try:
        assert session.lens.information_status is None
        assert not session.lens.information_available
    finally:
        sdk.close()


def test_a_lens_without_distance_data_reports_no_applicable_information() -> None:
    """What the reference body's prime produced: a request that succeeds and
    a read that has nothing to give."""
    scenario = Scenario(lens_information_enabled=True, lens_distance_rows=0)
    sdk, session = _open(scenario)
    try:
        assert session.lens.information_available
        with pytest.raises(
            crsdkpy.UnsupportedOperationError, match="no applicable lens information"
        ):
            session.lens.load_information()
        assert session.lens.distances == ()
    finally:
        sdk.close()


def test_reading_without_a_successful_request_is_refused() -> None:
    scenario = Scenario(lens_information_enabled=True, lens_distance_rows=4)
    sdk, session = _open(scenario)
    try:
        with pytest.raises(
            crsdkpy.UnsupportedOperationError, match="no applicable lens information"
        ):
            session.lens.read_information()
    finally:
        sdk.close()


def test_the_documented_sequence_yields_the_distance_table() -> None:
    scenario = Scenario(lens_information_enabled=True, lens_distance_rows=6)
    sdk, session = _open(scenario)
    try:
        rows = session.lens.load_information()
        assert len(rows) == 6
        assert rows == session.lens.distances
        # Both documented units appear, and each row keeps its own numbers.
        assert {row.unit for row in rows} == {"meter", "feet"}
        for row in rows:
            assert row.normalized_value > 0
            assert row.focus_position > 0
            assert row.data_version == 1
    finally:
        sdk.close()


def test_a_read_is_permitted_once_per_request() -> None:
    """The vendor's rule, enforced rather than quietly relaxed."""
    scenario = Scenario(lens_information_enabled=True, lens_distance_rows=4)
    sdk, session = _open(scenario)
    try:
        first = session.lens.load_information()
        assert first
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            session.lens.read_information()
        # A fresh request makes another read legal again.
        second = session.lens.load_information()
        assert second == first
    finally:
        sdk.close()


def test_a_refused_request_is_reported_with_its_vendor_warning() -> None:
    scenario = Scenario(
        lens_information_enabled=True,
        lens_distance_rows=4,
        lens_request_outcome=crsdkpy.lens.LENS_REQUEST_FAILED,
    )
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="0x0002003D"):
            session.lens.load_information()
        assert session.lens.distances == ()
    finally:
        sdk.close()


def test_a_busy_camera_is_a_refusal_not_a_success() -> None:
    scenario = Scenario(
        lens_information_enabled=True,
        lens_distance_rows=4,
        lens_request_outcome=crsdkpy.lens.LENS_REQUEST_BUSY,
    )
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="0x0002003C"):
            session.lens.load_information()
    finally:
        sdk.close()


def test_a_silent_camera_times_out_rather_than_reading_anyway() -> None:
    scenario = Scenario(
        lens_information_enabled=True,
        lens_distance_rows=4,
        lens_request_silent=True,
    )
    sdk, session = _open(scenario)
    try:
        with pytest.raises(crsdkpy.OperationTimeoutError):
            session.lens.load_information(timeout_ms=200)
    finally:
        sdk.close()


def test_the_request_outcome_reaches_the_caller_event_stream_too() -> None:
    """The warning is the vendor's only report, so it must not be swallowed."""
    scenario = Scenario(lens_information_enabled=True, lens_distance_rows=2)
    sdk, session = _open(scenario)
    try:
        session.lens.request_information()
        outcome = session.wait_for_event(crsdkpy.WarningEvent, timeout_ms=2_000)
        assert outcome.code == crsdkpy.lens.LENS_REQUEST_SUCCEEDED
    finally:
        sdk.close()


def test_lens_operations_fail_explicitly_on_a_closed_session() -> None:
    sdk, session = _open(Scenario(lens_information_enabled=True))
    try:
        session.close()
        with pytest.raises(crsdkpy.SessionClosedError):
            session.lens.request_information()
        with pytest.raises(crsdkpy.SessionClosedError):
            session.lens.read_information()
    finally:
        sdk.close()


def test_the_distance_row_keeps_an_unrecognised_unit() -> None:
    row = crsdkpy.LensDistance(
        info_type=99, data_version=2, normalized_value=10, focus_position=20
    )
    assert row.unit is None
    assert row.info_type == 99
    # The vendor's own "undefined" is a real value, not a missing one.
    assert crsdkpy.LensDistance(0, 1, 1, 1).unit is None
