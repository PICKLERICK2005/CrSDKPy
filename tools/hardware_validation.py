"""Hardware validation suite.

Runs the gates that can only be answered by a real camera, in priority order,
using the smallest number of exposures that still proves each claim. Everything
here goes through the public API: if a gate needs private access to pass, the
public API is the thing that is wrong.

    python tools/hardware_validation.py                 # everything
    python tools/hardware_validation.py --stages A B    # a subset
    python tools/hardware_validation.py --list

Stages, in the order they should be run:

    R  regressions          no exposures
    A  content association  2 exposures   RemoteTransfer
    B  postview             1 exposure    Remote + host destination
    C  live view            (shares B's exposure and session)
    D  video                1 short recording
    M  manual-focus capture 1 exposure    needs the lens switched to MF
    N  autofocus no-lock    0 exposures   needs the lens covered

Safety: nothing here formats media, deletes content, updates firmware, or
touches focus position or AF area. Every temporary change is restored in a
finally block, and the restored state is verified rather than assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from typing import Optional

import crsdkpy
from crsdkpy._jpeg import jpeg_dimensions, looks_complete

# Vendor codes, used only to assert the camera is in the state a gate needs.
# Ordinary application code never needs these; see docs/INTEGRATION_CONTRACT.md.
CODE_ISO = 0x0104
CODE_FOCUS_MODE = 0x0109
CODE_FOCUS_INDICATION = 0x0707
CODE_CAMERA_CAUTION = 0x078B
CODE_SYSTEM_CAUTION = 0x078C
FOCUS_MODE_MF = 0x0001
FOCUS_MODE_AF_S = 0x0002

CARD = crsdkpy.StillDestination.MEMORY_CARD
HOST_AND_CARD = crsdkpy.StillDestination.HOST_AND_MEMORY_CARD
THUMBNAIL = crsdkpy.PreviewKind.THUMBNAIL
SCREENNAIL = crsdkpy.PreviewKind.SCREENNAIL
POSTVIEW = crsdkpy.PreviewKind.POSTVIEW

RESULTS: list[tuple[str, str, str]] = []


def record(stage: str, outcome: str, detail: str = "") -> None:
    RESULTS.append((stage, outcome, detail))
    print(f"  [{outcome}] {stage}{': ' + detail if detail else ''}")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def ask(prompt: str) -> None:
    """Pause for a physical action. The only interactive part of the suite."""
    print(f"\n>>> {prompt}")
    input("    press Enter when ready: ")


def check_clean(session: crsdkpy.Session, stage: str) -> None:
    snapshot = session.properties.snapshot()
    camera = snapshot.value_of(CODE_CAMERA_CAUTION)
    system = snapshot.value_of(CODE_SYSTEM_CAUTION)
    if camera == 1 and system == 1:
        record(stage, "ok", "cautions clear")
    else:
        record(stage, "FAIL", f"cautions camera=0x{camera:X} system=0x{system:X}")


def describe(preview: crsdkpy.Preview) -> str:
    dimensions = jpeg_dimensions(preview.data)
    return (
        f"{preview.byte_length} bytes "
        f"{dimensions[0]}x{dimensions[1]} sha={digest(preview.data)}"
    )


def validate_preview(
    stage: str, preview: crsdkpy.Preview, expect_content: Optional[int]
) -> bool:
    """Structural validity, not byte equality.

    Repeated transfers of one still are not byte-identical on the reference
    body, so identity is checked through the content association the camera
    reports, never through a hash of the image.
    """
    problems = []
    if not looks_complete(preview.data):
        problems.append("not a whole JPEG")
    dimensions = jpeg_dimensions(preview.data)
    if dimensions is None:
        problems.append("no readable frame header")
    elif dimensions != (preview.width, preview.height):
        problems.append(f"geometry disagrees: {dimensions} vs reported")
    if not preview.is_exact_still:
        problems.append("not marked as an exact still")
    if expect_content is not None and preview.content_id != expect_content:
        problems.append(
            f"belongs to content {preview.content_id}, expected {expect_content}"
        )
    if problems:
        record(stage, "FAIL", "; ".join(problems))
        return False
    record(stage, "ok", describe(preview))
    return True


# -- R: regressions, no exposures -------------------------------------------
def stage_regressions(sdk: crsdkpy.SDK) -> None:
    print("\n== R: regressions ==")
    cameras = sdk.discover()
    if not cameras:
        record("R.discovery", "FAIL", "no camera found")
        raise SystemExit(1)
    camera = cameras[0]
    record(
        "R.discovery",
        "ok",
        f"{camera.info.model} {camera.info.transport} pid={camera.info.usb_pid}",
    )

    with camera.open("remote") as session:
        snapshot = session.properties.snapshot()
        # Deliberately not an exact count: it is a live figure that varies by
        # control mode, and asserting it turns a normal change into a failure.
        record("R.remote.properties", "ok", f"{len(snapshot)} properties")
        check_clean(session, "R.remote.cautions")

        battery = session.battery
        record("R.battery", "ok" if battery.known else "FAIL", repr(battery))
        slots = session.storage
        record("R.storage", "ok" if slots else "FAIL", ", ".join(map(repr, slots)))

        before = session.properties.get(CODE_ISO).value
        session.properties.set(CODE_ISO, 125)
        time.sleep(0.4)
        mid = session.properties.get(CODE_ISO).value
        session.properties.set(CODE_ISO, before)
        time.sleep(0.4)
        after = session.properties.get(CODE_ISO).value
        ok = mid == 125 and after == before
        record(
            "R.iso.write",
            "ok" if ok else "FAIL",
            f"{before} -> {mid} -> {after}",
        )

        record(
            "R.remote.capabilities",
            "ok",
            f"live_view={session.capabilities.live_view} "
            f"content_index={session.capabilities.content_index}",
        )

    with camera.open("remote_transfer") as session:
        caps = session.capabilities
        ok = caps.content_index and not caps.live_view
        record(
            "R.transfer.capabilities",
            "ok" if ok else "FAIL",
            f"content_index={caps.content_index} live_view={caps.live_view}",
        )


# -- A: fresh content association -------------------------------------------
def stage_content(sdk: crsdkpy.SDK) -> None:
    print("\n== A: fresh content association ==")
    camera = sdk.discover()[0]
    with camera.open("remote_transfer") as session:
        if session.properties.get(CODE_FOCUS_MODE).value != FOCUS_MODE_AF_S:
            record("A", "SKIP", "camera is not in AF-S")
            return

        session.drain_events(timeout_ms=1200)
        while session.drain_events(timeout_ms=300):
            pass

        baseline = [c.content_id for c in session.content.since()]
        newest = baseline[-1] if baseline else None
        record("A.baseline", "ok", f"{len(baseline)} item(s), newest {newest}")

        ask("Aim at a high-contrast target, then trigger shot A")
        first = shoot(session, "A.shot1", newest)
        if first is None:
            return

        ask("Change the framing noticeably, then trigger shot B")
        second = shoot(session, "A.shot2", first["content"].content_id)
        if second is None:
            return

        # The point of the whole stage: shot B must not be able to come back
        # carrying shot A's identity.
        advanced = second["content"].content_id > first["content"].content_id
        record(
            "A.ids_advanced",
            "ok" if advanced else "FAIL",
            f"{first['content'].content_id} -> {second['content'].content_id}",
        )
        distinct = (
            second["screennail"].content_id == second["content"].content_id
            and second["screennail"].content_id != first["content"].content_id
        )
        record("A.no_stale_association", "ok" if distinct else "FAIL")

        # Re-fetching shot A after shot B must still describe shot A. Bytes are
        # deliberately not compared: this body regenerates an embedded
        # identifier per transfer.
        again = session.content.preview(first["content"], SCREENNAIL)
        record(
            "A.refetch_first",
            "ok" if again.content_id == first["content"].content_id else "FAIL",
            f"id={again.content_id} {again.byte_length} bytes sha={digest(again.data)}",
        )

        newer = session.content.since(first["content"])
        only_second = [c.content_id for c in newer] == [second["content"].content_id]
        record("A.newer_than_filter", "ok" if only_second else "FAIL",
               str([c.content_id for c in newer]))
        check_clean(session, "A.cautions")


def shoot(
    session: crsdkpy.Session, stage: str, baseline: Optional[int]
) -> Optional[dict]:
    """One gated capture, timed from the exposure onwards."""
    try:
        capture = session.autofocus_and_capture()
    except crsdkpy.AutofocusFailedError as exc:
        record(stage, "FAIL", f"autofocus did not confirm: {exc.focus_state.value}")
        return None
    if not capture.exposed:
        record(stage, "FAIL", "no exposure event")
        return None
    exposed_at = time.monotonic()
    record(
        stage,
        "ok",
        f"focus {capture.focus.state.value} in {capture.focus.elapsed_ms} ms, "
        f"exposure at {capture.exposure_latency_ms} ms",
    )

    content = capture.wait_for_content(timeout_ms=15_000)
    visible_ms = int((time.monotonic() - exposed_at) * 1000)
    if baseline is not None and content.content_id <= baseline:
        record(f"{stage}.content", "FAIL", "content id did not advance past baseline")
        return None
    record(
        f"{stage}.content",
        "ok",
        f"id={content.content_id} {content.filename} file_id={content.file_id} "
        f"+{visible_ms} ms after exposure",
    )

    previews = {}
    for kind in (THUMBNAIL, SCREENNAIL):
        started = time.monotonic()
        preview = capture.preview(kind, timeout_ms=20_000)
        elapsed = int((time.monotonic() - started) * 1000)
        total = int((time.monotonic() - exposed_at) * 1000)
        if not validate_preview(f"{stage}.{kind.value}", preview, content.content_id):
            return None
        record(
            f"{stage}.{kind.value}.timing",
            "ok",
            f"transfer {elapsed} ms, +{total} ms after exposure",
        )
        previews[kind.value] = preview

    return {"capture": capture, "content": content,
            "screennail": previews["screennail"]}


# -- B and C: postview and live view ----------------------------------------
def stage_postview_and_live_view(sdk: crsdkpy.SDK, *, live_view: bool) -> None:
    print("\n== B/C: postview and live view ==")
    camera = sdk.discover()[0]
    session = camera.open("remote")
    original = session.destination
    try:
        session.set_destination(HOST_AND_CARD)
        record(
            "B.destination",
            "ok",
            f"{original.value} -> {session.destination.value}",
        )
        caps = session.capabilities
        record(
            "B.capabilities",
            "ok",
            f"postview_delivery={caps.postview_delivery} "
            f"postview_configuration={caps.postview_configuration} "
            f"live_view={caps.live_view}",
        )

        # Configuration is attempted for its own sake: the reference body
        # refuses it in this mode and delivers postview anyway, and that
        # disagreement is exactly what the two capabilities exist to express.
        try:
            session.configure_postview(enabled=True)
            record("B.configure", "ok", "accepted")
        except crsdkpy.UnsupportedOperationError as exc:
            record("B.configure", "ok", f"refused as expected: {exc.message[:60]}")

        if live_view and session.capabilities.live_view:
            stats = session.live_view.measure(duration_ms=5_000)
            record("C.stream", "ok" if stats.frames else "FAIL", repr(stats))
            record(
                "C.detail",
                "ok",
                f"frames={stats.frames} empty_polls={stats.empty_polls} "
                f"skipped={stats.skipped} bytes {stats.min_bytes}-{stats.max_bytes} "
                f"interval {stats.min_interval_ms}-{stats.max_interval_ms} ms "
                f"max_fetch={stats.max_fetch_ms} ms "
                f"throughput={stats.throughput_mib_s:.2f} MiB/s",
            )
        elif live_view:
            record("C.stream", "FAIL", "live view unavailable in remote mode")

        ask("Aim at a high-contrast target, then trigger the postview shot")
        capture = session.autofocus_and_capture()
        if not capture.exposed:
            record("B.capture", "FAIL", "no exposure event")
            return
        exposed_at = time.monotonic()
        record("B.capture", "ok", f"exposure at {capture.exposure_latency_ms} ms")

        try:
            postview = capture.preview(POSTVIEW, timeout_ms=15_000)
            delivered_ms = int((time.monotonic() - exposed_at) * 1000)
            validate_preview("B.postview", postview, None)
            record("B.postview.timing", "ok", f"+{delivered_ms} ms after exposure")
        except crsdkpy.CrSDKPyError as exc:
            record("B.postview", "FAIL", f"{type(exc).__name__}: {exc}")

        if live_view and session.capabilities.live_view:
            # The stream pauses around the exposure and comes back on its own.
            resumed = session.live_view.try_get_frame(timeout_ms=3_000)
            record(
                "C.resume",
                "ok" if resumed is not None else "FAIL",
                repr(resumed) if resumed else "no frame after the exposure",
            )
        check_clean(session, "B.cautions")
    finally:
        try:
            session.set_destination(original)
            record(
                "B.restore",
                "ok",
                f"destination back to {session.destination.value}",
            )
        finally:
            session.close()


# -- D: video ---------------------------------------------------------------
def stage_video(sdk: crsdkpy.SDK) -> None:
    print("\n== D: video ==")
    camera = sdk.discover()[0]
    with camera.open("remote") as session:
        if not session.capabilities.video:
            record("D", "SKIP", "this session reports no recording state")
            return
        record("D.idle_before", "ok" if session.video.state
               is crsdkpy.RecordingState.IDLE else "FAIL",
               session.video.state.value)

        recording = session.video.start()
        record("D.start", "ok" if recording.active else "FAIL",
               session.video.state.value)

        if session.capabilities.live_view:
            frame = session.live_view.try_get_frame(timeout_ms=2_000)
            record("D.live_view_while_recording",
                   "ok" if frame is not None else "FAIL",
                   repr(frame) if frame else "no frame")

        time.sleep(3.0)
        record("D.active", "ok" if session.video.recording else "FAIL",
               session.video.state.value)

        recording.stop()
        record("D.stop", "ok" if session.video.state
               is crsdkpy.RecordingState.IDLE else "FAIL",
               session.video.state.value)
        check_clean(session, "D.cautions")


# -- M and N: optional focus gates ------------------------------------------
def stage_manual_focus(sdk: crsdkpy.SDK) -> None:
    print("\n== M: manual-focus capture ==")
    ask("Switch the lens to MF")
    camera = sdk.discover()[0]
    with camera.open("remote") as session:
        mode = session.properties.get(CODE_FOCUS_MODE).value
        if mode != FOCUS_MODE_MF:
            record("M", "SKIP", f"camera reports focus mode 0x{mode:X}, not MF")
            return
        capture = session.capture()
        record(
            "M.capture",
            "ok" if capture.exposed else "FAIL",
            f"exposure at {capture.exposure_latency_ms} ms"
            if capture.exposed
            else "no exposure event",
        )
        check_clean(session, "M.cautions")
    ask("Switch the lens back to AF")


def stage_no_lock(sdk: crsdkpy.SDK) -> None:
    print("\n== N: autofocus no-lock cleanup ==")
    ask("Cover the lens completely")
    camera = sdk.discover()[0]
    with camera.open("remote") as session:
        try:
            session.autofocus_and_capture(focus_timeout_ms=3_000)
            record("N.refusal", "FAIL", "an exposure was requested without focus")
        except crsdkpy.AutofocusFailedError as exc:
            record("N.refusal", "ok", f"refused after {exc.elapsed_ms} ms")
        engaged = session.raw.half_press
        record("N.cleanup", "ok" if not engaged else "FAIL",
               f"half press engaged={engaged}")
        check_clean(session, "N.cautions")
    ask("Uncover the lens")


# -- restoration ------------------------------------------------------------
def restore_and_verify(sdk: crsdkpy.SDK) -> None:
    print("\n== restore ==")
    camera = sdk.discover()[0]
    with camera.open("remote") as session:
        if session.raw.half_press:
            session.raw.set_half_press(False)
        if session.destination is not CARD:
            session.set_destination(CARD)
        if session.video.state is not crsdkpy.RecordingState.IDLE:
            session.video.stop()

        snapshot = session.properties.snapshot()
        state = {
            "iso": snapshot.value_of(CODE_ISO),
            "focus_mode": snapshot.value_of(CODE_FOCUS_MODE),
            "focus_indication": snapshot.value_of(CODE_FOCUS_INDICATION),
            "destination": session.destination.value,
            "recording": session.video.state.value,
            "half_press": session.raw.half_press,
        }
        for name, value in state.items():
            print(f"  {name:18}: {value}")
        check_clean(session, "restore.cautions")

        ok = (
            state["destination"] == CARD.value
            and state["recording"] == "idle"
            and not state["half_press"]
        )
        record("restore", "ok" if ok else "FAIL")


STAGES = {
    "R": ("regressions, no exposures", stage_regressions),
    "A": ("fresh content association, 2 exposures", stage_content),
    "B": ("postview and live view, 1 exposure", None),
    "C": ("live view (runs with B)", None),
    "D": ("video, one short recording", stage_video),
    "M": ("manual-focus capture, 1 exposure, needs MF", stage_manual_focus),
    "N": ("autofocus no-lock, needs the lens covered", stage_no_lock),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stages", nargs="+", default=["R", "A", "B", "D"])
    parser.add_argument("--backend", default="host")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for key, (description, _) in STAGES.items():
            print(f"  {key}  {description}")
        return 0

    wanted = [stage.upper() for stage in args.stages]
    print(f"stages: {', '.join(wanted)}")

    with crsdkpy.SDK(backend=args.backend) as sdk:
        try:
            if "R" in wanted:
                stage_regressions(sdk)
            if "A" in wanted:
                stage_content(sdk)
            if "B" in wanted or "C" in wanted:
                stage_postview_and_live_view(sdk, live_view="C" in wanted or
                                             "B" in wanted)
            if "D" in wanted:
                stage_video(sdk)
            if "M" in wanted:
                stage_manual_focus(sdk)
            if "N" in wanted:
                stage_no_lock(sdk)
        finally:
            restore_and_verify(sdk)

    print("\n== summary ==")
    failures = [r for r in RESULTS if r[1] == "FAIL"]
    skipped = [r for r in RESULTS if r[1] == "SKIP"]
    for stage, outcome, detail in RESULTS:
        if outcome != "ok":
            print(f"  {outcome:4} {stage}: {detail}")
    print(
        f"\n{len(RESULTS)} checks, {len(failures)} failed, {len(skipped)} skipped"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
