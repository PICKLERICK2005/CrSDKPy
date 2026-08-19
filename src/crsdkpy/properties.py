"""Generic property model.

The vendor SDK exposes numeric property codes, and hardware has been observed
reporting codes that do not appear in the vendor's own property enumeration.
Unknown codes are therefore first-class: :class:`PropertyCode` wraps any
integer, naming it only if CrSDKPy happens to recognise it.

Nothing here treats the number of properties as meaningful. Property count
varies with control mode on a single body and must never be a health check.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from .enums import PropertyAccess, PropertyValueType

__all__ = [
    "KNOWN_PROPERTY_NAMES",
    "Property",
    "PropertyCode",
    "PropertyRange",
    "PropertySnapshot",
    "register_property_name",
]


# A deliberately partial registry. It exists to make debugging readable, not to
# define what is valid. Absence from this table never restricts anything.
KNOWN_PROPERTY_NAMES: dict[int, str] = {
    # Verified against the vendor's property enumeration. Deliberately partial:
    # a camera reports hundreds of codes and most have no name here, which is
    # fine. A wrong name would be worse than none, so only checked values live
    # in this table.
    0x0001: "S1",
    0x0002: "AEL",
    0x0100: "FNumber",
    0x0103: "ShutterSpeed",
    0x0104: "IsoSensitivity",
    0x0106: "FileType",
    0x0109: "FocusMode",
    0x010E: "DriveMode",
    0x0119: "StillImageStoreDestination",
    0x011A: "PriorityKeySettings",
    0x0179: "FocusModeSetting",
    0x0194: "FollowFocusPositionSetting",
    0x0260: "PreAF",
    0x0500: "S2",
    0x0506: "StillImageTransSize",
    0x0705: "RecordingState",
    0x0707: "FocusIndication",
    0x0766: "FocusPositionCurrentValue",
    0x0767: "FocusDrivingStatus",
    0x078B: "CameraErrorCautionStatus",
    0x078C: "SystemErrorCautionStatus",
    0x0797: "TrackingOnAndAFOnEnableStatus",
    0x0799: "MeteredManualLevel",
}


def register_property_name(code: int, name: str) -> None:
    """Teach CrSDKPy a symbolic name for a numeric property code.

    Useful for application code or a future backend that knows more codes than
    this release does. Registration is advisory only.
    """
    KNOWN_PROPERTY_NAMES[int(code)] = name


class PropertyCode:
    """A numeric property code, named only if recognised.

    Compares and hashes as its integer value, so a :class:`PropertyCode` and a
    plain ``int`` are interchangeable as mapping keys.

    >>> PropertyCode(0x0581).known
    False
    >>> PropertyCode(0x0581) == 0x0581
    True
    """

    __slots__ = ("_code",)

    def __init__(self, code: Union[int, PropertyCode]) -> None:
        if isinstance(code, PropertyCode):
            code = code._code
        code = int(code)
        if code < 0:
            raise ValueError(f"property code must be non-negative, got {code}")
        self._code = code

    @property
    def code(self) -> int:
        return self._code

    @property
    def name(self) -> Optional[str]:
        """Symbolic name, or ``None`` when CrSDKPy does not recognise the code."""
        return KNOWN_PROPERTY_NAMES.get(self._code)

    @property
    def known(self) -> bool:
        return self._code in KNOWN_PROPERTY_NAMES

    def __int__(self) -> int:
        return self._code

    def __index__(self) -> int:
        return self._code

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PropertyCode):
            return self._code == other._code
        if isinstance(other, int):
            return self._code == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._code)

    def __lt__(self, other: object) -> bool:
        if isinstance(other, PropertyCode):
            return self._code < other._code
        if isinstance(other, int):
            return self._code < other
        return NotImplemented

    def __repr__(self) -> str:
        name = self.name
        if name:
            return f"PropertyCode(0x{self._code:04X}, {name!r})"
        return f"PropertyCode(0x{self._code:04X})"

    def __str__(self) -> str:
        name = self.name
        return f"{name}(0x{self._code:04X})" if name else f"0x{self._code:04X}"


@dataclass(frozen=True)
class PropertyRange:
    """Inclusive numeric range with an optional step."""

    minimum: int
    maximum: int
    step: Optional[int] = None

    def contains(self, value: int) -> bool:
        if not (self.minimum <= value <= self.maximum):
            return False
        if self.step:
            return (value - self.minimum) % self.step == 0
        return True


@dataclass(frozen=True)
class Property:
    """A single property as reported by the camera at one moment."""

    code: PropertyCode
    value: Any = None
    value_type: PropertyValueType = PropertyValueType.UNKNOWN
    access: PropertyAccess = PropertyAccess.UNKNOWN
    allowed_values: tuple[Any, ...] = ()
    value_range: Optional[PropertyRange] = None
    #: Backend-supplied detail that has no generic meaning. Never interpreted
    #: by CrSDKPy; present so a backend can round-trip vendor specifics.
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        return self.access.writable

    @property
    def name(self) -> Optional[str]:
        return self.code.name

    def accepts(self, value: Any) -> bool:
        """Whether *value* satisfies the reported constraints.

        A property with neither an allowed-value list nor a range accepts
        anything; absence of constraints is not evidence of restriction.
        """
        if self.allowed_values:
            return value in self.allowed_values
        if self.value_range is not None and isinstance(value, int):
            return self.value_range.contains(value)
        return True

    def __repr__(self) -> str:
        return (
            f"Property({self.code}, value={self.value!r}, "
            f"access={self.access.value})"
        )


class PropertySnapshot(Mapping[PropertyCode, Property]):
    """An immutable point-in-time view of the camera's properties.

    Behaves as a mapping keyed by code; plain integers work as keys too.
    """

    __slots__ = ("_by_code", "_timestamp_ms")

    def __init__(
        self,
        properties: Sequence[Property],
        *,
        timestamp_ms: int = 0,
    ) -> None:
        self._by_code: dict[int, Property] = {int(p.code): p for p in properties}
        self._timestamp_ms = timestamp_ms

    @property
    def timestamp_ms(self) -> int:
        return self._timestamp_ms

    def __getitem__(self, key: Union[int, PropertyCode]) -> Property:
        return self._by_code[int(key)]

    def __iter__(self) -> Iterator[PropertyCode]:
        return (PropertyCode(c) for c in sorted(self._by_code))

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, (int, PropertyCode)):
            return int(key) in self._by_code
        return False

    def codes(self) -> tuple[PropertyCode, ...]:
        return tuple(PropertyCode(c) for c in sorted(self._by_code))

    def value_of(self, key: Union[int, PropertyCode], default: Any = None) -> Any:
        prop = self._by_code.get(int(key))
        return default if prop is None else prop.value

    def unknown_codes(self) -> tuple[PropertyCode, ...]:
        """Codes the camera reported that CrSDKPy has no name for."""
        return tuple(
            PropertyCode(c)
            for c in sorted(self._by_code)
            if c not in KNOWN_PROPERTY_NAMES
        )

    def __repr__(self) -> str:
        return f"PropertySnapshot({len(self._by_code)} properties)"
