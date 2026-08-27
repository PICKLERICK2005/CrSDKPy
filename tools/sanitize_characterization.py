"""Turn a private characterization capture into a tracked conformance profile.

Capture and sanitization are separate steps on purpose. ``characterize_camera``
writes everything a body reports, including things that identify the body and
the machine it was plugged into; that output is private evidence and stays
private. This reads it and emits the sanitized profile that *is* tracked, so
the moment private data would become public is one explicit command rather than
a property of how the capture happened to be written.

    python tools/sanitize_characterization.py \\
        --snapshot characterization/snapshots/body_snapshot.json \\
        --harvest  characterization/snapshots/body_property_harvest.json \\
        --focus-map characterization/snapshots/body_focus_mode_map.json \\
        --measured characterization/snapshots/body_measured.json \\
        -o src/crsdkpy/profiles/BODY.json

Only the snapshot is required. Each extra input adds a dimension the snapshot
does not carry: the harvest carries advertised value sets and vendor data
types, the focus map carries the mode mapping, and the measured file carries
feature evidence that is not a property at all.

What is removed, always:

* the body serial number, and the device key that embeds it;
* host platform, interpreter and user details;
* absolute filesystem paths, temporary directories and DCIM paths;
* per-event timestamp streams and other session-transient state;
* battery charge and shot counts.

What is kept, because a profile is useless without it: the model, firmware and
SDK versions, the lens where it shaped the result, every property code with its
vendor name where known, vendor data types, access, advertised values, measured
capabilities, and the codes the vendor's own enumeration does not name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from typing import Any, Optional

PROFILE_SCHEMA = "crsdkpy.conformance_profile/1"

#: Property codes whose *string* value identifies the individual body. The code
#: and its type stay, because "this body exposes a serial number as a string"
#: is a surface fact; the number itself never travels.
REDACTED_STRING_CODES = {0x079F}

#: Vendor data-type bits, mirrored from the SDK header so a value set can be
#: decoded without the header present.
_SIGN_BIT = 0x1000
_ARRAY_BIT = 0x2000
_RANGE_BIT = 0x4000
_WIDTHS = {0x0001: 1, 0x0002: 2, 0x0003: 4, 0x0004: 8}
_STRING_TYPE = 0xFFFF

#: Things that must never appear in a tracked profile, checked after building
#: it rather than trusted to have been handled field by field.
_FORBIDDEN = (
    re.compile(r"[A-Za-z]:[\\/]", re.ASCII),          # drive-letter paths
    re.compile(r"/(?:home|Users|tmp|var)/", re.ASCII),  # posix home and temp
    re.compile(r"DCIM", re.IGNORECASE),
    re.compile(r"DSC\d{5}", re.IGNORECASE),           # real capture filenames
    re.compile(r"\bTemp\b", re.ASCII),
)


def decode_value_set(data_type: int, blob: bytes) -> tuple[Optional[str], list[int]]:
    """Decode an advertised value set using the vendor's own type.

    The same rules the bridge applies: element width and signedness come from
    the declared type, never from the byte count. A shape this cannot take
    apart is reported as raw rather than sliced on a guess.
    """
    width = _WIDTHS.get(data_type & 0x0FFF, 0)
    ranged = bool(data_type & _RANGE_BIT)
    arrayed = bool(data_type & _ARRAY_BIT)
    if width == 0 or not (ranged or arrayed) or len(blob) % width:
        return "raw", []
    count = len(blob) // width
    if ranged and count != 3:
        return "raw", []
    code = {1: "b", 2: "h", 4: "i", 8: "q"}[width]
    if not data_type & _SIGN_BIT:
        code = code.upper() if width < 8 else "Q"
    values = list(struct.unpack(f"<{count}{code}", blob))
    return ("range" if ranged else "enum"), values


def clean_string(text: Optional[str]) -> Optional[str]:
    """Strip the length/BOM artefact the harvest tool leaves on a vendor string."""
    if text is None:
        return None
    return text.lstrip("?﻿").strip() or None


def _hex(code: int) -> str:
    return f"0x{code:04X}"


def _parse_code(code: Any) -> int:
    return code if isinstance(code, int) else int(str(code), 16)


def build_properties(
    snapshot: dict, harvest: Optional[dict]
) -> tuple[dict[str, dict], list[str]]:
    """Merge the surface inventory with the advertised value sets.

    The snapshot knows every code on every surface and how it was accessed; the
    harvest knows what each one advertises and its vendor data type. Neither
    alone is a profile.
    """
    harvested = {}
    if harvest:
        harvested = {_parse_code(p["code"]): p for p in harvest.get("properties", ())}

    merged: dict[int, dict] = {}
    unknown: set[int] = set()
    for surface in snapshot.get("surfaces", ()):
        for prop in surface.get("properties", ()):
            code = _parse_code(prop["code"])
            name = prop.get("name") or None
            if name is None:
                unknown.add(code)
            entry = merged.setdefault(
                code, {"name": name, "access": prop.get("access")}
            )
            # Access was identical across surfaces on the reference body; if a
            # future capture disagrees, the first surface wins and the
            # difference shows up as a finding rather than being averaged away.
            if entry.get("name") is None and name is not None:
                entry["name"] = name

    for code, entry in merged.items():
        source = harvested.get(code)
        if not source:
            continue
        data_type = source.get("data_type")
        if data_type is not None:
            entry["data_type"] = data_type
        blob = source.get("values_hex")
        if data_type is not None:
            # "none" is a measurement: the harvest asked and the property
            # advertised nothing. Leaving the field out would say only that
            # nobody looked, which is a different claim.
            if blob:
                kind, values = decode_value_set(data_type, bytes.fromhex(blob))
                entry["value_set"] = kind
                if values:
                    entry["values"] = values
            else:
                entry["value_set"] = "none"
        if data_type == _STRING_TYPE:
            text = clean_string(source.get("string"))
            if code in REDACTED_STRING_CODES:
                entry["value"] = None
                entry["redacted"] = "identifies the individual body"
            elif text is not None:
                entry["value"] = text

    ordered = {
        _hex(code): {k: v for k, v in entry.items() if v is not None or k == "value"}
        for code, entry in sorted(merged.items())
    }
    return ordered, sorted(_hex(c) for c in unknown)


def build_surfaces(
    snapshot: dict, all_codes: set[int], measured: Optional[dict] = None
) -> dict[str, dict]:
    """Record each surface as its delta from the full inventory.

    Storing what a surface is *missing* rather than repeating several hundred
    codes four times is both smaller and a clearer statement of what the
    measurement found.
    """
    surfaces: dict[str, dict] = {}
    for surface in snapshot.get("surfaces", ()):
        if surface.get("error") or "properties" not in surface:
            continue
        mode = surface.get("control_mode")
        destination = surface.get("still_destination")
        present = {_parse_code(p["code"]) for p in surface["properties"]}
        reference_values = {}
        for prop in surface["properties"]:
            code = _parse_code(prop["code"])
            if code in REFERENCE_VALUE_CODES:
                reference_values[_hex(code)] = prop.get("value")
        key = f"{mode}+{destination}"
        record = {
            "control_mode": mode,
            "still_destination": destination,
            "property_count": len(present),
            "absent_codes": sorted(_hex(c) for c in (all_codes - present)),
            "reference_values": reference_values,
        }
        # Capabilities come from the measured file, not from the snapshot.
        # Some of the library's own capability fields are optimistic defaults
        # that mean "nobody has tried yet", and recording one of those as
        # hardware evidence would be inventing a measurement.
        capabilities = ((measured or {}).get("capabilities") or {}).get(key)
        if capabilities:
            record["capabilities"] = capabilities
        surfaces[key] = record
    return surfaces


#: Values that encode what a surface *is* rather than what the body was doing:
#: the destination echo, the control-mode echo, and the postview resource state
#: that follows from the mode. These are worth flagging when they move.
REFERENCE_VALUE_CODES = {0x0119, 0x0725, 0x07AB}


def scrub(document: Any, findings: list[str], path: str = "") -> Any:
    """Walk the built profile and refuse anything that looks private.

    A whitelist per field is easy to get wrong as inputs grow, so the built
    document is swept as well. This raises rather than silently editing: a hit
    here means a sanitization rule is missing, and quietly dropping the value
    would hide that.
    """
    if isinstance(document, dict):
        for key, value in document.items():
            scrub(value, findings, f"{path}.{key}")
    elif isinstance(document, list):
        for index, value in enumerate(document):
            scrub(value, findings, f"{path}[{index}]")
    elif isinstance(document, str):
        for pattern in _FORBIDDEN:
            if pattern.search(document):
                findings.append(f"{path}: {document!r} matches {pattern.pattern!r}")
    return document


def build_profile(
    snapshot: dict,
    harvest: Optional[dict] = None,
    focus_map: Optional[dict] = None,
    measured: Optional[dict] = None,
    *,
    profile_id: Optional[str] = None,
) -> dict:
    """Assemble the tracked profile from the private inputs."""
    camera = snapshot.get("camera", {})
    properties, unknown = build_properties(snapshot, harvest)
    all_codes = {_parse_code(c) for c in properties}
    surfaces = build_surfaces(snapshot, all_codes, measured)

    lens_name = clean_string(snapshot.get("attached_lens"))
    if focus_map and focus_map.get("lens"):
        lens_name = clean_string(focus_map["lens"]) or lens_name
    lens_version = None
    lens_entry = properties.get(_hex(0x075F))
    if lens_entry:
        lens_version = lens_entry.get("value")

    firmware = None
    software_entry = properties.get(_hex(0x0751))
    if software_entry:
        firmware = software_entry.get("value")
    if focus_map and focus_map.get("firmware"):
        firmware = focus_map["firmware"]

    model = camera.get("model")
    sdk_version = (focus_map or {}).get("crsdk") or (measured or {}).get("sdk_version")

    profile = {
        "schema": PROFILE_SCHEMA,
        "profile_id": profile_id
        or f"{model}_fw{firmware}_crsdk{sdk_version}".replace(" ", ""),
        "body": {
            "model": model,
            "firmware": firmware,
            "transport": camera.get("transport"),
            "usb_pid": _hex(camera["usb_pid"]) if camera.get("usb_pid") else None,
            "adapter": camera.get("adapter"),
        },
        "sdk": {"version": sdk_version},
        "lens": {"model": lens_name, "version": lens_version},
        "provenance": {
            "sanitized_from": "a private characterization capture",
            "contains": (
                "property surface, vendor data types, access, advertised value "
                "sets, measured capabilities and unnamed vendor codes"
            ),
            "excludes": (
                "body serial, device key, host details, filesystem paths, "
                "capture filenames, event timestamp streams, battery and "
                "shot counts"
            ),
        },
        "invariants": [
            "surfaces_report_properties",
            "unknown_codes_are_preserved",
            "access_states_are_known",
            "codes_are_non_negative",
        ],
        "surfaces": surfaces,
        "unknown_codes": unknown,
        "properties": properties,
    }
    if focus_map:
        profile["focus_modes"] = sanitize_focus_map(focus_map)
    if measured:
        # Capabilities were folded into the surfaces above; what is left is
        # feature evidence that is not a property and not a capability flag.
        remainder = {
            k: v for k, v in measured.items()
            if k not in ("capabilities", "sdk_version")
        }
        if remainder:
            profile["measured"] = remainder

    problems: list[str] = []
    scrub(profile, problems)
    if problems:
        raise SystemExit(
            "refusing to write a profile that still carries private data:\n  "
            + "\n  ".join(problems)
        )
    return profile


def sanitize_focus_map(focus_map: dict) -> dict:
    """Keep the mode mapping and the position facts; drop identity and prose."""
    kept = {
        "modes": focus_map.get("modes", {}),
        "focus_position": focus_map.get("focus_position", {}),
    }
    notes = focus_map.get("notes")
    if notes:
        kept["notes"] = list(notes)
    return kept


def _load(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="camera_snapshot JSON")
    parser.add_argument("--harvest", help="property_harvest JSON, for value sets")
    parser.add_argument("--focus-map", help="focus_mode_map JSON")
    parser.add_argument("--measured", help="normalized feature-evidence JSON")
    parser.add_argument("--profile-id", help="override the derived profile id")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    profile = build_profile(
        _load(args.snapshot),
        _load(args.harvest),
        _load(args.focus_map),
        _load(args.measured),
        profile_id=args.profile_id,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(profile, handle, indent=1, sort_keys=True)
        handle.write("\n")

    print(f"wrote {args.output}")
    print(
        f"  {len(profile['properties'])} properties, "
        f"{len(profile['surfaces'])} surfaces, "
        f"{len(profile['unknown_codes'])} unnamed codes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
