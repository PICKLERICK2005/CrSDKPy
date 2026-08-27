"""Conformance profiles: what one body was measured to expose, and how to
compare another body against it.

A profile is sanitized hardware evidence. It records the property surface a
specific body/firmware/SDK combination reported across the control-mode and
destination surfaces that matter, the constraints each property advertised, and
the capabilities that were actually measured rather than assumed.

The point of comparing against one is **not** to assert that every camera is
the reference body. A different Sony body is expected to differ, and most
differences are information rather than failure:

* extra properties are normal -- a newer or larger body exposes more;
* a property changing between read-only and read-write is often mode-, lens- or
  state-dependent, and hardware has shown exactly that;
* vendor codes this release cannot name must be *surfaced*, never rejected;
* the number of properties is never a health check. The same body reported
  different counts in different control modes, so a count is an observation and
  nothing more.

What a comparison does assert is the small set of things any body has to do for
CrSDKPy to work at all. Those are the invariants, they are declared in the
profile rather than hardcoded here, and they are deliberately few.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

__all__ = [
    "Finding",
    "FindingKind",
    "Observation",
    "ConformanceProfile",
    "ConformanceReport",
    "PropertyRecord",
    "SurfaceRecord",
    "Severity",
    "compare",
    "load_profile",
    "profile_names",
    "reference_profile",
]

PROFILE_SCHEMA = "crsdkpy.conformance_profile/1"

_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")

#: The profile shipped as the comparison baseline. Naming it here rather than
#: leaving callers to guess keeps "the reference body" a single fact.
REFERENCE_PROFILE = "ILME-FX3A"


class Severity:
    """How much a finding should worry a caller.

    ``ERROR`` is reserved for a declared invariant. Everything else is a
    difference, and a difference between two camera bodies is ordinary.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class FindingKind:
    """What sort of difference was found.

    These are separate from :class:`Severity` on purpose: a caller filtering a
    report usually wants one axis or the other, not a single blended verdict.
    """

    #: A rule the profile declares as required broke.
    INVARIANT_VIOLATED = "invariant_violated"
    #: A value the profile records as semantically load-bearing differs.
    REFERENCE_VALUE_DIFFERS = "reference_value_differs"
    #: A value that varies with body, session or state differs.
    BODY_SPECIFIC = "body_specific"
    #: A code neither the profile nor the vendor name table accounts for.
    UNKNOWN_PROPERTY = "unknown_property"
    #: A code the observation has and the profile does not.
    NEW_PROPERTY = "new_property"
    #: A code the profile has and the observation does not.
    MISSING_PROPERTY = "missing_property"
    #: Read-only/read-write changed.
    ACCESS_CHANGED = "access_changed"
    #: The advertised list or range changed.
    VALUES_CHANGED = "values_changed"
    #: The vendor data type changed.
    TYPE_CHANGED = "type_changed"
    #: A surface the profile records was not sampled at all.
    SURFACE_NOT_SAMPLED = "surface_not_sampled"
    #: A surface the observation has and the profile does not.
    SURFACE_NEW = "surface_new"
    #: A measured capability differs.
    CAPABILITY_DIFFERS = "capability_differs"


@dataclass(frozen=True)
class Finding:
    """One difference, classified."""

    kind: str
    severity: str
    summary: str
    surface: Optional[str] = None
    code: Optional[int] = None
    name: Optional[str] = None
    expected: Any = None
    observed: Any = None

    @property
    def code_hex(self) -> Optional[str]:
        return None if self.code is None else f"0x{self.code:04X}"

    def __str__(self) -> str:
        where = []
        if self.surface:
            where.append(self.surface)
        if self.code is not None:
            label = self.code_hex
            if self.name:
                label = f"{label} {self.name}"
            where.append(label)
        prefix = " ".join(where)
        located = f"{prefix}: " if prefix else ""
        return f"[{self.severity}] {self.kind}: {located}{self.summary}"


@dataclass(frozen=True)
class PropertyRecord:
    """One property as a profile or an observation records it.

    ``value`` is never a comparison target on its own: current values move with
    session and body state. Structure -- the type, the access, the advertised
    constraint -- is what a surface comparison is about.
    """

    code: int
    name: Optional[str] = None
    #: Vendor ``CrDataType`` as reported, not CrSDKPy's collapsed view.
    data_type: Optional[int] = None
    access: Optional[str] = None
    #: ``"enum"``, ``"range"``, ``"raw"`` or ``None`` when nothing is advertised.
    value_set: Optional[str] = None
    values: tuple[int, ...] = ()
    value: Any = None

    @property
    def code_hex(self) -> str:
        return f"0x{self.code:04X}"

    @property
    def named(self) -> bool:
        return bool(self.name)

    def constraint_differs_from(self, other: PropertyRecord) -> bool:
        return self.value_set != other.value_set or self.values != other.values

    @classmethod
    def from_json(cls, code: str, entry: Mapping[str, Any]) -> PropertyRecord:
        raw_values = entry.get("values") or ()
        return cls(
            code=int(code, 16) if isinstance(code, str) else int(code),
            name=entry.get("name"),
            data_type=entry.get("data_type"),
            access=entry.get("access"),
            value_set=entry.get("value_set"),
            values=tuple(int(v) for v in raw_values),
            value=entry.get("value"),
        )


@dataclass(frozen=True)
class SurfaceRecord:
    """One control-mode/destination combination.

    ``absent_codes`` rather than a per-surface inventory: on the reference body
    the surfaces differ by two codes out of several hundred, so recording the
    delta is both smaller and clearer about what the measurement actually was.
    """

    control_mode: str
    still_destination: str
    property_count: int = 0
    absent_codes: frozenset[int] = field(default_factory=frozenset)
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    reference_values: Mapping[int, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.control_mode}+{self.still_destination}"

    @classmethod
    def from_json(cls, key: str, entry: Mapping[str, Any]) -> SurfaceRecord:
        mode, _, destination = key.partition("+")
        return cls(
            control_mode=entry.get("control_mode", mode),
            still_destination=entry.get("still_destination", destination),
            property_count=int(entry.get("property_count", 0)),
            absent_codes=frozenset(
                int(c, 16) for c in entry.get("absent_codes", ())
            ),
            capabilities=dict(entry.get("capabilities", {})),
            reference_values={
                int(c, 16): v
                for c, v in (entry.get("reference_values", {}) or {}).items()
            },
        )


class ConformanceProfile:
    """Sanitized evidence from one measured body.

    Construct with :func:`load_profile`. Nothing here reaches a camera; a
    profile is inert data.
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        schema = document.get("schema")
        if schema != PROFILE_SCHEMA:
            raise ValueError(
                f"not a conformance profile: schema is {schema!r}, "
                f"expected {PROFILE_SCHEMA!r}"
            )
        self._document = dict(document)
        self.properties: dict[int, PropertyRecord] = {
            record.code: record
            for record in (
                PropertyRecord.from_json(code, entry)
                for code, entry in document.get("properties", {}).items()
            )
        }
        self.surfaces: dict[str, SurfaceRecord] = {
            key: SurfaceRecord.from_json(key, entry)
            for key, entry in document.get("surfaces", {}).items()
        }
        self.unknown_codes: frozenset[int] = frozenset(
            int(c, 16) for c in document.get("unknown_codes", ())
        )
        self.invariants: tuple[str, ...] = tuple(document.get("invariants", ()))

    # -- identity ----------------------------------------------------------
    @property
    def profile_id(self) -> str:
        return self._document.get("profile_id", "")

    @property
    def body(self) -> Mapping[str, Any]:
        return self._document.get("body", {})

    @property
    def sdk(self) -> Mapping[str, Any]:
        return self._document.get("sdk", {})

    @property
    def lens(self) -> Mapping[str, Any]:
        return self._document.get("lens", {})

    @property
    def measured(self) -> Mapping[str, Any]:
        """Normalized feature evidence that is not a property or a capability."""
        return self._document.get("measured", {})

    @property
    def document(self) -> Mapping[str, Any]:
        """The underlying JSON, for callers that want a field this does not model."""
        return self._document

    def codes_for(self, surface: str) -> frozenset[int]:
        """Codes this profile says the named surface exposes."""
        record = self.surfaces.get(surface)
        if record is None:
            return frozenset()
        return frozenset(self.properties) - record.absent_codes

    def __repr__(self) -> str:
        return (
            f"ConformanceProfile({self.profile_id!r}, "
            f"{len(self.properties)} properties, {len(self.surfaces)} surfaces)"
        )


@dataclass
class Observation:
    """What a camera reported now, in the shape a comparison needs.

    Built either from a characterization snapshot with :meth:`from_snapshot` or
    directly by a caller that already has the numbers.
    """

    surfaces: dict[str, dict[int, PropertyRecord]] = field(default_factory=dict)
    capabilities: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    unknown_codes: dict[str, frozenset[int]] = field(default_factory=dict)
    body: Mapping[str, Any] = field(default_factory=dict)

    def property_count(self, surface: str) -> int:
        return len(self.surfaces.get(surface, {}))

    @classmethod
    def from_snapshot(cls, document: Mapping[str, Any]) -> Observation:
        """Read a ``crsdkpy.camera_snapshot/1`` document.

        Tolerant by design: a surface that failed to open is skipped rather
        than fabricated, because "not sampled" and "sampled and empty" are
        different answers and only one of them is a finding.
        """
        observation = cls(body=dict(document.get("camera", {})))
        for entry in document.get("surfaces", ()):
            if entry.get("error") or "properties" not in entry:
                continue
            key = f"{entry.get('control_mode')}+{entry.get('still_destination')}"
            records: dict[int, PropertyRecord] = {}
            for prop in entry["properties"]:
                record = _record_from_snapshot_property(prop)
                records[record.code] = record
            observation.surfaces[key] = records
            observation.capabilities[key] = dict(entry.get("capabilities", {}))
            observation.unknown_codes[key] = frozenset(
                _parse_code(c) for c in entry.get("unknown_codes", ())
            )
        return observation


def _parse_code(code: Union[int, str]) -> int:
    if isinstance(code, int):
        return code
    return int(code, 16)


def _record_from_snapshot_property(prop: Mapping[str, Any]) -> PropertyRecord:
    value_set, values = _constraint_from_snapshot(prop)
    return PropertyRecord(
        code=_parse_code(prop["code"]),
        name=prop.get("name") or None,
        data_type=prop.get("data_type"),
        access=prop.get("access"),
        value_set=value_set,
        values=values,
        value=prop.get("value"),
    )


def _constraint_from_snapshot(
    prop: Mapping[str, Any]
) -> tuple[Optional[str], tuple[int, ...]]:
    """Recover the advertised-constraint shape from a capture.

    A capture that states the shape is believed. An older one that does not is
    read for positive evidence only, and absent evidence stays ``None`` --
    "not captured" rather than "advertises nothing", because a comparison that
    confuses the two invents hundreds of differences out of a missing field.
    """
    stated = prop.get("value_set")
    if stated is not None:
        return stated, tuple(int(v) for v in (prop.get("values") or ()))
    value_range = prop.get("range")
    if isinstance(value_range, Mapping):
        return "range", (
            int(value_range.get("minimum", 0)),
            int(value_range.get("maximum", 0)),
            int(value_range.get("step") or 0),
        )
    allowed = prop.get("allowed_values")
    if allowed:
        return "enum", tuple(int(v) for v in allowed)
    if (prop.get("metadata") or {}).get("values_undecoded"):
        return "raw", ()
    return None, ()


class ConformanceReport:
    """The result of one comparison. Iterable over its findings."""

    def __init__(self, profile: ConformanceProfile, findings: Sequence[Finding]):
        self.profile = profile
        self.findings: tuple[Finding, ...] = tuple(findings)

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def of_kind(self, *kinds: str) -> tuple[Finding, ...]:
        wanted = set(kinds)
        return tuple(f for f in self.findings if f.kind in wanted)

    def of_severity(self, *severities: str) -> tuple[Finding, ...]:
        wanted = set(severities)
        return tuple(f for f in self.findings if f.severity in wanted)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return self.of_severity(Severity.ERROR)

    @property
    def conformant(self) -> bool:
        """True when no declared invariant broke.

        Deliberately not "no findings": a different body producing no findings
        at all would mean the comparison was not looking.
        """
        return not self.errors

    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for finding in self.findings:
            totals[finding.kind] = totals.get(finding.kind, 0) + 1
        return totals

    def summary(self) -> str:
        if not self.findings:
            return f"{self.profile.profile_id}: no differences"
        parts = [f"{kind}={count}" for kind, count in sorted(self.counts().items())]
        verdict = "conformant" if self.conformant else "INVARIANT VIOLATED"
        return f"{self.profile.profile_id}: {verdict}; " + ", ".join(parts)

    def __repr__(self) -> str:
        return f"ConformanceReport({len(self.findings)} findings)"


# -- invariants -------------------------------------------------------------
#
# Each takes the profile and the observation and yields findings. They are
# deliberately few and deliberately body-agnostic: an invariant that only the
# reference body can satisfy is not an invariant, it is a description.

def _invariant_surfaces_report_properties(
    profile: ConformanceProfile, observation: Observation
) -> Iterator[Finding]:
    for key, records in observation.surfaces.items():
        if not records:
            yield Finding(
                kind=FindingKind.INVARIANT_VIOLATED,
                severity=Severity.ERROR,
                surface=key,
                summary=(
                    "the surface was sampled and reported no properties at all; "
                    "a session that opens must expose something"
                ),
                expected=">0 properties",
                observed=0,
            )


def _invariant_unknown_codes_are_preserved(
    profile: ConformanceProfile, observation: Observation
) -> Iterator[Finding]:
    """Every code called unknown must still be in the inventory.

    Dropping a code because this release has no name for it is the one failure
    mode that makes a characterization actively misleading, so it is the one
    thing checked rather than reported.
    """
    for key, unknown in observation.unknown_codes.items():
        records = observation.surfaces.get(key, {})
        lost = sorted(code for code in unknown if code not in records)
        if lost:
            yield Finding(
                kind=FindingKind.INVARIANT_VIOLATED,
                severity=Severity.ERROR,
                surface=key,
                summary=(
                    "codes reported as unknown are missing from the property "
                    "inventory; an unnamed code must be preserved, not dropped: "
                    + ", ".join(f"0x{c:04X}" for c in lost)
                ),
                expected="unknown codes present in the inventory",
                observed=[f"0x{c:04X}" for c in lost],
            )


_KNOWN_ACCESS = frozenset(
    {"read_only", "read_write", "write_only", "unknown"}
)


def _invariant_access_states_are_known(
    profile: ConformanceProfile, observation: Observation
) -> Iterator[Finding]:
    for key, records in observation.surfaces.items():
        for record in sorted(records.values(), key=lambda r: r.code):
            if record.access is not None and record.access not in _KNOWN_ACCESS:
                yield Finding(
                    kind=FindingKind.INVARIANT_VIOLATED,
                    severity=Severity.ERROR,
                    surface=key,
                    code=record.code,
                    name=record.name,
                    summary=f"access {record.access!r} is not a modelled state",
                    expected=sorted(_KNOWN_ACCESS),
                    observed=record.access,
                )


def _invariant_codes_are_non_negative(
    profile: ConformanceProfile, observation: Observation
) -> Iterator[Finding]:
    for key, records in observation.surfaces.items():
        for code in sorted(records):
            if code < 0:
                yield Finding(
                    kind=FindingKind.INVARIANT_VIOLATED,
                    severity=Severity.ERROR,
                    surface=key,
                    code=code,
                    summary="property codes must be non-negative",
                    expected=">= 0",
                    observed=code,
                )


INVARIANTS = {
    "surfaces_report_properties": _invariant_surfaces_report_properties,
    "unknown_codes_are_preserved": _invariant_unknown_codes_are_preserved,
    "access_states_are_known": _invariant_access_states_are_known,
    "codes_are_non_negative": _invariant_codes_are_non_negative,
}


def compare(
    profile: ConformanceProfile,
    observation: Observation,
    *,
    surfaces: Optional[Iterable[str]] = None,
) -> ConformanceReport:
    """Compare an observation against a profile.

    Findings come back classified rather than pass/fail. Only a declared
    invariant produces :attr:`Severity.ERROR`; a body that simply differs
    produces warnings and information, which is the normal outcome for any
    camera that is not the one the profile was measured on.

    Notably absent: the property *count*. The same body reported different
    counts in different control modes, so a count is never compared as a
    health check -- it is reported as information when it moves and nothing
    more.
    """
    findings: list[Finding] = []

    for name in profile.invariants:
        check = INVARIANTS.get(name)
        if check is None:
            findings.append(
                Finding(
                    kind=FindingKind.INVARIANT_VIOLATED,
                    severity=Severity.ERROR,
                    summary=(
                        f"the profile declares invariant {name!r}, which this "
                        "release does not implement; the check cannot be made"
                    ),
                    expected=sorted(INVARIANTS),
                    observed=name,
                )
            )
            continue
        findings.extend(check(profile, observation))

    wanted = set(surfaces) if surfaces is not None else None
    for key in sorted(set(profile.surfaces) | set(observation.surfaces)):
        if wanted is not None and key not in wanted:
            continue
        findings.extend(_compare_surface(profile, observation, key))

    return ConformanceReport(profile, findings)


def _compare_surface(
    profile: ConformanceProfile, observation: Observation, key: str
) -> Iterator[Finding]:
    expected_surface = profile.surfaces.get(key)
    observed = observation.surfaces.get(key)

    if expected_surface is not None and observed is None:
        yield Finding(
            kind=FindingKind.SURFACE_NOT_SAMPLED,
            severity=Severity.INFO,
            surface=key,
            summary="the profile records this surface but it was not sampled",
        )
        return
    if expected_surface is None:
        yield Finding(
            kind=FindingKind.SURFACE_NEW,
            severity=Severity.INFO,
            surface=key,
            summary=(
                "the observation covers a surface the profile does not; a body "
                "may support combinations the reference body did not"
            ),
            observed=len(observed or {}),
        )
        return

    expected_codes = profile.codes_for(key)
    observed_codes = frozenset(observed)

    # The count is reported, never judged. Modes on one body produced
    # different counts, so equality here would be a false alarm by design.
    if len(observed_codes) != expected_surface.property_count:
        yield Finding(
            kind=FindingKind.BODY_SPECIFIC,
            severity=Severity.INFO,
            surface=key,
            summary=(
                "property count differs; this is an observation, not a health "
                "check -- counts vary by control mode on a single body"
            ),
            expected=expected_surface.property_count,
            observed=len(observed_codes),
        )

    for code in sorted(observed_codes - expected_codes):
        record = observed[code]
        yield Finding(
            kind=(
                FindingKind.UNKNOWN_PROPERTY
                if not record.named
                else FindingKind.NEW_PROPERTY
            ),
            severity=Severity.INFO,
            surface=key,
            code=code,
            name=record.name,
            summary=(
                "code not in the profile; a different body exposing more is "
                "expected, and an unnamed one must be surfaced rather than "
                "rejected"
            ),
            observed=record.access,
        )

    for code in sorted(expected_codes - observed_codes):
        expected_record = profile.properties[code]
        yield Finding(
            kind=FindingKind.MISSING_PROPERTY,
            severity=Severity.WARNING,
            surface=key,
            code=code,
            name=expected_record.name,
            summary=(
                "the profile records this code on this surface and the "
                "observation does not report it"
            ),
            expected=expected_record.access,
        )

    for code in sorted(observed_codes & expected_codes):
        yield from _compare_property(key, profile.properties[code], observed[code])

    yield from _compare_capabilities(key, expected_surface, observation)

    for code, expected_value in sorted(expected_surface.reference_values.items()):
        record = observed.get(code)
        # A record carrying no value says nothing about it, the same way an
        # uncaptured constraint does.
        if record is None or record.value is None or record.value == expected_value:
            continue
        yield Finding(
            kind=FindingKind.REFERENCE_VALUE_DIFFERS,
            severity=Severity.WARNING,
            surface=key,
            code=code,
            name=(profile.properties.get(code) or record).name,
            summary=(
                "a value the profile records as load-bearing for this surface "
                "differs; it encodes what the surface is, not body state"
            ),
            expected=expected_value,
            observed=record.value,
        )


def _compare_property(
    surface: str, expected: PropertyRecord, observed: PropertyRecord
) -> Iterator[Finding]:
    if (
        expected.access is not None
        and observed.access is not None
        and expected.access != observed.access
    ):
        yield Finding(
            kind=FindingKind.ACCESS_CHANGED,
            severity=Severity.INFO,
            surface=surface,
            code=expected.code,
            name=expected.name or observed.name,
            summary=(
                "access differs; the vendor documents this flag as varying "
                "with camera state, so it is reported rather than failed"
            ),
            expected=expected.access,
            observed=observed.access,
        )

    if (
        expected.data_type is not None
        and observed.data_type is not None
        and expected.data_type != observed.data_type
    ):
        yield Finding(
            kind=FindingKind.TYPE_CHANGED,
            severity=Severity.WARNING,
            surface=surface,
            code=expected.code,
            name=expected.name or observed.name,
            summary=(
                "the vendor data type differs; decoding depends on this, so a "
                "change is worth looking at even between bodies"
            ),
            expected=_hex_or_none(expected.data_type),
            observed=_hex_or_none(observed.data_type),
        )

    # A capture that never recorded the advertised constraint says nothing
    # about it, and treating "not captured" as "advertises nothing" would
    # manufacture a difference for every property at once.
    if (
        expected.value_set is not None
        and observed.value_set is not None
        and expected.constraint_differs_from(observed)
    ):
        yield Finding(
            kind=FindingKind.VALUES_CHANGED,
            severity=Severity.INFO,
            surface=surface,
            code=expected.code,
            name=expected.name or observed.name,
            summary=(
                "the advertised values differ; what a property accepts is a "
                "runtime fact of this body, lens and state"
            ),
            expected=_describe_values(expected),
            observed=_describe_values(observed),
        )


def _compare_capabilities(
    surface: str, expected: SurfaceRecord, observation: Observation
) -> Iterator[Finding]:
    observed = observation.capabilities.get(surface)
    if not observed or not expected.capabilities:
        return
    for capability in sorted(set(expected.capabilities) & set(observed)):
        if capability == "extra":
            continue
        want = expected.capabilities[capability]
        got = observed[capability]
        if want != got:
            yield Finding(
                kind=FindingKind.CAPABILITY_DIFFERS,
                severity=Severity.INFO,
                surface=surface,
                summary=f"measured capability {capability!r} differs",
                expected=want,
                observed=got,
            )


def _hex_or_none(value: Optional[int]) -> Optional[str]:
    return None if value is None else f"0x{value:04X}"


def _describe_values(record: PropertyRecord) -> Optional[str]:
    if record.value_set is None:
        return None
    if record.value_set == "range" and len(record.values) == 3:
        minimum, maximum, step = record.values
        return f"range {minimum}..{maximum} step {step}"
    if record.value_set == "enum":
        return f"enum of {len(record.values)}"
    return record.value_set


# -- loading ----------------------------------------------------------------
def profile_names() -> tuple[str, ...]:
    """Profiles shipped with this release."""
    if not os.path.isdir(_PROFILE_DIR):
        return ()
    return tuple(
        sorted(
            os.path.splitext(entry)[0]
            for entry in os.listdir(_PROFILE_DIR)
            if entry.endswith(".json")
        )
    )


def load_profile(name_or_path: str = REFERENCE_PROFILE) -> ConformanceProfile:
    """Load a shipped profile by name, or any profile by path."""
    path = name_or_path
    if os.path.sep not in name_or_path and not name_or_path.endswith(".json"):
        path = os.path.join(_PROFILE_DIR, f"{name_or_path}.json")
    if not os.path.exists(path):
        available = ", ".join(profile_names()) or "none"
        raise FileNotFoundError(
            f"no conformance profile at {path!r}; shipped profiles: {available}"
        )
    with open(path, encoding="utf-8") as handle:
        return ConformanceProfile(json.load(handle))


def reference_profile() -> ConformanceProfile:
    """The measured body this release compares against by default."""
    return load_profile(REFERENCE_PROFILE)
