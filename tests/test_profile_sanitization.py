"""Turning a private capture into a tracked profile.

The capture identifies a body and the machine it was plugged into. The profile
is tracked and will be published. Everything here is about that one step, and
the inputs are synthetic: the real captures are private and do not ship, so a
test that needed them would not run for anyone else.

Two properties matter more than the details. The output must not carry
anything identifying, and it must be the same bytes every time -- a profile
that reordered itself between runs would make every future body diff unreadable.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "sanitize_characterization.py",
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("sanitize_characterization", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize = _load_tool()


def _snapshot() -> dict:
    """A capture carrying every kind of thing that must not be published."""
    properties = [
        {"code": "0x0100", "name": "FNumber", "value": 1600, "access": "read_only"},
        {"code": "0x020E", "name": "FocusPositionSetting", "value": 30000,
         "access": "read_write"},
        {"code": "0x0119", "name": "StillImageStoreDestination", "value": 2,
         "access": "read_write"},
        {"code": "0x079F", "name": "BodySerialNumber", "value": 0,
         "access": "read_only"},
        {"code": "0x07B2", "name": "ModelName", "value": 0, "access": "read_only"},
        {"code": "0x0581", "name": None, "value": 0, "access": "read_only"},
    ]
    return {
        "schema": "crsdkpy.camera_snapshot/1",
        "host": {"platform": "Windows-11-10.0.26200-SP0", "python": "3.13.14"},
        "attached_lens": "Sony FE 50mm F1.4 GM",
        "camera": {
            "model": "TEST-BODY",
            "transport": "USB",
            "usb_pid": 3922,
            "device_key": "TEST-BODY:440035003600430045003000",
            "adapter": "Cr_PTP_USB",
        },
        "surfaces": [
            {
                "control_mode": "remote",
                "still_destination": "memory_card",
                "property_count": len(properties),
                "properties": properties,
                "unknown_codes": ["0x0581"],
                "battery": "BatteryStatus(37%)",
                "storage": ["StorageSlot(1, ok, shots=4191)"],
                "events_during_open": ["PropertyChangedEvent(timestamp_ms=4152)"],
            },
            {
                "control_mode": "remote_transfer",
                "still_destination": "memory_card",
                "property_count": len(properties) - 1,
                "properties": [p for p in properties if p["code"] != "0x0581"],
                "unknown_codes": [],
            },
        ],
    }


def _harvest() -> dict:
    return {
        "schema": "crsdkpy.property_harvest/1",
        "control_mode": "remote",
        "properties": [
            # UInt16Array with a two-entry list.
            {"code": "0x0100", "data_type": 8194, "values_hex": "18062003"},
            # UInt16Range: minimum, maximum, step.
            {"code": "0x020E", "data_type": 16386, "values_hex": "0000FFFF0100"},
            # Present, advertises nothing. That is a measurement, not a gap.
            {"code": "0x0119", "data_type": 8194},
            {"code": "0x079F", "data_type": 65535, "string": "?04514164"},
            {"code": "0x07B2", "data_type": 65535, "string": "?TEST-BODY"},
            {"code": "0x0581", "data_type": 8195},
        ],
    }


def _measured() -> dict:
    return {
        "sdk_version": "2.02.00",
        "capabilities": {
            "remote+memory_card": {"live_view": True, "postview_configuration": False},
            "remote_transfer+memory_card": {
                "live_view": False,
                "postview_configuration": True,
            },
        },
        "content": {"ids_contiguous": False},
    }


def _build(**overrides) -> dict:
    return sanitize.build_profile(
        overrides.get("snapshot", _snapshot()),
        overrides.get("harvest", _harvest()),
        overrides.get("focus_map"),
        overrides.get("measured", _measured()),
        profile_id="TEST-BODY_fw1.00",
    )


# -- private data -----------------------------------------------------------
def test_the_serial_number_is_redacted_but_its_property_survives() -> None:
    """"This body reports a serial as a string" is a surface fact worth
    keeping; the number is not."""
    profile = _build()
    serial = profile["properties"]["0x079F"]
    assert serial["name"] == "BodySerialNumber"
    assert serial["data_type"] == 65535
    assert serial["value"] is None
    assert "04514164" not in json.dumps(profile)
    assert serial["redacted"]


def test_host_and_device_key_never_reach_the_profile() -> None:
    profile = _build()
    text = json.dumps(profile)
    assert "Windows-11" not in text
    assert "3.13.14" not in text
    assert "440035003600430045003000" not in text
    assert "device_key" not in text
    assert "host" not in profile


def test_session_transient_state_is_dropped() -> None:
    """Battery charge, shot counts and event streams describe one session."""
    text = json.dumps(_build())
    assert "BatteryStatus" not in text
    assert "StorageSlot" not in text
    assert "shots" not in text
    assert "events_during_open" not in text
    assert "timestamp_ms" not in text


def test_a_path_that_slipped_through_is_refused_rather_than_edited() -> None:
    """A leak means a rule is missing. Quietly deleting the value would hide
    that, so the build fails instead."""
    snapshot = _snapshot()
    snapshot["surfaces"][0]["properties"].append(
        {"code": "0x0248", "name": "SavePath", "value": "D:/DCIM/111_1108",
         "access": "read_only"}
    )
    harvest = _harvest()
    harvest["properties"].append(
        {"code": "0x0248", "data_type": 65535, "string": "?D:/DCIM/111_1108"}
    )
    with pytest.raises(SystemExit, match="private data"):
        _build(snapshot=snapshot, harvest=harvest)


@pytest.mark.parametrize(
    "value",
    [
        "C:\\Users\\someone\\capture.json",
        "/home/someone/capture.json",
        "D:/DCIM/111_1108/DSC03820.ARW",
        "DSC03821.JPG",
    ],
)
def test_the_sweep_recognises_each_kind_of_private_string(value) -> None:
    problems: list[str] = []
    sanitize.scrub({"field": value}, problems)
    assert problems, f"{value!r} was not recognised as private"


def test_the_sweep_passes_technically_meaningful_strings(value=None) -> None:
    problems: list[str] = []
    sanitize.scrub(
        {
            "model": "ILME-FX3A",
            "lens": "Sony FE 50mm F1.4 GM",
            "firmware": "2.02",
            "adapter": "Cr_PTP_USB",
            "name": "CrDeviceProperty_FocusPositionSetting",
        },
        problems,
    )
    assert problems == []


# -- normalization ----------------------------------------------------------
def test_the_output_is_byte_identical_across_runs() -> None:
    """A profile that reordered itself would make every future diff unreadable."""
    first = json.dumps(_build(), indent=1, sort_keys=True)
    second = json.dumps(_build(), indent=1, sort_keys=True)
    assert first == second


def test_property_codes_are_normalized_to_upper_case_hex() -> None:
    profile = _build()
    for code in profile["properties"]:
        assert code.startswith("0x")
        assert code == code[:2] + code[2:].upper()
        assert len(code) == 6


def test_vendor_string_artefacts_are_stripped() -> None:
    assert sanitize.clean_string("?ILME-FX3A") == "ILME-FX3A"
    assert sanitize.clean_string("\ufeff2.02") == "2.02"
    assert sanitize.clean_string("  ") is None
    assert sanitize.clean_string(None) is None


def test_a_value_set_is_decoded_with_the_vendor_type_not_the_byte_count() -> None:
    assert sanitize.decode_value_set(8194, bytes.fromhex("18062003")) == (
        "enum", [1560, 800]
    )
    assert sanitize.decode_value_set(16386, bytes.fromhex("0000FFFF0100")) == (
        "range", [0, 65535, 1]
    )
    # Signedness comes from the type: the same bytes read differently.
    assert sanitize.decode_value_set(0x3002, bytes.fromhex("44FD")) == ("enum", [-700])
    assert sanitize.decode_value_set(0x2002, bytes.fromhex("44FD")) == ("enum", [64836])


def test_a_shape_that_cannot_be_taken_apart_is_left_raw() -> None:
    # A range must be exactly three elements.
    assert sanitize.decode_value_set(16386, bytes.fromhex("00000100")) == ("raw", [])
    # An unknown base type is not guessed at.
    assert sanitize.decode_value_set(0x2FFF, bytes.fromhex("0000")) == ("raw", [])


def test_advertising_nothing_is_recorded_as_a_measurement() -> None:
    """"none" and a missing field are different claims: one says the property
    advertises nothing, the other says nobody looked."""
    profile = _build()
    assert profile["properties"]["0x0119"]["value_set"] == "none"
    assert profile["properties"]["0x0100"]["value_set"] == "enum"
    # A code the harvest never covered carries no claim either way.
    snapshot = _snapshot()
    harvest = {"schema": "crsdkpy.property_harvest/1", "properties": []}
    bare = _build(snapshot=snapshot, harvest=harvest)
    assert "value_set" not in bare["properties"]["0x0100"]


# -- what the profile is for ------------------------------------------------
def test_surfaces_are_recorded_as_their_delta_from_the_inventory() -> None:
    profile = _build()
    remote = profile["surfaces"]["remote+memory_card"]
    transfer = profile["surfaces"]["remote_transfer+memory_card"]
    assert remote["absent_codes"] == []
    assert transfer["absent_codes"] == ["0x0581"]
    assert remote["property_count"] == 6
    assert transfer["property_count"] == 5


def test_unnamed_codes_are_listed_and_left_unnamed() -> None:
    profile = _build()
    assert profile["unknown_codes"] == ["0x0581"]
    assert "name" not in profile["properties"]["0x0581"]


def test_measured_capabilities_come_from_evidence_not_the_capture() -> None:
    """The library's own capability fields include optimistic defaults, so the
    profile takes capabilities from the measured file instead."""
    profile = _build()
    assert profile["surfaces"]["remote+memory_card"]["capabilities"] == {
        "live_view": True,
        "postview_configuration": False,
    }
    assert (
        profile["surfaces"]["remote_transfer+memory_card"]["capabilities"][
            "postview_configuration"
        ]
        is True
    )


def test_load_bearing_values_are_recorded_per_surface() -> None:
    profile = _build()
    assert profile["surfaces"]["remote+memory_card"]["reference_values"] == {
        "0x0119": 2
    }


def test_the_profile_declares_the_invariants_it_expects_to_be_checked() -> None:
    profile = _build()
    assert "unknown_codes_are_preserved" in profile["invariants"]
    assert "surfaces_report_properties" in profile["invariants"]


def test_the_result_loads_as_a_conformance_profile() -> None:
    from crsdkpy.conformance import ConformanceProfile

    loaded = ConformanceProfile(_build())
    assert loaded.profile_id == "TEST-BODY_fw1.00"
    assert loaded.unknown_codes == frozenset({0x0581})
    assert 0x0581 not in loaded.codes_for("remote_transfer+memory_card")


def test_a_focus_map_contributes_only_the_mapping_and_position_facts() -> None:
    focus_map = {
        "schema": "crsdkpy.focus_mode_map/1",
        "body": "TEST-BODY",
        "lens": "Sony FE 50mm F1.4 GM",
        "firmware": "1.00",
        "crsdk": "2.02.00",
        "modes": {"MF": {"FocusMode_0x0109": 1}},
        "focus_position": {"setting_0x020E": {"range": {"min": 0, "max": 65535}}},
        "notes": ["FocusMode reports read_only in MF"],
    }
    profile = _build(focus_map=focus_map)
    assert profile["focus_modes"]["modes"]["MF"]["FocusMode_0x0109"] == 1
    position = profile["focus_modes"]["focus_position"]["setting_0x020E"]
    assert position["range"]["max"] == 65535
    # Body identity is not repeated inside the focus block.
    assert "body" not in profile["focus_modes"]
    assert profile["lens"]["model"] == "Sony FE 50mm F1.4 GM"


def test_capabilities_are_not_duplicated_into_the_measured_block() -> None:
    profile = _build()
    assert "capabilities" not in profile.get("measured", {})
    assert profile["measured"]["content"]["ids_contiguous"] is False
