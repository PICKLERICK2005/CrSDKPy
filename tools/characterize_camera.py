"""Capture a machine-readable capability snapshot from a connected camera.

Read-only by default. The point is that a body we do not physically have can be
characterized by whoever does have it: they run this, send the JSON, and we can
reason about what their camera exposes without owning it.

    python tools/characterize_camera.py -o snapshot.json
    python tools/characterize_camera.py --modes remote --lens-info

Property names are resolved from a vendor-header-derived table when one is
supplied with --names; codes with no name stay as codes, because a wrong name
would be worse than none. Codes absent from the vendor enumeration are recorded
as unknown rather than dropped.

Nothing here formats media, deletes content, or writes a property unless a
mutating probe is explicitly asked for.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from typing import Any, Optional

import crsdkpy

#: The four surfaces worth sampling. Control mode and still destination each
#: change what a session exposes, and hardware showed the combinations are not
#: interchangeable, so they are enumerated rather than assumed.
SURFACES = [
    ("remote", "memory_card"),
    ("remote", "host_and_memory_card"),
    ("remote_transfer", "memory_card"),
    ("remote_transfer", "host_and_memory_card"),
]

DESTINATIONS = {
    "memory_card": crsdkpy.StillDestination.MEMORY_CARD,
    "host_and_memory_card": crsdkpy.StillDestination.HOST_AND_MEMORY_CARD,
}

CODE_DESTINATION = 0x0119

#: Families worth grouping in the summary. Substring match against the resolved
#: name, so an unrecognised code simply does not appear in a family.
FAMILIES = {
    "lens": ("Lens",),
    "focus": ("Focus", "FollowFocus", "PreAF"),
    "zoom": ("Zoom",),
    "framing": ("Eframing",),
    "interval": ("IntervalRec", "Interval_Rec"),
    "drive": ("DriveMode", "SelfTimer", "Bracket", "ContShooting", "Continuous"),
    "exposure": (
        "IsoSensitivity", "ShutterSpeed", "FNumber", "ExposureBiasCompensation",
        "ExposureProgramMode", "MeteringMode", "ExposureCtrlType", "GainControl",
    ),
    "white_balance": ("WhiteBalance", "ColorTemp", "ColorTuning", "Tint"),
    "image": (
        "PictureProfile", "CreativeLook", "ImageQuality", "FileType",
        "AspectRatio", "ImageSize", "StillImage", "Compression", "RAW",
    ),
    "stabilization": ("Stabilization", "SteadyShot", "Flicker", "ShutterType",
                      "SilentMode", "Crop"),
    "media": ("Media", "Slot", "Storage", "Recording", "Format", "Delete"),
    "transfer": ("Transfer", "Contents", "Download", "SaveInfo", "PostView"),
}


def load_names(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return {int(k, 16): v.replace("CrDeviceProperty_", "") for k, v in raw.items()}


def jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": value[:256].hex(), "length": len(value)}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return repr(value)


def describe_property(prop, names: dict) -> dict:
    code = int(prop.code)
    entry = {
        "code": f"0x{code:04X}",
        "name": names.get(code),
        "value": jsonable(prop.value),
        "value_type": prop.value_type.value,
        "access": prop.access.value,
        "writable": prop.writable,
    }
    if prop.allowed_values:
        # Capped: some enumerations are long and the shape matters more than
        # every member.
        values = [jsonable(v) for v in prop.allowed_values[:256]]
        entry["allowed_values"] = values
        entry["allowed_values_count"] = len(prop.allowed_values)
    if prop.value_range is not None:
        entry["range"] = {
            "minimum": prop.value_range.minimum,
            "maximum": prop.value_range.maximum,
            "step": prop.value_range.step,
        }
    if prop.metadata:
        entry["metadata"] = {k: jsonable(v) for k, v in prop.metadata.items()}
    return entry


def capabilities_of(session) -> dict:
    caps = session.capabilities
    out = {}
    for field in (
        "still_capture", "autofocus_s1", "video", "live_view", "content_index",
        "thumbnail", "screennail", "postview_configuration",
        "postview_delivery", "raw_commands",
    ):
        out[field] = caps.get(field)
    if caps.extra:
        out["extra"] = {k: bool(v) for k, v in caps.extra.items()}
    return out


def group_families(properties: list, names: dict) -> dict:
    families: dict = {}
    for entry in properties:
        name = entry.get("name") or ""
        for family, needles in FAMILIES.items():
            if any(n.lower() in name.lower() for n in needles):
                families.setdefault(family, []).append(entry["code"])
    return families


def sample_surface(camera, mode: str, destination: str, names: dict,
                   open_budget_s: float) -> dict:
    record: dict = {"control_mode": mode, "still_destination": destination}
    started = time.monotonic()
    session = camera.open(mode)
    open_ms = (time.monotonic() - started) * 1000
    record["open_ms"] = round(open_ms, 1)
    if open_ms > open_budget_s * 1000:
        # A slow open is itself evidence, and stacking more work behind it is
        # how a characterization run turns into a hang.
        record["warning"] = (
            f"open took {open_ms / 1000:.1f} s, beyond the {open_budget_s:.0f} s "
            "budget; surface sampled but treat timings with suspicion"
        )
    try:
        wanted = DESTINATIONS[destination]
        if session.destination is not wanted:
            session.set_destination(wanted)
            # The camera takes 100-200 ms to publish the change, so confirm it
            # rather than reading it back immediately.
            deadline = time.monotonic() + 8
            while session.destination is not wanted:
                if time.monotonic() >= deadline:
                    record["destination_error"] = (
                        f"could not confirm {destination}; "
                        f"still {session.destination.value}"
                    )
                    break
                time.sleep(0.15)
        record["destination_confirmed"] = session.destination.value

        for event in session.drain_events(timeout_ms=600):
            record.setdefault("events_during_open", []).append(repr(event))

        snapshot = session.properties.snapshot()
        codes = list(snapshot.codes())
        record["property_count"] = len(codes)

        properties = []
        for code in sorted(int(c) for c in codes):
            try:
                properties.append(
                    describe_property(session.properties.get(code), names)
                )
            except crsdkpy.CrSDKPyError as exc:
                properties.append(
                    {"code": f"0x{code:04X}", "name": names.get(code),
                     "error": f"{type(exc).__name__}: {exc}"}
                )
        record["properties"] = properties
        record["unknown_codes"] = [
            p["code"] for p in properties if p.get("name") is None
        ]
        record["families"] = group_families(properties, names)
        record["capabilities"] = capabilities_of(session)
        record["battery"] = repr(session.battery)
        record["storage"] = [repr(slot) for slot in session.storage]
        record["state"] = session.state.value
        try:
            record["recording_state"] = session.video.state.value
        except crsdkpy.CrSDKPyError as exc:
            record["recording_state"] = f"unavailable: {type(exc).__name__}"
        try:
            info = session.live_view.status()
            record["live_view_status"] = repr(info)
        except crsdkpy.CrSDKPyError as exc:
            record["live_view_status"] = f"unavailable: {type(exc).__name__}"
        if session.capabilities.content_index:
            try:
                items = session.content.since()
                record["content_items"] = len(items)
                if items:
                    newest = items[-1]
                    record["content_newest"] = {
                        "content_id": newest.content_id,
                        "path": getattr(newest, "path", None),
                    }
            except crsdkpy.CrSDKPyError as exc:
                record["content_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Restore the destination before closing so the next surface starts
        # from the same place, and so an interrupted run leaves the card as the
        # destination rather than the host.
        try:
            if session.destination is not crsdkpy.StillDestination.MEMORY_CARD:
                session.set_destination(crsdkpy.StillDestination.MEMORY_CARD)
                deadline = time.monotonic() + 8
                while (
                    session.destination is not crsdkpy.StillDestination.MEMORY_CARD
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.15)
        except crsdkpy.CrSDKPyError as exc:
            record["restore_error"] = f"{type(exc).__name__}: {exc}"
        session.close()
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="camera_snapshot.json")
    parser.add_argument("--names", help="JSON map of hex code -> vendor name")
    parser.add_argument("--backend", default="host")
    parser.add_argument(
        "--modes", nargs="+",
        help="limit to these control modes (default: all four surfaces)",
    )
    parser.add_argument(
        "--lens", default=None,
        help="attached lens, recorded verbatim; capability results depend on it",
    )
    parser.add_argument(
        "--open-budget", type=float, default=30.0,
        help="seconds an open may take before the result is flagged",
    )
    args = parser.parse_args()

    names = load_names(args.names)
    surfaces = [s for s in SURFACES if not args.modes or s[0] in args.modes]

    snapshot: dict = {
        "schema": "crsdkpy.camera_snapshot/1",
        "crsdkpy_version": crsdkpy.__version__,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "attached_lens": args.lens,
        "vendor_names_loaded": len(names),
        "surfaces": [],
    }

    with crsdkpy.SDK(backend=args.backend) as sdk:
        cameras = sdk.discover()
        if not cameras:
            print("no camera found", file=sys.stderr)
            return 1
        camera = cameras[0]
        info = camera.info
        snapshot["camera"] = {
            "model": info.model,
            "transport": info.transport,
            "usb_pid": info.usb_pid,
            "device_key": info.device_key,
            "adapter": getattr(info, "adapter", None),
        }
        print(f"camera: {info.model} {info.transport} pid={info.usb_pid}")
        if args.lens:
            print(f"lens:   {args.lens}")

        for mode, destination in surfaces:
            label = f"{mode} + {destination}"
            print(f"  sampling {label} ...", flush=True)
            try:
                record = sample_surface(
                    camera, mode, destination, names, args.open_budget
                )
                print(
                    f"    {record.get('property_count')} properties, "
                    f"{len(record.get('unknown_codes', []))} unknown, "
                    f"open {record.get('open_ms')} ms"
                )
            except crsdkpy.CrSDKPyError as exc:
                record = {
                    "control_mode": mode,
                    "still_destination": destination,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(f"    FAILED: {type(exc).__name__}: {exc}")
            snapshot["surfaces"].append(record)

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=1, sort_keys=False)
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
