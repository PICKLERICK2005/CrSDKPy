"""Comparing a body against a conformance profile.

The profile is sanitized evidence from one measured body. Comparing another
body against it must be useful *for that other body* -- which means most
differences are information, not failure. These tests pin that distinction in
both directions: the handful of things that are genuinely required, and the
much larger set of things that are allowed to differ and must be reported
rather than rejected.

The one thing deliberately absent is any assertion that two cameras have the
same number of properties. One body reported different counts in different
control modes, so a count can never be a health check.
"""

from __future__ import annotations

import copy
import json

import pytest

import crsdkpy
from crsdkpy.conformance import (
    ConformanceProfile,
    FindingKind,
    Observation,
    PropertyRecord,
    Severity,
    compare,
    load_profile,
    profile_names,
    reference_profile,
)

SURFACES = (
    "remote+memory_card",
    "remote+host_and_memory_card",
    "remote_transfer+memory_card",
    "remote_transfer+host_and_memory_card",
)


def _profile(**overrides) -> ConformanceProfile:
    """A small two-surface profile, so a test reads as one idea."""
    document = {
        "schema": "crsdkpy.conformance_profile/1",
        "profile_id": "TEST-BODY",
        "body": {"model": "TEST-BODY", "firmware": "1.00"},
        "invariants": [
            "surfaces_report_properties",
            "unknown_codes_are_preserved",
            "access_states_are_known",
            "codes_are_non_negative",
        ],
        "unknown_codes": ["0x0581"],
        "properties": {
            "0x0100": {
                "name": "FNumber",
                "access": "read_write",
                "data_type": 8194,
                "value_set": "enum",
                "values": [280, 400],
            },
            "0x020E": {
                "name": "FocusPositionSetting",
                "access": "read_write",
                "data_type": 16386,
                "value_set": "range",
                "values": [0, 65535, 1],
            },
            "0x0581": {"access": "read_only", "data_type": 8195, "value_set": "none"},
            "0x0119": {
                "name": "StillImageStoreDestination",
                "access": "read_write",
                "value_set": "none",
            },
        },
        "surfaces": {
            "remote+memory_card": {
                "control_mode": "remote",
                "still_destination": "memory_card",
                "property_count": 4,
                "absent_codes": [],
                "capabilities": {"live_view": True, "content_index": False},
                "reference_values": {"0x0119": 2},
            },
            "remote_transfer+memory_card": {
                "control_mode": "remote_transfer",
                "still_destination": "memory_card",
                "property_count": 3,
                "absent_codes": ["0x0581"],
                "capabilities": {"live_view": False, "content_index": True},
                "reference_values": {"0x0119": 2},
            },
        },
    }
    document.update(overrides)
    return ConformanceProfile(document)


def _property(code, **fields) -> dict:
    entry = {
        "code": code,
        "name": fields.get("name"),
        "access": fields.get("access", "read_write"),
        "value": fields.get("value", 0),
    }
    if "value_set" in fields:
        entry["value_set"] = fields["value_set"]
        entry["values"] = fields.get("values", [])
    if "data_type" in fields:
        entry["data_type"] = fields["data_type"]
    return entry


def _snapshot(surface_properties, capabilities=None, unknown=None) -> dict:
    """A capture document in the shape the characterization tool writes."""
    surfaces = []
    for key, properties in surface_properties.items():
        mode, _, destination = key.partition("+")
        surfaces.append(
            {
                "control_mode": mode,
                "still_destination": destination,
                "properties": properties,
                "capabilities": (capabilities or {}).get(key, {}),
                "unknown_codes": (unknown or {}).get(key, []),
            }
        )
    return {"schema": "crsdkpy.camera_snapshot/1", "surfaces": surfaces}


def _matching_snapshot() -> dict:
    remote = [
        _property("0x0100", name="FNumber", value_set="enum", values=[280, 400],
                  data_type=8194),
        _property("0x020E", name="FocusPositionSetting", value_set="range",
                  values=[0, 65535, 1], data_type=16386),
        _property("0x0581", access="read_only", value_set="none", data_type=8195),
        _property("0x0119", name="StillImageStoreDestination", value=2),
    ]
    # Copied, not shared: a test that changes one surface must not silently
    # change the other, which is the whole point of comparing them separately.
    transfer = copy.deepcopy([p for p in remote if p["code"] != "0x0581"])
    return _snapshot(
        {"remote+memory_card": remote, "remote_transfer+memory_card": transfer},
        capabilities={
            "remote+memory_card": {"live_view": True, "content_index": False},
            "remote_transfer+memory_card": {"live_view": False, "content_index": True},
        },
        unknown={"remote+memory_card": ["0x0581"]},
    )


# -- the shipped profile ----------------------------------------------------
def test_the_reference_profile_loads_and_is_the_measured_body() -> None:
    profile = reference_profile()
    assert profile.body["model"] == "ILME-FX3A"
    assert profile.body["firmware"] == "2.02"
    assert profile.sdk["version"] == "2.02.00"
    assert profile.lens["model"] == "Sony FE 50mm F1.4 GM"
    assert "ILME-FX3A" in profile_names()


def test_the_reference_profile_carries_no_identifying_data() -> None:
    """The tracked artifact is the thing that could leak, so it is checked."""
    import os

    path = os.path.join(
        os.path.dirname(os.path.abspath(crsdkpy.__file__)),
        "profiles",
        "ILME-FX3A.json",
    )
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    lowered = text.lower()
    for leaked in ("dcim", "c:/", "c:\\", "/users/", "/home/", "serialnumber\": \""):
        assert leaked not in lowered, f"{leaked!r} present in the tracked profile"
    # The serial's code and type stay; only the number is gone.
    profile = reference_profile()
    serial = profile.properties[0x079F]
    assert serial.name == "BodySerialNumber"
    assert serial.value is None


def test_the_reference_profile_preserves_the_four_measured_surfaces() -> None:
    profile = reference_profile()
    assert set(profile.surfaces) == set(SURFACES)
    assert profile.surfaces["remote+memory_card"].property_count == 394
    assert profile.surfaces["remote+host_and_memory_card"].property_count == 394
    assert profile.surfaces["remote_transfer+memory_card"].property_count == 392
    assert (
        profile.surfaces["remote_transfer+host_and_memory_card"].property_count == 392
    )


def test_the_reference_profile_keeps_the_unnamed_codes_numerically() -> None:
    """Two codes the vendor enumeration does not name. No name is invented."""
    profile = reference_profile()
    assert profile.unknown_codes == frozenset({0x0581, 0x0582})
    for code in (0x0581, 0x0582):
        record = profile.properties[code]
        assert record.name is None
        assert record.access == "read_only"
    # Present in Remote, absent in RemoteTransfer -- the measured difference.
    assert 0x0581 in profile.codes_for("remote+memory_card")
    assert 0x0581 not in profile.codes_for("remote_transfer+memory_card")


def test_the_reference_profile_records_constraints_and_identity_strings() -> None:
    profile = reference_profile()
    focus = profile.properties[0x020E]
    assert focus.value_set == "range"
    assert focus.values == (0, 65535, 1)
    assert profile.properties[0x0109].value_set == "enum"
    assert profile.properties[0x07B2].value == "ILME-FX3A"
    assert profile.properties[0x0765].value == "FE 50mm F1.4 GM"


def test_the_reference_profile_records_measured_not_optimistic_capabilities() -> None:
    """Postview configuration was refused in Remote and accepted in transfer.

    The library's own capability field is optimistic until something refuses,
    so the profile has to carry the measurement rather than that default.
    """
    profile = reference_profile()
    assert profile.surfaces["remote+memory_card"].capabilities[
        "postview_configuration"
    ] is False
    assert profile.surfaces["remote_transfer+memory_card"].capabilities[
        "postview_configuration"
    ] is True
    # Delivery follows the destination, independently of configuration.
    assert profile.surfaces["remote+memory_card"].capabilities[
        "postview_delivery"
    ] is False
    assert profile.surfaces["remote+host_and_memory_card"].capabilities[
        "postview_delivery"
    ] is True


# -- a matching body --------------------------------------------------------
def test_a_body_matching_the_profile_produces_no_findings() -> None:
    report = compare(_profile(), Observation.from_snapshot(_matching_snapshot()))
    assert report.conformant
    assert list(report) == []


def test_comparison_is_deterministic() -> None:
    """Two runs over the same inputs must produce the same report, in order."""
    profile = _profile()
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"].append(
        _property("0x7F01", access="read_only")
    )
    first = compare(profile, Observation.from_snapshot(snapshot))
    second = compare(profile, Observation.from_snapshot(snapshot))
    assert [str(f) for f in first] == [str(f) for f in second]
    assert first.counts() == second.counts()


def test_findings_are_ordered_by_surface_then_code() -> None:
    profile = _profile()
    snapshot = _matching_snapshot()
    for code in ("0x7F05", "0x7F01", "0x7F03"):
        snapshot["surfaces"][0]["properties"].append(_property(code))
    report = compare(profile, Observation.from_snapshot(snapshot))
    codes = [f.code for f in report if f.surface == "remote+memory_card" and f.code]
    assert codes == sorted(codes)


# -- what must never differ -------------------------------------------------
def test_a_surface_that_reports_nothing_is_an_invariant_violation() -> None:
    snapshot = _snapshot({"remote+memory_card": []})
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert not report.conformant
    violated = report.of_kind(FindingKind.INVARIANT_VIOLATED)
    assert any("reported no properties" in f.summary for f in violated)


def test_dropping_an_unknown_code_is_an_invariant_violation() -> None:
    """The one failure that makes a characterization actively misleading."""
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"] = [
        p for p in snapshot["surfaces"][0]["properties"] if p["code"] != "0x0581"
    ]
    # Still claimed as unknown, but no longer in the inventory.
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert not report.conformant
    assert any(
        "must be preserved, not dropped" in f.summary
        for f in report.of_kind(FindingKind.INVARIANT_VIOLATED)
    )


def test_an_unmodelled_access_state_is_an_invariant_violation() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"][0]["access"] = "sideways"
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert not report.conformant
    assert report.errors[0].observed == "sideways"


def test_a_profile_declaring_an_unimplemented_invariant_says_so() -> None:
    profile = _profile(invariants=["surfaces_report_properties", "invented_rule"])
    report = compare(profile, Observation.from_snapshot(_matching_snapshot()))
    assert not report.conformant
    assert any("does not implement" in f.summary for f in report.errors)


# -- what is allowed to differ ----------------------------------------------
def test_the_property_count_is_never_an_invariant() -> None:
    """A different count is information. It is the single most tempting thing
    to assert and the one hardware proved wrong."""
    snapshot = _matching_snapshot()
    for code in ("0x7F01", "0x7F02", "0x7F03", "0x7F04"):
        snapshot["surfaces"][0]["properties"].append(_property(code))
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    counts = [
        f for f in report.of_kind(FindingKind.BODY_SPECIFIC)
        if "property count differs" in f.summary
    ]
    assert len(counts) == 1
    assert counts[0].severity == Severity.INFO
    assert counts[0].expected == 4
    assert counts[0].observed == 8


def test_a_body_exposing_extra_named_properties_is_normal() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"].append(
        _property("0x0300", name="SomethingNewer")
    )
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    extra = report.of_kind(FindingKind.NEW_PROPERTY)
    assert [f.code for f in extra] == [0x0300]
    assert extra[0].severity == Severity.INFO


def test_an_extra_unnamed_vendor_code_is_surfaced_not_rejected() -> None:
    """A future firmware adding a code this release cannot name is expected."""
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"].append(_property("0x7FFE"))
    snapshot["surfaces"][0]["unknown_codes"].append("0x7FFE")
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    unknown = report.of_kind(FindingKind.UNKNOWN_PROPERTY)
    assert [f.code for f in unknown] == [0x7FFE]
    assert unknown[0].name is None


def test_a_missing_property_is_reported_as_a_warning_not_an_error() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"] = [
        p for p in snapshot["surfaces"][0]["properties"] if p["code"] != "0x0100"
    ]
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    missing = report.of_kind(FindingKind.MISSING_PROPERTY)
    assert [f.code for f in missing] == [0x0100]
    assert missing[0].severity == Severity.WARNING
    assert missing[0].name == "FNumber"


def test_an_access_change_is_information_because_it_tracks_camera_state() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"][0]["access"] = "read_only"
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    changed = report.of_kind(FindingKind.ACCESS_CHANGED)
    assert [f.code for f in changed] == [0x0100]
    assert changed[0].severity == Severity.INFO
    assert (changed[0].expected, changed[0].observed) == ("read_write", "read_only")


def test_an_allowed_list_change_is_information() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"][0]["values"] = [280, 400, 560]
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    changed = report.of_kind(FindingKind.VALUES_CHANGED)
    assert [f.code for f in changed] == [0x0100]
    assert changed[0].expected == "enum of 2"
    assert changed[0].observed == "enum of 3"


def test_a_range_change_is_information_and_says_what_moved() -> None:
    snapshot = _matching_snapshot()
    focus = next(
        p for p in snapshot["surfaces"][0]["properties"] if p["code"] == "0x020E"
    )
    focus["values"] = [0, 1000, 2]
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    changed = report.of_kind(FindingKind.VALUES_CHANGED)
    assert changed[0].expected == "range 0..65535 step 1"
    assert changed[0].observed == "range 0..1000 step 2"


def test_a_data_type_change_is_a_warning_because_decoding_depends_on_it() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"][0]["data_type"] = 8195
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    changed = report.of_kind(FindingKind.TYPE_CHANGED)
    assert changed[0].severity == Severity.WARNING
    assert (changed[0].expected, changed[0].observed) == ("0x2002", "0x2003")


def test_a_load_bearing_reference_value_differing_is_a_warning() -> None:
    """The destination echo says what the surface *is*, not what state it is in."""
    snapshot = _matching_snapshot()
    destination = next(
        p for p in snapshot["surfaces"][0]["properties"] if p["code"] == "0x0119"
    )
    destination["value"] = 3
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    differed = report.of_kind(FindingKind.REFERENCE_VALUE_DIFFERS)
    assert [f.code for f in differed] == [0x0119]
    assert differed[0].severity == Severity.WARNING
    assert (differed[0].expected, differed[0].observed) == (2, 3)


def test_an_ordinary_value_differing_produces_nothing() -> None:
    """Current values move with session state and are not a comparison target."""
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"][0]["value"] = 99999
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert list(report) == []


def test_a_capability_difference_is_information() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["capabilities"]["live_view"] = False
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    differed = report.of_kind(FindingKind.CAPABILITY_DIFFERS)
    assert differed[0].severity == Severity.INFO
    assert "live_view" in differed[0].summary


# -- four surfaces ----------------------------------------------------------
def test_each_surface_is_compared_separately() -> None:
    """A difference in one surface must not be attributed to another."""
    snapshot = _matching_snapshot()
    snapshot["surfaces"][1]["properties"].append(_property("0x0300", name="Extra"))
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    extra = report.of_kind(FindingKind.NEW_PROPERTY)
    assert [(f.surface, f.code) for f in extra] == [
        ("remote_transfer+memory_card", 0x0300)
    ]


def test_the_measured_surface_difference_is_respected_per_surface() -> None:
    """0x0581 belongs to Remote only, so its absence in transfer is not missing."""
    report = compare(_profile(), Observation.from_snapshot(_matching_snapshot()))
    assert not report.of_kind(FindingKind.MISSING_PROPERTY)


def test_a_transfer_surface_reporting_the_remote_only_code_is_extra_not_wrong() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][1]["properties"].append(
        _property("0x0581", access="read_only")
    )
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    found = report.of_kind(FindingKind.UNKNOWN_PROPERTY)
    assert [(f.surface, f.code) for f in found] == [
        ("remote_transfer+memory_card", 0x0581)
    ]


def test_a_surface_that_was_not_sampled_is_reported_not_failed() -> None:
    snapshot = _snapshot(
        {"remote+memory_card": _matching_snapshot()["surfaces"][0]["properties"]},
        unknown={"remote+memory_card": ["0x0581"]},
    )
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    skipped = report.of_kind(FindingKind.SURFACE_NOT_SAMPLED)
    assert [f.surface for f in skipped] == ["remote_transfer+memory_card"]


def test_a_surface_the_profile_does_not_know_is_reported_not_failed() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"].append(
        {
            "control_mode": "contents_transfer",
            "still_destination": "memory_card",
            "properties": [_property("0x0100", name="FNumber")],
            "capabilities": {},
            "unknown_codes": [],
        }
    )
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert report.conformant
    new = report.of_kind(FindingKind.SURFACE_NEW)
    assert [f.surface for f in new] == ["contents_transfer+memory_card"]


def test_a_surface_that_failed_to_open_is_skipped_not_invented() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"].append(
        {
            "control_mode": "remote_transfer",
            "still_destination": "host_and_memory_card",
            "error": "CameraConnectionError: nope",
        }
    )
    observation = Observation.from_snapshot(snapshot)
    assert "remote_transfer+host_and_memory_card" not in observation.surfaces


def test_the_comparison_can_be_limited_to_named_surfaces() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][1]["properties"].append(_property("0x0300", name="Extra"))
    report = compare(
        _profile(),
        Observation.from_snapshot(snapshot),
        surfaces=["remote+memory_card"],
    )
    assert list(report) == []


# -- capture compatibility --------------------------------------------------
def test_an_older_capture_without_constraints_does_not_invent_differences() -> None:
    """A capture that never recorded value sets says nothing about them.

    Reading absence as "advertises nothing" would manufacture a finding for
    every constrained property at once, which is exactly what the reference
    capture would have produced.
    """
    snapshot = _matching_snapshot()
    for prop in snapshot["surfaces"][0]["properties"]:
        prop.pop("value_set", None)
        prop.pop("values", None)
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert not report.of_kind(FindingKind.VALUES_CHANGED)


def test_the_older_allowed_values_and_range_fields_are_understood() -> None:
    snapshot = _matching_snapshot()
    for prop in snapshot["surfaces"][0]["properties"]:
        prop.pop("value_set", None)
        prop.pop("values", None)
    snapshot["surfaces"][0]["properties"][0]["allowed_values"] = [280, 400]
    focus = next(
        p for p in snapshot["surfaces"][0]["properties"] if p["code"] == "0x020E"
    )
    focus["range"] = {"minimum": 0, "maximum": 65535, "step": 1}
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    assert not report.of_kind(FindingKind.VALUES_CHANGED)


def test_the_reference_capture_compares_cleanly_against_its_own_profile() -> None:
    """An end-to-end check with no camera: profile in, observation in, and the
    only differences are the ones the profile deliberately corrects."""
    profile = reference_profile()
    observation = Observation(
        surfaces={
            key: {
                code: PropertyRecord(
                    code=code,
                    name=record.name,
                    access=record.access,
                )
                for code, record in profile.properties.items()
                if code in profile.codes_for(key)
            }
            for key in SURFACES
        },
        capabilities={
            key: dict(profile.surfaces[key].capabilities) for key in SURFACES
        },
        unknown_codes={key: profile.unknown_codes & profile.codes_for(key)
                       for key in SURFACES},
    )
    report = compare(profile, observation)
    assert report.conformant
    assert list(report) == []


# -- report surface ---------------------------------------------------------
def test_a_report_summarises_without_claiming_equality() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"].append(_property("0x0300", name="Extra"))
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    summary = report.summary()
    assert "conformant" in summary
    assert "new_property=1" in summary
    assert len(report) == 2  # the extra property, and the count moving with it


def test_findings_render_with_their_location() -> None:
    snapshot = _matching_snapshot()
    snapshot["surfaces"][0]["properties"][0]["access"] = "read_only"
    report = compare(_profile(), Observation.from_snapshot(snapshot))
    rendered = str(report.of_kind(FindingKind.ACCESS_CHANGED)[0])
    assert "remote+memory_card" in rendered
    assert "0x0100 FNumber" in rendered
    assert "[info]" in rendered


def test_an_unusable_document_is_refused_clearly() -> None:
    with pytest.raises(ValueError, match="not a conformance profile"):
        ConformanceProfile({"schema": "something.else/1"})


def test_a_missing_profile_names_what_is_available() -> None:
    with pytest.raises(FileNotFoundError, match="ILME-FX3A"):
        load_profile("NoSuchBody")


def test_a_profile_can_be_loaded_from_an_explicit_path(tmp_path) -> None:
    path = tmp_path / "custom.json"
    path.write_text(
        json.dumps(
            {
                "schema": "crsdkpy.conformance_profile/1",
                "profile_id": "CUSTOM",
                "properties": {},
                "surfaces": {},
            }
        ),
        encoding="utf-8",
    )
    assert load_profile(str(path)).profile_id == "CUSTOM"


# -- the simulator's relationship to the profile ----------------------------
def test_the_simulator_derives_its_measured_facts_from_the_profile() -> None:
    """One home for each measured fact.

    The FX3A simulator profile used to restate the identity and the per-mode
    capabilities by hand. They now come from the tracked profile, so there is
    one place a measurement can be wrong.
    """
    from crsdkpy.simulator.profiles import FX3A

    profile = reference_profile()
    assert FX3A.model == profile.body["model"]
    assert FX3A.firmware == profile.body["firmware"]
    assert FX3A.adapter == profile.body["adapter"]
    assert FX3A.usb_pid == int(profile.body["usb_pid"], 16)

    remote = FX3A.modes[crsdkpy.SessionMode.REMOTE]
    transfer = FX3A.modes[crsdkpy.SessionMode.REMOTE_TRANSFER]
    measured_remote = profile.surfaces["remote+memory_card"].capabilities
    measured_transfer = profile.surfaces["remote_transfer+memory_card"].capabilities
    assert remote.live_view is measured_remote["live_view"]
    assert transfer.content_index is measured_transfer["content_index"]
    assert remote.postview_configuration is measured_remote["postview_configuration"]
    assert transfer.postview_configuration is (
        measured_transfer["postview_configuration"]
    )


def test_the_simulator_takes_its_unnamed_codes_from_the_measurement() -> None:
    from crsdkpy.simulator.profiles import FX3A

    profile = reference_profile()
    remote = FX3A.modes[crsdkpy.SessionMode.REMOTE]
    assert set(remote.extra_property_codes) == profile.unknown_codes
    assert FX3A.modes[crsdkpy.SessionMode.REMOTE_TRANSFER].extra_property_codes == ()


def test_the_simulator_is_not_an_emulator_of_the_reference_body() -> None:
    """Deriving the measured facts must not drag in several hundred properties.

    The simulator exists to exercise CrSDKPy deterministically, not to
    reproduce one camera. Its property set stays small and synthetic.
    """
    from crsdkpy.simulator.profiles import FX3A

    profile = reference_profile()
    exposed = set(FX3A.property_codes_for(crsdkpy.SessionMode.REMOTE))
    assert len(exposed) < len(profile.properties) / 4
    # It does model the one measured structural difference between modes.
    transfer = set(FX3A.property_codes_for(crsdkpy.SessionMode.REMOTE_TRANSFER))
    assert exposed - transfer == set(profile.unknown_codes)


def test_abnormal_behaviour_stays_in_scenarios_not_in_the_profile() -> None:
    """Deliberate misbehaviour is a scenario, and a profile is evidence.

    Mixing them would make the reference body look like it does things it was
    never observed doing.
    """
    from crsdkpy.simulator import SCENARIOS

    profile_text = json.dumps(reference_profile().document).lower()
    for marker in (
        "capture_without_exposure", "busy", "reconnect_after_ms",
        "content_id_step", "focus_stuck", "transfer_fail", "scenario",
    ):
        assert marker not in profile_text
    # And the scenarios that cover the abnormal cases still exist.
    for scenario in (
        "capture_without_exposure", "delayed_content", "non_contiguous_content",
        "af_no_lock", "af_sticky", "zoom_not_operable", "framing_refused",
    ):
        assert scenario in SCENARIOS
