"""Telling a user what is missing, and where CrSDKPy looked for it.

Running against a camera needs the package, a built bridge and host, the Sony
runtime, and ``CrAdapter`` reachable from the host executable's own directory.
When one of those is absent the failure that surfaces without help is an exit
code or a vendor status, so these tests are about each stage saying which one
it is.

None of this loads the vendor SDK or touches a camera.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

import crsdkpy
from crsdkpy.backend.host import HostBackend
from crsdkpy.diagnostics import (
    CRADAPTER_DIRNAME,
    RuntimeStaging,
    describe_runtime_staging,
    diagnose,
    vendor_core_filename,
)


def _stage(directory, *, core=True, adapter=True, adapter_files=1):
    """Build a directory that looks staged to whatever degree is asked."""
    if core:
        with open(os.path.join(directory, vendor_core_filename()), "wb") as handle:
            handle.write(b"not a real library")
    if adapter:
        path = os.path.join(directory, CRADAPTER_DIRNAME)
        os.makedirs(path, exist_ok=True)
        for index in range(adapter_files):
            with open(os.path.join(path, f"adapter{index}.bin"), "wb") as handle:
                handle.write(b"x")
    name = "crsdkpy_host.exe" if sys.platform == "win32" else "crsdkpy_host"
    host = os.path.join(directory, name)
    with open(host, "wb") as handle:
        handle.write(b"not a real binary")
    return host


# -- staging inspection -----------------------------------------------------
def test_a_fully_staged_directory_is_complete(tmp_path) -> None:
    _stage(str(tmp_path))
    staging = describe_runtime_staging(str(tmp_path))
    assert staging.complete
    assert staging.missing == ()
    assert "staged in" in staging.explain()


def test_a_missing_runtime_names_both_pieces(tmp_path) -> None:
    _stage(str(tmp_path), core=False, adapter=False)
    staging = describe_runtime_staging(str(tmp_path))
    assert not staging.complete
    assert vendor_core_filename() in staging.missing
    assert any(CRADAPTER_DIRNAME in item for item in staging.missing)
    explained = staging.explain()
    assert str(tmp_path) in explained
    assert "not distributed with CrSDKPy" in explained


def test_a_missing_adapter_directory_is_distinguished_from_a_missing_runtime(
    tmp_path,
) -> None:
    """The two failures need different fixes, so they read differently."""
    _stage(str(tmp_path), adapter=False)
    staging = describe_runtime_staging(str(tmp_path))
    assert not staging.complete
    assert staging.core_present
    assert vendor_core_filename() not in staging.explain()
    assert CRADAPTER_DIRNAME in staging.explain()


def test_an_empty_adapter_directory_is_not_a_staged_runtime(tmp_path) -> None:
    """Present but empty is the shape a half-finished copy leaves behind."""
    _stage(str(tmp_path), adapter_files=0)
    staging = describe_runtime_staging(str(tmp_path))
    assert not staging.complete
    assert staging.adapter_present
    assert "there but empty" in staging.explain()


def test_the_explanation_says_where_the_adapter_is_resolved_from(tmp_path) -> None:
    """The one thing the vendor's own error never mentions."""
    _stage(str(tmp_path), core=False, adapter=False)
    explained = describe_runtime_staging(str(tmp_path)).explain()
    assert "host executable" in explained


def test_a_directory_that_does_not_exist_is_not_a_crash(tmp_path) -> None:
    staging = describe_runtime_staging(str(tmp_path / "nope"))
    assert not staging.complete
    assert not staging.core_present


def test_no_directory_at_all_says_so() -> None:
    staging = describe_runtime_staging(None)
    assert not staging.complete
    assert "not located" in staging.explain()


# -- the host refuses to launch into a broken staging -----------------------
def test_starting_against_an_unstaged_host_names_what_is_missing(tmp_path) -> None:
    """Checked before launch: afterwards the only evidence is an exit code."""
    host = _stage(str(tmp_path), core=False, adapter=False)
    backend = HostBackend(host_path=host)
    with pytest.raises(crsdkpy.SDKNotFoundError) as excinfo:
        backend.start()
    message = str(excinfo.value)
    assert vendor_core_filename() in message
    assert CRADAPTER_DIRNAME in message
    # It says where it looked, not merely that something went wrong.
    assert str(tmp_path) in message
    assert host in message


def test_the_missing_runtime_error_is_not_confused_with_a_crash(tmp_path) -> None:
    host = _stage(str(tmp_path), core=False, adapter=False)
    backend = HostBackend(host_path=host)
    with pytest.raises(crsdkpy.SDKNotFoundError):
        backend.start()
    # SDKNotFoundError, not BackendError: nothing was started, so nothing died.
    assert issubclass(crsdkpy.SDKNotFoundError, crsdkpy.BackendUnavailableError)


def test_a_supplied_command_skips_the_staging_check() -> None:
    """The test host has no vendor runtime by design and must still run."""
    fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_host.py")
    backend = HostBackend(
        command=[sys.executable, fake], adapter_dir=os.path.dirname(fake)
    )
    backend.start()
    try:
        assert backend.state == "ready"
    finally:
        backend.shutdown()


# -- a host that dies says why ----------------------------------------------
def _dying_host(tmp_path, body: str) -> str:
    script = tmp_path / "dying_host.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(script)


def test_a_host_that_dies_noisily_reports_what_it_said(tmp_path) -> None:
    """stderr used to go to the void, so a loader failure read as an exit code."""
    script = _dying_host(
        tmp_path,
        """
        import sys
        sys.stderr.write("Cr_Core.dll: The specified module could not be found")
        sys.stderr.flush()
        raise SystemExit(7)
        """,
    )
    backend = HostBackend(command=[sys.executable, script], adapter_dir=str(tmp_path))
    with pytest.raises(crsdkpy.BackendError) as excinfo:
        backend.start()
    message = str(excinfo.value)
    assert "Cr_Core.dll" in message
    assert "The specified module could not be found" in message
    assert excinfo.value.backend_code == 7


def test_a_host_that_dies_silently_points_at_the_usual_cause(tmp_path) -> None:
    script = _dying_host(tmp_path, "raise SystemExit(9)")
    backend = HostBackend(command=[sys.executable, script], adapter_dir=str(tmp_path))
    with pytest.raises(crsdkpy.BackendError) as excinfo:
        backend.start()
    message = str(excinfo.value)
    assert "reported nothing" in message
    assert CRADAPTER_DIRNAME in message
    assert excinfo.value.backend_code == 9


def test_a_dead_host_is_never_reported_as_a_clean_close(tmp_path) -> None:
    """A dead host is a failure, not a session that ended tidily."""
    script = _dying_host(tmp_path, "raise SystemExit(4)")
    backend = HostBackend(command=[sys.executable, script], adapter_dir=str(tmp_path))
    with pytest.raises(crsdkpy.BackendError):
        backend.start()
    assert backend.state != "ready"


def test_the_exit_code_is_reported_rather_than_none(tmp_path) -> None:
    """The pipe closes before the status is reapable; waiting is the difference
    between naming the failure and reporting nothing."""
    script = _dying_host(tmp_path, "raise SystemExit(11)")
    backend = HostBackend(command=[sys.executable, script], adapter_dir=str(tmp_path))
    with pytest.raises(crsdkpy.BackendError) as excinfo:
        backend.start()
    assert "exit code 11" in str(excinfo.value)


def test_host_stderr_is_captured_without_deadlocking_a_chatty_host(tmp_path) -> None:
    """A pipe nobody drains blocks the child once its buffer fills."""
    script = _dying_host(
        tmp_path,
        """
        import sys
        sys.stderr.write("noise\\n" * 20000)
        sys.stderr.flush()
        raise SystemExit(5)
        """,
    )
    backend = HostBackend(command=[sys.executable, script], adapter_dir=str(tmp_path))
    with pytest.raises(crsdkpy.BackendError) as excinfo:
        backend.start()
    # Reported, and bounded rather than unloaded whole into the message.
    assert "noise" in str(excinfo.value)
    assert len(str(excinfo.value)) < 6000


def test_a_host_that_writes_junk_to_stdout_is_a_protocol_error(tmp_path) -> None:
    """stdout carries the protocol and nothing else."""
    script = _dying_host(
        tmp_path,
        """
        import sys
        sys.stdout.buffer.write(b"hello, this is not a frame at all")
        sys.stdout.buffer.flush()
        import time
        time.sleep(5)
        """,
    )
    backend = HostBackend(command=[sys.executable, script], adapter_dir=str(tmp_path))
    with pytest.raises(crsdkpy.BackendError, match="malformed frame"):
        backend.start()


# -- the overall picture ----------------------------------------------------
def test_diagnose_reports_every_stage_without_loading_the_vendor_sdk() -> None:
    result = diagnose()
    assert result.package_version == crsdkpy.__version__
    assert result.simulator_ready
    report = result.report()
    for stage in ("package", "simulator", "native bridge", "host executable"):
        assert stage in report


def test_diagnose_is_runnable_as_a_module() -> None:
    """The shortest thing to run when nothing starts, and it must be quiet.

    Importing the package pulls this module in, so running the submodule form
    warns about a double import. The package entry point avoids that, and a
    diagnostic command that opens with a RuntimeWarning is not one.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "crsdkpy"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "CrSDKPy" in completed.stdout
    assert "RuntimeWarning" not in completed.stderr


def test_an_unbuilt_host_does_not_report_the_runtime_as_missing() -> None:
    """Nowhere to look is not the same as looked and found nothing."""
    from crsdkpy.diagnostics import Diagnosis

    report = Diagnosis(package_version="x", host_path=None, staging=None).report()
    assert "not checked yet" in report
    assert "MISSING" not in report


def test_an_unready_installation_says_the_simulator_still_works() -> None:
    unstaged = RuntimeStaging(directory="/nowhere")
    assert not unstaged.complete
    # The report distinguishes the two readiness questions.
    result = diagnose()
    assert result.simulator_ready
    assert isinstance(result.hardware_ready, bool)
