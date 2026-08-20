"""Phase-resolved capture timing.

Field integrations reported capture times clustering at suspiciously round
values -- around 11 s, 13 s and 20 s -- rather than spreading the way a real
camera write latency would. Sums of this library's own deadlines land on those
same numbers, so the question is not "how slow is the camera" but "which wait
is running to its end, and why".

Answering that needs each phase timed separately, which no single call does:

    baseline read -> autofocus -> release -> exposure event -> content visible
    -> filename resolved

so this drives the public API one phase at a time and prints one row per
iteration. Any phase whose duration lands on a deadline is flagged, because a
phase that ends exactly on its deadline did not measure the camera, it measured
us waiting.

    python tools/capture_timing.py --mode af --count 10
    python tools/capture_timing.py --mode plain --count 10   # needs MF

Nothing here formats media, deletes content, or changes focus mode. Every
iteration is one real exposure.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from typing import Optional

import crsdkpy

# The deadlines a phase can run into. A measured duration within a small
# tolerance of one of these is evidence the wait expired rather than completed.
DEADLINES_MS = {
    "focus": 3_000,
    "exposure": 10_000,
    "content": 10_000,
}
TOLERANCE_MS = 400

CODE_FOCUS_MODE = 0x0109


def near_deadline(name: str, elapsed_ms: float) -> Optional[int]:
    """Return the deadline this duration appears to have hit, if any."""
    deadline = DEADLINES_MS.get(name)
    if deadline is None:
        return None
    return deadline if abs(elapsed_ms - deadline) <= TOLERANCE_MS else None


class Row:
    """One iteration, phase by phase."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.baseline_ms = 0.0
        self.capture_ms = 0.0
        self.content_ms = 0.0
        self.total_ms = 0.0
        self.exposure_latency_ms: Optional[int] = None
        self.focus_ms: Optional[int] = None
        self.focus_state = ""
        self.focus_source = ""
        self.exposed = False
        self.baseline_id: Optional[int] = None
        self.content_id: Optional[int] = None
        self.path = ""
        self.error = ""
        self.flags: list[str] = []

    def advanced(self) -> bool:
        return (
            self.content_id is not None
            and self.baseline_id is not None
            and self.content_id > self.baseline_id
        )

    def __str__(self) -> str:
        content = f"{self.content_id}" if self.content_id is not None else "-"
        name = self.path.rsplit("/", 1)[-1] if self.path else "-"
        flags = (" " + ",".join(self.flags)) if self.flags else ""
        af = self.focus_ms if self.focus_ms is not None else "-"
        exp = self.exposure_latency_ms if self.exposure_latency_ms is not None else "-"
        return (
            f"  {self.index:3d} "
            f"base {self.baseline_ms:6.0f} "
            f"cap {self.capture_ms:7.0f} "
            f"(af {af:>5} exp {exp:>5}) "
            f"content {self.content_ms:7.0f} "
            f"total {self.total_ms:7.0f} ms | "
            f"exposed={str(self.exposed):5} id={content:>7} {name:>14}"
            f"{flags}{(' ' + self.error) if self.error else ''}"
        )


def run_iteration(session: crsdkpy.Session, index: int, mode: str) -> Row:
    row = Row(index)
    t_start = time.monotonic()

    # Drain first so a straggler from the previous iteration is not counted as
    # this one's evidence.
    session.drain_events(timeout_ms=0)

    t0 = time.monotonic()
    baseline = [c.content_id for c in session.content.since()]
    row.baseline_id = baseline[-1] if baseline else None
    row.baseline_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    try:
        if mode == "af":
            capture = session.autofocus_and_capture(raise_on_focus_failure=False)
        else:
            capture = session.capture()
        row.capture_ms = (time.monotonic() - t0) * 1000
        row.exposed = capture.exposed
        row.exposure_latency_ms = capture.exposure_latency_ms
        focus = getattr(capture, "focus", None)
        if focus is not None:
            row.focus_ms = focus.elapsed_ms
            row.focus_state = focus.state.value
            row.focus_source = focus.source
    except crsdkpy.CrSDKPyError as exc:
        row.capture_ms = (time.monotonic() - t0) * 1000
        row.error = f"{type(exc).__name__}: {exc}"
        row.total_ms = (time.monotonic() - t_start) * 1000
        return row

    if row.exposed:
        t0 = time.monotonic()
        try:
            content = capture.wait_for_content()
            row.content_ms = (time.monotonic() - t0) * 1000
            row.content_id = content.content_id
            row.path = content.path or ""
        except crsdkpy.CrSDKPyError as exc:
            row.content_ms = (time.monotonic() - t0) * 1000
            row.error = f"{type(exc).__name__}: {exc}"

    row.total_ms = (time.monotonic() - t_start) * 1000

    # A capture that produced no exposure reports no latency at all, so the
    # check has to look at how long the call itself took. This is the case that
    # matters: it is the accepted-release-with-no-exposure the whole tool exists
    # to find, and comparing a missing latency would silently miss every one.
    if not row.exposed and not row.error:
        row.flags.append("NO-EXPOSURE")
        # Measure the wait alone. The call also spent time focusing, and on
        # hardware that was enough to push the total past the deadline it was
        # being compared against -- 10740 ms of call for a 10000 ms wait -- so
        # comparing the whole call would miss the very case this looks for.
        waited = row.capture_ms - (row.focus_ms or 0)
        if near_deadline("exposure", waited):
            row.flags.append("HIT-EXPOSURE-DEADLINE")

    for name, elapsed in (
        ("focus", row.focus_ms),
        ("exposure", row.exposure_latency_ms),
        ("content", row.content_ms),
    ):
        if elapsed is None:
            continue
        if near_deadline(name, float(elapsed)):
            row.flags.append(f"HIT-{name.upper()}-DEADLINE")
    return row


def summarise(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label:22}: none")
        return
    ordered = sorted(values)
    print(
        f"  {label:22}: n={len(values):3d} "
        f"min {ordered[0]:7.0f} "
        f"median {statistics.median(ordered):7.0f} "
        f"max {ordered[-1]:7.0f} ms"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["af", "plain"], default="af")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--session", default="remote_transfer")
    parser.add_argument("--backend", default="host")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to wait between iterations, to test whether the "
        "refusal rate depends on how hard the body is driven",
    )
    args = parser.parse_args()

    print(
        f"mode={args.mode} count={args.count} session={args.session} "
        f"delay={args.delay}s"
    )

    rows: list[Row] = []
    with crsdkpy.SDK(backend=args.backend) as sdk:
        camera = sdk.discover()[0]
        with camera.open(args.session) as session:
            focus_mode = session.properties.get(CODE_FOCUS_MODE).value
            print(f"focus_mode=0x{focus_mode:04X} ({focus_mode})")
            print(f"destination={session.destination.value}")
            print(
                "  idx      base      cap    (af    exp)   content    total"
            )
            for index in range(1, args.count + 1):
                if index > 1 and args.delay:
                    time.sleep(args.delay)
                row = run_iteration(session, index, args.mode)
                rows.append(row)
                print(row, flush=True)

    print("\n== distribution ==")
    summarise("baseline read", [r.baseline_ms for r in rows])
    summarise("capture call", [r.capture_ms for r in rows])
    summarise(
        "exposure latency",
        [float(r.exposure_latency_ms) for r in rows if r.exposure_latency_ms],
    )
    summarise("content visible", [r.content_ms for r in rows if r.exposed])
    summarise("whole operation", [r.total_ms for r in rows])

    exposed = [r for r in rows if r.exposed]
    resolved = [r for r in rows if r.content_id is not None]
    advanced = [r for r in rows if r.advanced()]
    flagged = [r for r in rows if r.flags]
    print(
        f"\n  exposed {len(exposed)}/{len(rows)}, "
        f"content resolved {len(resolved)}/{len(rows)}, "
        f"id advanced {len(advanced)}/{len(rows)}, "
        f"flagged {len(flagged)}"
    )
    for row in flagged:
        focus = (
            f" focus={row.focus_state or '-'} via {row.focus_source or '-'}"
            f" in {row.focus_ms} ms"
            if row.focus_ms is not None
            else ""
        )
        print(f"    iteration {row.index}: {','.join(row.flags)}{focus}")
    if len(exposed) != len(rows):
        print(
            "\n  A capture flagged NO-EXPOSURE means the camera accepted the "
            "release and\n  never exposed. It reports no event of any kind, "
            "so the time went on the\n  exposure deadline rather than on the "
            "camera. Re-run with --delay to see\n  whether pacing changes it."
        )
    errors = [r for r in rows if r.error]
    for row in errors:
        print(f"    iteration {row.index} error: {row.error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
