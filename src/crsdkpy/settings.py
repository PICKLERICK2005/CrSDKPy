"""Live typed views over the generic camera property layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .errors import PropertyNotSupportedError, UnsupportedOperationError
from .properties import Property, PropertyCode, PropertyRange

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

__all__ = [
    "STILL_TRANSFER_ORIGINAL",
    "STILL_TRANSFER_SMALL",
    "Drive",
    "Exposure",
    "Setting",
    "StillTransfer",
    "WhiteBalance",
]

#: Documented CrPropertyStillImageTransSize values. Original is the
#: captured file; SmallSize is the vendor's "Small Size JPEG/HEIF".
STILL_TRANSFER_ORIGINAL = 0
STILL_TRANSFER_SMALL = 1


class Setting:
    """One live camera setting backed by :class:`Property`.

    The wrapper does not cache or reinterpret vendor values. Unknown current
    integers remain integers. Advertised lists and ranges validate requested
    writes only.
    """

    def __init__(self, session: Session, code: int) -> None:
        self._session = session
        self.code = PropertyCode(code)

    @property
    def descriptor(self) -> Optional[Property]:
        try:
            return self._session.properties.get(self.code)
        except PropertyNotSupportedError:
            return None

    @property
    def available(self) -> bool:
        return self.descriptor is not None

    @property
    def value(self) -> Any:
        prop = self.descriptor
        return prop.value if prop is not None else None

    @property
    def writable(self) -> bool:
        prop = self.descriptor
        return bool(prop and prop.writable)

    @property
    def allowed_values(self) -> tuple[Any, ...]:
        prop = self.descriptor
        return prop.allowed_values if prop is not None else ()

    @property
    def value_range(self) -> Optional[PropertyRange]:
        prop = self.descriptor
        return prop.value_range if prop is not None else None

    @property
    def values_undecoded(self) -> bool:
        prop = self.descriptor
        return bool(prop and prop.metadata.get("values_undecoded"))

    @property
    def validation_available(self) -> bool:
        prop = self.descriptor
        return bool(
            prop
            and not prop.metadata.get("values_undecoded")
            and (prop.allowed_values or prop.value_range is not None)
        )

    def accepts(self, value: Any) -> bool:
        prop = self.descriptor
        return bool(prop and prop.accepts(value))

    def set(self, value: Any) -> None:
        prop = self.descriptor
        if prop is None:
            raise UnsupportedOperationError(
                f"camera does not expose setting {self.code}",
                capability=f"property.0x{int(self.code):04x}",
                operation="setting.set",
            )
        if not prop.writable:
            raise UnsupportedOperationError(
                f"setting {self.code} is read-only",
                capability="property.write",
                operation="setting.set",
            )
        if not prop.accepts(value):
            raise ValueError(
                f"value {value!r} is outside the constraints for {self.code}"
            )
        self._session.properties.set(self.code, value)

    def __repr__(self) -> str:
        if not self.available:
            return f"Setting({self.code}, unavailable)"
        return f"Setting({self.code}, value={self.value!r}, writable={self.writable})"


class Exposure:
    """Documented exposure-related property settings."""

    def __init__(self, session: Session) -> None:
        self.f_number = Setting(session, 0x0100)
        self.compensation = Setting(session, 0x0101)
        self.shutter_speed = Setting(session, 0x0103)
        self.iso = Setting(session, 0x0104)
        self.program = Setting(session, 0x0105)


class WhiteBalance:
    """Documented white-balance mode setting."""

    def __init__(self, session: Session) -> None:
        self.mode = Setting(session, 0x0108)


class Drive:
    """Documented drive-mode setting, without capture orchestration."""

    def __init__(self, session: Session) -> None:
        self.mode = Setting(session, 0x010E)


class StillTransfer:
    """What a host-bound still is transferred as.

    The vendor documents two values: ``Original`` (0) and ``SmallSize`` (1,
    "Small Size JPEG/HEIF"). The reference body reported SmallSize, which is
    why a host-bound capture arrived downscaled rather than as the captured
    file. Whether that body honours ``Original`` has not been tested on
    hardware, so this exposes the setting and claims nothing about the result.
    """

    def __init__(self, session: Session) -> None:
        self.size = Setting(session, 0x0506)

    @property
    def is_original(self) -> Optional[bool]:
        """True/False for the documented values, ``None`` otherwise.

        ``None`` also covers a camera that does not expose the setting, since
        an absent setting says nothing about what will be delivered.
        """
        value = self.size.value
        if value == STILL_TRANSFER_ORIGINAL:
            return True
        if value == STILL_TRANSFER_SMALL:
            return False
        return None
