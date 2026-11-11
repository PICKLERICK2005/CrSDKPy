"""RC-only AF-S guard; launch does not authorize or initiate a capture.

Uses the existing per-shot resolver and capture sequence unchanged. Refuses
S2-only dispatch and more than one gated invocation per process. No source
files or installed packages are patched.
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from uuid import uuid4


def guarded_selection(original, blocked):
    used = False

    def selected(adapter, session, use_af, *, automatic=False, dwell_ms=35):
        nonlocal used
        if used or not use_af or not automatic or dwell_ms != 35:
            return blocked()
        used = True  # A rejection/exception never permits an automatic retry.
        return original(adapter, session, use_af, automatic=True, dwell_ms=35)

    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sptfs-root", type=Path, required=True)
    parser.add_argument("--closure-dir", type=Path, required=True)
    args, controller_args = parser.parse_known_args()
    source = args.sptfs_root.resolve()
    sys.path.insert(0, str(source))
    from sptfs.camera_contract import CaptureResult, ExposureState
    from sptfs.drivers.sony_crsdkpy import SonyCrSDKPyAdapter

    original = SonyCrSDKPyAdapter._capture_selected
    original_connect = SonyCrSDKPyAdapter.connect
    adapters = []

    def connect(adapter):
        if adapter not in adapters:
            adapters.append(adapter)
        return original_connect(adapter)

    SonyCrSDKPyAdapter.connect = connect
    SonyCrSDKPyAdapter._capture_selected = guarded_selection(
        original,
        lambda: CaptureResult(
            ExposureState.NOT_EXPOSED,
            reason="RC AF-S gate blocked: require auto/gated AF-S, 35 ms, "
                   "and a fresh individually authorized process",
        ),
    )
    sys.argv = [str(source / "controller.py"), *controller_args]
    try:
        runpy.run_path(str(source / "controller.py"), run_name="__main__")
    finally:
        SonyCrSDKPyAdapter._capture_selected = original
        SonyCrSDKPyAdapter.connect = original_connect
        closure = []
        for adapter in adapters:
            session, sdk = adapter._session, adapter._sdk
            process = getattr(getattr(sdk, "backend", None), "_process", None)
            record = {}
            try:
                adapter.close()  # explicit close on controller exit, never capture
                record.update(session_closed=session.closed if session else None,
                              sdk_stopped=not sdk.started if sdk else None,
                              host_returncode=process.poll() if process else None)
            except Exception as exc:
                record["close_error"] = str(exc)
            closure.append(record)
        args.closure_dir.mkdir(parents=True, exist_ok=True)
        path = args.closure_dir / f"closure-{uuid4().hex}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(closure, handle, indent=2)
        print(f"RC closure evidence: {path}")


if __name__ == "__main__":
    main()
