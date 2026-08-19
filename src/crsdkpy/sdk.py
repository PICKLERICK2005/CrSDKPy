"""SDK entry point."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional, Union

from .camera import Camera
from .clock import Clock
from .errors import SDKNotStartedError
from .simulator.scenarios import Scenario, get_scenario

if TYPE_CHECKING:  # pragma: no cover
    from .backend.contract import Backend

__all__ = ["SDK"]


class SDK:
    """Owns the backend and discovers cameras.

    The backend is chosen explicitly:

    ``"simulator"``
        A deterministic in-process model. Needs no camera and no vendor SDK.
    ``"host"``
        A real camera, with the vendor SDK running in a helper process. This
        is the one to use for hardware.
    ``"native"``
        A real camera with the vendor SDK loaded in-process. Lower level and
        mainly useful for diagnosis: it is subject to the vendor's
        adapter-directory constraint, which is the reason the hosted backend
        exists.

    A backend whose bridge or host executable is not built raises an error
    saying how to build it.

    >>> with SDK(backend="simulator", profile="fx3a") as sdk:
    ...     camera = sdk.discover()[0]
    ...     camera.model
    'ILME-FX3A'
    """

    def __init__(
        self,
        backend: Union[str, Backend] = "simulator",
        *,
        profile: Any = "fx3a",
        scenario: Union[str, Scenario, None] = None,
        cameras: Optional[Sequence[Any]] = None,
        clock: Optional[Clock] = None,
        sdk_path: Optional[str] = None,
        autostart: bool = True,
    ) -> None:
        self._backend = self._make_backend(
            backend,
            profile=profile,
            scenario=scenario,
            cameras=cameras,
            clock=clock,
            sdk_path=sdk_path,
        )
        self._started = False
        self._cameras: list[Camera] = []
        if autostart:
            self.start()

    @staticmethod
    def _make_backend(
        backend: Union[str, Backend],
        *,
        profile: Any,
        scenario: Union[str, Scenario, None],
        cameras: Optional[Sequence[Any]],
        clock: Optional[Clock],
        sdk_path: Optional[str],
    ) -> Backend:
        if not isinstance(backend, str):
            return backend
        name = backend.lower()
        if name in ("simulator", "sim", "simulated"):
            from .simulator.backend import SimulatedBackend

            resolved = (
                get_scenario(scenario) if isinstance(scenario, str) else scenario
            )
            return SimulatedBackend(
                profile, scenario=resolved, clock=clock, cameras=cameras
            )
        if name in ("native", "crsdk", "sony"):
            from .backend.native import NativeBackend

            return NativeBackend(sdk_path=sdk_path)
        if name in ("host", "hosted"):
            # Runs the vendor SDK in a helper process. Preferred over "native"
            # wherever the interpreter does not sit beside the vendor runtime.
            from .backend.host import HostBackend

            return HostBackend(sdk_path)
        raise ValueError(
            f"unknown backend {backend!r}; expected 'simulator', 'host' or 'native'"
        )

    # -- lifecycle ---------------------------------------------------------
    @property
    def backend(self) -> Backend:
        return self._backend

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def clock(self) -> Clock:
        """The clock driving every wait.

        With the simulator this is a virtual clock, so tests can advance
        through long vendor latencies instantly.
        """
        return self._backend.clock

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> SDK:
        if not self._started:
            self._backend.start()
            self._started = True
        return self

    def close(self) -> None:
        """Close every camera session and shut the backend down.

        Safe to call more than once.
        """
        if not self._started:
            return
        for camera in self._cameras:
            camera.close_sessions()
        self._cameras.clear()
        self._backend.shutdown()
        self._started = False

    def __enter__(self) -> SDK:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- discovery ---------------------------------------------------------
    def discover(self) -> list[Camera]:
        """Enumerate connected cameras.

        Cameras are identified by an opaque backend key, never by list index,
        so a camera reference stays valid across rediscovery.
        """
        if not self._started:
            raise SDKNotStartedError(
                "SDK.start() must be called before discovering cameras",
                operation="discover",
            )
        found = self._backend.enumerate_cameras()
        existing = {c.device_key: c for c in self._cameras}
        cameras: list[Camera] = []
        for info in found:
            camera = existing.get(info.device_key)
            if camera is None:
                camera = Camera(self._backend, info)
            cameras.append(camera)
        self._cameras = cameras
        return list(cameras)

    def camera_by_key(self, device_key: str) -> Optional[Camera]:
        for camera in self._cameras:
            if camera.device_key == device_key:
                return camera
        return None

    def __repr__(self) -> str:
        state = "started" if self._started else "stopped"
        return f"SDK(backend={self.backend_name!r}, {state})"
