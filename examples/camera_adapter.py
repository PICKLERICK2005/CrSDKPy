"""An integration example: wrapping CrSDKPy behind an application's own shape.

This is not part of the library and not anybody's production driver. It shows
what an application-side adapter needs from CrSDKPy, which is a short list, and
demonstrates that nothing Sony-specific has to appear in it: no S1 or S2, no
command enumerations, no control-mode internals, no vendor property codes, no
native handles, no adapter paths, no IPC.

The only place a vendor concept legitimately shows up is a control mode chosen
at connect time, because that genuinely changes what a session can do and no
abstraction can hide a decision the camera makes you take.

Run it against the simulator, which needs no camera and no vendor SDK::

    python examples/camera_adapter.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import crsdkpy


@dataclass(frozen=True)
class Shot:
    """What an application usually wants back from a capture."""

    ok: bool
    #: Encoded preview bytes, when one could be obtained.
    image: Optional[bytes] = None
    width: Optional[int] = None
    height: Optional[int] = None
    #: Durable filename on the camera, when the session can resolve one.
    filename: Optional[str] = None
    #: Milliseconds from requesting the capture to the exposure.
    latency_ms: Optional[int] = None
    reason: Optional[str] = None


class SonyCameraAdapter:
    """A small application-facing wrapper over one Sony body.

    Deliberately narrow. Everything it does is expressed in terms of what the
    session reports it can do, so the same code drives a body with live view
    and one without, and neither is special-cased by model.
    """

    def __init__(self, backend: Any = "simulator", *, mode: str = "remote") -> None:
        self._backend = backend
        self._mode = mode
        self._sdk: Optional[crsdkpy.SDK] = None
        self._session: Optional[crsdkpy.Session] = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self, *, prefer_postview: bool = False) -> bool:
        """Open the first camera found. Returns whether one was opened."""
        self._sdk = crsdkpy.SDK(backend=self._backend)
        self._sdk.start()
        cameras = self._sdk.discover()
        if not cameras:
            self.disconnect()
            return False

        camera = cameras[0]
        # Postview needs the host among the still destinations. Ask for it
        # only when wanted, and only when the body offers it.
        destination = None
        if prefer_postview and camera.capabilities.supports_destination(
            crsdkpy.StillDestination.HOST_AND_MEMORY_CARD
        ):
            destination = crsdkpy.StillDestination.HOST_AND_MEMORY_CARD
        self._session = camera.open(self._mode, destination=destination)
        return True

    def disconnect(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._sdk is not None:
            self._sdk.close()
            self._sdk = None

    def __enter__(self) -> SonyCameraAdapter:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()

    @property
    def session(self) -> crsdkpy.Session:
        if self._session is None:
            raise RuntimeError("not connected")
        return self._session

    # -- what this body can do ---------------------------------------------
    def capabilities(self) -> dict:
        """A plain dictionary an application can log or branch on."""
        caps = self.session.capabilities
        return {name: caps.get(name) for name in caps.names()}

    # -- capture -----------------------------------------------------------
    def capture(self) -> Shot:
        """Release the shutter without autofocus.

        Correct for a manual-focus rig. On a body in an autofocus mode this
        may legitimately produce no exposure, which is reported rather than
        assumed away.
        """
        return self._finish(self.session.capture())

    def autofocus_and_capture(self) -> Shot:
        """Focus first and release only if focus was confirmed.

        Falls back to a plain release on a body with no separate half-press
        stage, because that is a property of the camera and not an error.
        """
        session = self.session
        if not session.capabilities.autofocus_s1:
            return self.capture()
        try:
            return self._finish(session.autofocus_and_capture())
        except crsdkpy.AutofocusFailedError as exc:
            return Shot(ok=False, reason=str(exc))

    def _finish(self, capture: crsdkpy.Capture) -> Shot:
        if not capture.exposed:
            return Shot(ok=False, reason=capture.failure or "no exposure")
        preview = self.preview(capture)
        return Shot(
            ok=True,
            image=preview.data if preview else None,
            width=preview.width if preview else None,
            height=preview.height if preview else None,
            filename=capture.content.filename if capture.content else None,
            latency_ms=capture.exposure_latency_ms,
        )

    # -- previews ----------------------------------------------------------
    def preview(self, capture: crsdkpy.Capture) -> Optional[crsdkpy.Preview]:
        """Best exact-still preview this session can provide, or ``None``.

        Order of preference is a policy decision, made here rather than in the
        library: postview is full resolution and needs no content lookup, a
        screennail is small and needs one, a thumbnail is the last resort.
        """
        caps = self.session.capabilities
        wanted = []
        if caps.postview_delivery:
            wanted.append(crsdkpy.PreviewKind.POSTVIEW)
        if caps.screennail:
            wanted.append(crsdkpy.PreviewKind.SCREENNAIL)
        if caps.thumbnail:
            wanted.append(crsdkpy.PreviewKind.THUMBNAIL)

        for kind in wanted:
            try:
                return capture.preview(kind)
            except (
                crsdkpy.UnsupportedOperationError,
                crsdkpy.OperationTimeoutError,
                crsdkpy.CameraConnectionError,
            ):
                continue  # try the next form rather than failing the capture
        return None

    # -- live view ---------------------------------------------------------
    def live_view(self, *, limit: int = 1):
        """Yield live-view frames, or nothing when the session has no stream.

        A live-view frame is never the captured still, whatever its timing.
        """
        if not self.session.capabilities.live_view:
            return
        yield from self.session.live_view.frames(limit=limit)

    # -- status ------------------------------------------------------------
    def battery(self) -> crsdkpy.BatteryStatus:
        return self.session.battery

    def storage(self) -> tuple:
        return tuple(self.session.storage)


def main() -> None:
    camera = SonyCameraAdapter("simulator")
    # Asking for postview costs nothing on a body that cannot do it: the
    # request is only made when the camera says the destination exists.
    if not camera.connect(prefer_postview=True):
        print("no camera found")
        return
    try:
        caps = camera.capabilities()
        print("capabilities:", ", ".join(sorted(n for n, on in caps.items() if on)))
        print("battery     :", camera.battery())
        for slot in camera.storage():
            print("storage     :", slot)

        for frame in camera.live_view(limit=1):
            print("live view   :", frame)

        shot = camera.autofocus_and_capture()
        print("shot        :", shot.ok, shot.filename, f"{shot.latency_ms} ms")
        if shot.image:
            print(
                "preview     :",
                len(shot.image),
                "bytes",
                f"{shot.width}x{shot.height}",
            )
        elif shot.ok:
            print("preview     : none available in this mode")
    finally:
        camera.disconnect()


if __name__ == "__main__":
    main()
