"""Read-only A7 IV AF-S gate. Contains no capture or setting-write path."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-root", type=Path, required=True)
    parser.add_argument("--sptfs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("remote", "remote_transfer"),
                        default="remote_transfer")
    args = parser.parse_args()
    stage = args.stage_root.resolve()
    if args.output.exists():
        parser.error("output already exists; preserve previous evidence")
    import crsdkpy
    from crsdkpy import SDK
    from crsdkpy.backend import _ipc
    from crsdkpy.backend.host import HostBackend

    assert crsdkpy.__version__ == "0.1.0b4"
    assert Path(crsdkpy.__file__).is_relative_to(stage / "venv")
    manifest = json.loads((stage / "stage-manifest.json").read_text())
    for name, expected in manifest["binaries"].items():
        actual = hashlib.sha256((stage / "native" / name).read_bytes()).hexdigest()
        assert actual == expected
    for name, value in {
        "CRSDKPY_HOST": stage / "native/crsdkpy_host.exe",
        "CRSDKPY_BRIDGE": stage / "native/crsdkpy_bridge.dll",
        "CRSDKPY_SAVE_DIR": stage / "host-save",
    }.items():
        os.environ[name] = str(value)
    spec = importlib.util.spec_from_file_location(
        "measured_sony_focus", args.sptfs_root / "sptfs/drivers/sony_focus.py"
    )
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    backend = HostBackend()
    original = backend._call_ex
    allowed = {
        _ipc.OP_INIT, _ipc.OP_SHUTDOWN, _ipc.OP_ENUMERATE, _ipc.OP_CAMERA_AT,
        _ipc.OP_OPEN_SESSION, _ipc.OP_CLOSE_SESSION, _ipc.OP_CONNECTION_STATE,
        _ipc.OP_POLL_EVENTS, _ipc.OP_LIST_PROPERTIES, _ipc.OP_GET_PROPERTY,
        _ipc.OP_PROPERTY_STRING, _ipc.OP_PROPERTY_VALUES,
    }
    evidence = {"artifact_hashes": manifest["binaries"], "mode": args.mode,
                "operations": [],
                "properties": {}, "afs_preflight_passed": False,
                "initialization_exception": "existing session SetSaveInfo only"}

    def checked(op, **kwargs):
        if op not in allowed:
            raise AssertionError(f"non-read-only IPC operation refused: {op}")
        result = original(op, **kwargs)
        evidence["operations"].append({"op": op, "status": result[0].status})
        return result

    backend._call_ex = checked
    sdk = SDK(backend=backend, autostart=False)
    session = None
    try:
        sdk.start()
        cameras = sdk.discover()
        assert len(cameras) == 1, "require exactly one camera"
        camera = cameras[0]
        assert camera.model == "ILCE-7M4"
        session = camera.open(args.mode)  # destination=None: do not write it
        evidence["destination_before"] = session.destination.value
        for code in (0x0751, 0x0765, 0x075F, 0x0109, 0x0179, 0x025E,
                     0x0210, 0x020E, 0x0001, 0x0500):
            prop = session.properties.get(code)
            evidence["properties"][f"0x{code:04X}"] = {
                "value": prop.value, "type": prop.value_type.value,
                "access": prop.access.value,
                "allowed_values": list(prop.allowed_values),
                "range": asdict(prop.value_range) if prop.value_range else None,
            }
        try:
            autofocus, decision = resolver.resolve_focus(session, camera)
            evidence["resolver"] = decision
            evidence["afs_preflight_passed"] = (
                autofocus and decision["observed_mode"] == "AF-S"
                and decision["selected_policy"] == "gated_af"
            )
        except resolver.FocusPolicyError as exc:
            evidence["resolver"] = exc.evidence
            evidence["error"] = str(exc)
        evidence["destination_after"] = session.destination.value
        assert evidence["destination_before"] == evidence["destination_after"]
    finally:
        sdk.close()
        evidence["session_closed"] = session.closed if session else None
        evidence["sdk_stopped"] = not sdk.started
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["afs_preflight_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
