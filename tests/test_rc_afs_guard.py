"""A diagnostic guard must only remove permission, never force AF or retry."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_afs_gate_blocks_mf_overrides_and_repeated_attempts():
    spec = importlib.util.spec_from_file_location(
        "rc_guard", Path(__file__).parents[1] / "tools/run_sptfs_afs_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attempts = []

    def original(*args, **kwargs):
        attempts.append(kwargs)
        return "identified"

    guard = module.guarded_selection(original, lambda: "blocked")
    assert guard(None, None, False, automatic=True) == "blocked"
    assert guard(None, None, True, automatic=False) == "blocked"
    assert guard(None, None, True, automatic=True, dwell_ms=200) == "blocked"
    assert attempts == []
    assert guard(None, None, True, automatic=True) == "identified"
    assert guard(None, None, True, automatic=True) == "blocked"
    assert attempts == [{"automatic": True, "dwell_ms": 35}]


def test_afs_gate_exception_does_not_authorize_retry():
    spec = importlib.util.spec_from_file_location(
        "rc_guard", Path(__file__).parents[1] / "tools/run_sptfs_afs_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def original(*args, **kwargs):
        raise RuntimeError("ambiguous")

    guard = module.guarded_selection(original, lambda: "blocked")
    with pytest.raises(RuntimeError, match="ambiguous"):
        guard(None, None, True, automatic=True)
    assert guard(None, None, True, automatic=True) == "blocked"


def test_controller_interruption_closes_session_and_records_evidence(
    monkeypatch, tmp_path,
):
    spec = importlib.util.spec_from_file_location(
        "rc_guard", Path(__file__).parents[1] / "tools/run_sptfs_afs_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Adapter:
        def connect(self):
            self._session = SimpleNamespace(closed=False)
            self._sdk = SimpleNamespace(
                started=True,
                backend=SimpleNamespace(_process=SimpleNamespace(poll=lambda: 0)),
            )

        def close(self):
            self._session.closed = True
            self._sdk.started = False

        def _capture_selected(self, *args, **kwargs):
            raise AssertionError("test must never request capture")

    original_connect = Adapter.connect
    monkeypatch.setitem(sys.modules, "sptfs.camera_contract", SimpleNamespace(
        CaptureResult=object, ExposureState=SimpleNamespace(NOT_EXPOSED=0)))
    monkeypatch.setitem(sys.modules, "sptfs.drivers.sony_crsdkpy", SimpleNamespace(
        SonyCrSDKPyAdapter=Adapter))

    def interrupted(*args, **kwargs):
        Adapter().connect()
        raise KeyboardInterrupt

    monkeypatch.setattr(module.runpy, "run_path", interrupted)
    monkeypatch.setattr(sys, "argv", ["guard", "--sptfs-root", str(tmp_path),
                                     "--closure-dir", str(tmp_path)])
    # main inserts the source directory; monkeypatch restores the path afterward.
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(KeyboardInterrupt):
        module.main()
    record = json.loads(next(tmp_path.glob("closure-*.json")).read_text())[0]
    assert record == {"session_closed": True, "sdk_stopped": True,
                      "host_returncode": 0}
    assert Adapter.connect is original_connect
