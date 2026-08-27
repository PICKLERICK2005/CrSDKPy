"""What is installed, what is missing, and where CrSDKPy looked.

Running against a camera needs four separate things to be true, and when one
of them is not, the failure a user actually sees is usually from something
further down the chain: a helper process that exits with a number, or a vendor
status code. This answers the question directly instead.

    python -m crsdkpy.diagnostics

The four things:

1. the Python package -- always present if this imports at all;
2. the first-party native bridge and host, which the user builds;
3. the Sony runtime library, which the user obtains from Sony;
4. ``CrAdapter``, which the vendor SDK resolves **against the host
   executable's directory** rather than the working directory.

Point 4 is the one that catches people. Staging the SDK somewhere sensible and
pointing at it is not enough: the transport adapters have to be reachable from
where the host binary lives, and nothing in the vendor's own error text says
so.

Nothing here loads the vendor SDK or touches a camera. It is a filesystem
inspection and it is safe to run when everything is broken.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "CRADAPTER_DIRNAME",
    "RuntimeStaging",
    "describe_runtime_staging",
    "diagnose",
    "vendor_core_filename",
]

#: The vendor's transport-adapter directory, by the name the SDK expects.
CRADAPTER_DIRNAME = "CrAdapter"


def vendor_core_filename() -> str:
    """The Sony runtime library's filename on this platform."""
    if sys.platform == "win32":
        return "Cr_Core.dll"
    if sys.platform == "darwin":
        return "libCr_Core.dylib"
    return "libCr_Core.so"


@dataclass(frozen=True)
class RuntimeStaging:
    """Whether the vendor runtime is where the host will look for it."""

    directory: Optional[str]
    core_present: bool = False
    adapter_present: bool = False
    adapter_entries: int = 0

    @property
    def complete(self) -> bool:
        return self.core_present and self.adapter_present and self.adapter_entries > 0

    @property
    def missing(self) -> tuple[str, ...]:
        """What is not there, named the way a user would look for it."""
        absent = []
        if not self.core_present:
            absent.append(vendor_core_filename())
        if not self.adapter_present:
            absent.append(f"{CRADAPTER_DIRNAME}{os.sep}")
        elif self.adapter_entries == 0:
            absent.append(f"{CRADAPTER_DIRNAME}{os.sep} is there but empty")
        return tuple(absent)

    def explain(self) -> str:
        """A sentence a user can act on, naming the directory inspected."""
        if self.directory is None:
            return "there is no directory to inspect: the host was not located"
        if self.complete:
            return f"the Sony runtime is staged in {self.directory}"
        return (
            f"the Sony runtime is not staged in {self.directory}: "
            + " and ".join(self.missing)
            + ". The vendor SDK resolves "
            f"{CRADAPTER_DIRNAME} against the host executable's own directory, "
            "so copying the runtime beside the host is what makes it reachable. "
            "The Sony Camera Remote SDK is not distributed with CrSDKPy."
        )


def describe_runtime_staging(directory: Optional[str]) -> RuntimeStaging:
    """Inspect one directory for the vendor runtime. Never raises."""
    if not directory or not os.path.isdir(directory):
        return RuntimeStaging(directory=directory)
    adapter = os.path.join(directory, CRADAPTER_DIRNAME)
    entries = 0
    adapter_present = os.path.isdir(adapter)
    if adapter_present:
        try:
            entries = len(os.listdir(adapter))
        except OSError:  # pragma: no cover - unreadable directory
            entries = 0
    return RuntimeStaging(
        directory=directory,
        core_present=os.path.isfile(os.path.join(directory, vendor_core_filename())),
        adapter_present=adapter_present,
        adapter_entries=entries,
    )


@dataclass
class Diagnosis:
    """The whole picture, in the order a user has to satisfy it."""

    package_version: str = ""
    bridge_path: Optional[str] = None
    host_path: Optional[str] = None
    staging: Optional[RuntimeStaging] = None
    profiles: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def simulator_ready(self) -> bool:
        """The simulator needs nothing but the package."""
        return bool(self.package_version)

    @property
    def hardware_ready(self) -> bool:
        return bool(
            self.host_path and self.staging is not None and self.staging.complete
        )

    def report(self) -> str:
        lines = [f"CrSDKPy {self.package_version}", ""]
        lines.append(f"  package          installed ({len(self.profiles)} profile(s))")
        lines.append(
            "  simulator        ready"
            if self.simulator_ready
            else "  simulator        UNAVAILABLE"
        )
        lines.append(
            f"  native bridge    {self.bridge_path or 'not built or not found'}"
        )
        lines.append(f"  host executable  {self.host_path or 'not built or not found'}")
        if self.staging is None:
            lines.append("  Sony runtime     not checked: no host to check beside")
        elif self.staging.complete:
            lines.append(f"  Sony runtime     staged in {self.staging.directory}")
        else:
            lines.append(f"  Sony runtime     MISSING: {self.staging.explain()}")
        lines.append("")
        lines.append(
            "  hardware backend ready" if self.hardware_ready
            else "  hardware backend not ready"
        )
        if self.notes:
            lines.append("")
            lines.extend(f"  note: {note}" for note in self.notes)
        return os.linesep.join(lines)


def diagnose() -> Diagnosis:
    """Inspect the installation without loading the vendor SDK."""
    from . import __version__, conformance
    from .backend._cabi import LIBRARY_ENV_VAR, find_library
    from .backend.host import HOST_ENV_VAR, find_host_executable

    bridge = find_library()
    host = find_host_executable()
    staging = describe_runtime_staging(os.path.dirname(host) if host else None)

    result = Diagnosis(
        package_version=__version__,
        bridge_path=bridge,
        host_path=host,
        staging=staging,
        profiles=conformance.profile_names(),
    )
    if not host:
        result.notes.append(
            "build the host and bridge with cmake -S native -B native/build "
            "-DCRSDK_ROOT=/path/to/CrSDK/RemoteCli, or point "
            f"{HOST_ENV_VAR} at an existing one"
        )
    if not bridge:
        result.notes.append(
            f"the in-process backend additionally needs {LIBRARY_ENV_VAR} or a "
            "bridge in the build directory"
        )
    if host and staging is not None and not staging.complete:
        result.notes.append(staging.explain())
    if result.simulator_ready and not result.hardware_ready:
        result.notes.append(
            "the simulator runs with none of the above: "
            "crsdkpy.SDK(backend='simulator', profile='fx3a')"
        )
    return result


def main() -> int:
    print(diagnose().report())
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
