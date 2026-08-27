"""Compare a characterization capture against a tracked conformance profile.

The last step of the workflow:

    characterize_camera.py   -> a private capture of what a body reports
    sanitize_characterization.py -> the tracked profile, once, for a new body
    compare_profile.py       -> what this body does differently

    python tools/compare_profile.py --snapshot capture.json
    python tools/compare_profile.py --snapshot capture.json --profile ILME-FX3A
    python tools/compare_profile.py --snapshot capture.json --severity warning

Differences are the expected outcome. A different Sony body will report
different properties, different constraints and different capabilities, and
none of that is a failure -- so the exit status reflects only whether a
declared invariant broke, not whether anything differed.

A capture whose own profile does not exist yet is still worth running this
against: the reference body's profile will produce a long, informative diff
that is exactly the starting point for writing the new body's profile.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from crsdkpy.conformance import (
    FindingKind,
    Observation,
    Severity,
    compare,
    load_profile,
    profile_names,
)

_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

#: Kinds worth calling out in the summary because they usually mean a body is
#: genuinely different rather than merely differently configured.
_HEADLINE = (
    FindingKind.INVARIANT_VIOLATED,
    FindingKind.MISSING_PROPERTY,
    FindingKind.NEW_PROPERTY,
    FindingKind.UNKNOWN_PROPERTY,
)


def render(report, minimum: str, limit: Optional[int]) -> None:
    floor = _ORDER[minimum]
    shown = [f for f in report if _ORDER[f.severity] <= floor]
    shown.sort(key=lambda f: (_ORDER[f.severity], f.surface or "", f.code or -1))

    print(report.summary())
    print()
    if not shown:
        print(f"nothing at severity {minimum} or above")
        return

    truncated = 0
    if limit is not None and len(shown) > limit:
        truncated = len(shown) - limit
        shown = shown[:limit]

    for finding in shown:
        print(f"  {finding}")
        if finding.expected is not None or finding.observed is not None:
            print(f"      profile: {finding.expected!r}")
            print(f"      camera:  {finding.observed!r}")
    if truncated:
        print(f"  ... {truncated} more not shown; raise --limit to see them")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="camera_snapshot JSON")
    parser.add_argument(
        "--profile", default="ILME-FX3A",
        help=f"profile name or path (shipped: {', '.join(profile_names()) or 'none'})",
    )
    parser.add_argument(
        "--severity", default=Severity.INFO, choices=sorted(_ORDER),
        help="lowest severity to print; the report counts everything regardless",
    )
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--json", help="also write the findings to this path")
    parser.add_argument(
        "--surfaces", nargs="+",
        help="limit the comparison to these surface keys, e.g. remote+memory_card",
    )
    args = parser.parse_args()

    with open(args.snapshot, encoding="utf-8") as handle:
        snapshot = json.load(handle)

    profile = load_profile(args.profile)
    observation = Observation.from_snapshot(snapshot)
    if not observation.surfaces:
        print("the capture contains no successfully sampled surface", file=sys.stderr)
        return 2

    report = compare(profile, observation, surfaces=args.surfaces)
    render(report, args.severity, args.limit if args.limit > 0 else None)

    headline = report.of_kind(*_HEADLINE)
    if headline:
        print()
        print("worth a look:")
        for kind in _HEADLINE:
            count = len(report.of_kind(kind))
            if count:
                print(f"  {kind}: {count}")

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "profile": profile.profile_id,
                    "conformant": report.conformant,
                    "counts": report.counts(),
                    "findings": [
                        {
                            "kind": f.kind,
                            "severity": f.severity,
                            "surface": f.surface,
                            "code": f.code_hex,
                            "name": f.name,
                            "summary": f.summary,
                            "expected": f.expected,
                            "observed": f.observed,
                        }
                        for f in report
                    ],
                },
                handle,
                indent=1,
                sort_keys=True,
            )
            handle.write("\n")
        print(f"\nwrote {args.json}")

    # Differences are normal; only a broken invariant is a failure.
    return 0 if report.conformant else 1


if __name__ == "__main__":
    sys.exit(main())
