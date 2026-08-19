"""Device status an application needs but should not decode itself.

Battery and media are the two things almost every integration reads, and both
are vendor-encoded enumerations behind numeric property codes. Exposing them
as typed values keeps those codes out of application code, which is the whole
point of the capability model: an application asks what it wants to know, not
where a particular camera happens to keep it.

Every field is optional. A camera that does not report one of these is normal
and must not be a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["BatteryStatus", "StorageSlot"]


@dataclass(frozen=True)
class BatteryStatus:
    """Charge state, as far as the camera reports it.

    ``percent`` and ``level`` are separate readings, not two views of one:
    some bodies report a percentage, some only a coarse level, some both.
    """

    #: Remaining charge 0-100, when the camera reports a percentage.
    percent: Optional[int] = None
    #: Coarse level normalised to 0.0-1.0, when the camera reports one.
    level: Optional[float] = None
    #: Running from USB power rather than a battery.
    usb_power: bool = False
    #: Vendor level value before normalisation, for diagnostics.
    raw_level: Optional[int] = None

    @property
    def known(self) -> bool:
        return self.percent is not None or self.level is not None or self.usb_power

    def __repr__(self) -> str:
        if self.usb_power and self.percent is None:
            return "BatteryStatus(usb_power)"
        if self.percent is not None:
            return f"BatteryStatus({self.percent}%)"
        if self.level is not None:
            return f"BatteryStatus(level={self.level:.2f})"
        return "BatteryStatus(unknown)"


@dataclass(frozen=True)
class StorageSlot:
    """One media slot.

    ``writable`` is deliberately conservative: anything other than a plainly
    healthy card reads as not writable, because a client deciding whether it
    can shoot should not have to enumerate every vendor error state.
    """

    slot: int
    #: Normalised state, e.g. ``"ok"``, ``"no_card"``, ``"card_error"``.
    status: str = "unknown"
    remaining_shots: Optional[int] = None
    remaining_seconds: Optional[int] = None
    raw_status: Optional[int] = None

    @property
    def present(self) -> bool:
        return self.status not in ("no_card", "unknown")

    @property
    def writable(self) -> bool:
        return self.status == "ok"

    def __repr__(self) -> str:
        return (
            f"StorageSlot({self.slot}, {self.status}, "
            f"shots={self.remaining_shots})"
        )
