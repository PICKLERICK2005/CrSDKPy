"""Software-only installed-wheel gate. Never discovers or opens a real camera."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-root", type=Path, required=True)
    args = parser.parse_args()
    stage = args.stage_root.resolve()
    import crsdkpy
    from crsdkpy.backend import _cabi, _ipc
    from crsdkpy.backend.host import HostBackend
    from crsdkpy.backend.native import decode_event
    from crsdkpy.events import NativeEventInfo
    from crsdkpy.session import Session

    assert crsdkpy.__version__ == "0.1.0b4"
    assert Path(crsdkpy.__file__).is_relative_to(Path(sys.prefix))
    assert Path(sys.prefix).is_relative_to(stage)
    os.environ["CRSDKPY_HOST"] = str(stage / "native/crsdkpy_host.exe")
    os.environ["CRSDKPY_BRIDGE"] = str(stage / "native/crsdkpy_bridge.dll")
    os.environ["CRSDKPY_SAVE_DIR"] = str(stage / "host-save")
    result = {
        "sdk_version": crsdkpy.__version__,
        "package_path": crsdkpy.__file__,
        "physical_camera_sessions_opened": 0,
    }

    # Start the actual staged host, but refuse all operations beyond library
    # initialization/shutdown. HELLO/BYE framing is handled by the backend.
    host = HostBackend()
    original = host._call_ex
    operations = []

    def checked(op, **kwargs):
        if op not in (_ipc.OP_INIT, _ipc.OP_SHUTDOWN):
            raise AssertionError("real-camera operation forbidden in software gate")
        operations.append(op)
        return original(op, **kwargs)

    host._call_ex = checked
    try:
        host.start()
        ack = host.handshake
        assert (ack.abi_major, ack.abi_minor) == (1, 6)
        assert (ack.protocol_major, ack.protocol_minor) == (1, 3)
        result.update(abi=[1, 6], ipc=[1, 3])
    finally:
        host.shutdown()
    result.update(real_host_state=host.state, real_host_operations=operations)
    lib = _cabi.load_bridge(str(stage / "native/crsdkpy_bridge.dll"))
    assert lib.crsdkpy_abi_version() == 0x10006

    # Metadata and connection/disconnection use an explicitly fake peer.
    # No physical discovery/property command is permitted by the guard above.
    fixture = Path(__file__).resolve().parents[1] / "tests/fake_host.py"
    fake = HostBackend(
        command=[sys.executable, str(fixture)], adapter_dir=str(fixture.parent)
    )
    with crsdkpy.SDK(backend=fake) as sdk:
        camera = sdk.discover()[0]
        session = camera.open("remote_transfer")
        try:
            assert session.properties.get(0x0751).value == "9.99"
            assert session.properties.get(0x0109).allowed_values == (1, 2, 3)
            r = session.properties.get(0x020E).value_range
            assert (r.minimum, r.maximum, r.step) == (0, 65535, 1)
        finally:
            session.close()
        assert session.closed and not camera.sessions
    result["fake_peer_metadata_and_cleanup"] = "passed"

    raw = _cabi.EventStruct(
        kind=_cabi.EVENT_FOCUS,
        reserved=7,
        timestamp_ms=42,
        code=0x1234,
        i0=2,
        i1=_cabi.FOCUS_SRC_WARNING,
    )
    event = decode_event(raw, timestamp_ms=999)
    assert event.timestamp_ms == 999 and event.native.timestamp_ms == 42
    assert event.native.sequence == 7 and event.native.raw_code == 0x1234
    assert Session._after_boundary(event, NativeEventInfo(40, 6))
    assert not Session._after_boundary(event, NativeEventInfo(50, 8))
    result["provenance_and_boundaries"] = "passed"
    (stage / "evidence").mkdir(exist_ok=True)
    output = stage / "evidence/software-gate.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
