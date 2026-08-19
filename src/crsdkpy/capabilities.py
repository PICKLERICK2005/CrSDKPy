"""Capability model.

Capabilities are discovered at runtime, never inferred from a model name.
The first characterized body proved that the same camera exposes different
capabilities depending on control mode *and* on still destination, and that
being allowed to configure a feature does not mean the feature delivers.

Unrecognised capability names are preserved in :attr:`extra` so that a backend
describing a feature this release has never heard of stays usable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from .enums import SessionMode, StillDestination

__all__ = ["CameraCapabilities", "SessionCapabilities"]


@dataclass(frozen=True)
class CameraCapabilities:
    """Broad, stable capabilities of the physical device.

    These describe what the body can do at all, not what the current session
    can do. A capability true here may still be unavailable in a given mode.
    """

    still_capture: bool = False
    #: Whether the half-press / autofocus stage exists as a separate control.
    autofocus_s1: bool = False
    video: bool = False
    #: Modes the camera can be opened in.
    modes: frozenset[SessionMode] = field(default_factory=frozenset)
    #: Destinations the camera accepts, where selectable.
    destinations: frozenset[StillDestination] = field(default_factory=frozenset)
    #: Whether live view is available in *some* mode.
    live_view_any_mode: bool = False
    #: Whether the content index is available in *some* mode.
    content_index_any_mode: bool = False
    extra: Mapping[str, bool] = field(default_factory=dict)

    def supports_mode(self, mode: SessionMode) -> bool:
        return mode in self.modes

    def supports_destination(self, destination: StillDestination) -> bool:
        return destination in self.destinations

    def get(self, name: str, default: bool = False) -> bool:
        """Look up a capability by name, including unrecognised ones."""
        if name in self.extra:
            return bool(self.extra[name])
        value = getattr(self, name, None)
        return bool(value) if isinstance(value, bool) else default

    def __contains__(self, name: str) -> bool:
        return name in self.extra or isinstance(getattr(self, name, None), bool)


@dataclass(frozen=True)
class SessionCapabilities:
    """What this specific open session can actually do.

    Derived from the camera, the control mode, the still destination, and any
    runtime observation the backend has made.
    """

    mode: SessionMode
    destination: StillDestination

    still_capture: bool = False
    autofocus_s1: bool = False
    video: bool = False
    live_view: bool = False
    content_index: bool = False
    thumbnail: bool = False
    screennail: bool = False
    #: Whether the postview *configuration* call is accepted.
    postview_configuration: bool = False
    #: Whether a postview is actually *delivered* after a capture. Deliberately
    #: separate: hardware accepted configuration without delivering, and
    #: delivered without accepting configuration.
    postview_delivery: bool = False
    raw_commands: bool = True
    extra: Mapping[str, bool] = field(default_factory=dict)

    def get(self, name: str, default: bool = False) -> bool:
        if name in self.extra:
            return bool(self.extra[name])
        value = getattr(self, name, None)
        return bool(value) if isinstance(value, bool) else default

    def __contains__(self, name: str) -> bool:
        return name in self.extra or isinstance(getattr(self, name, None), bool)

    def names(self) -> Iterator[str]:
        for key, value in vars(self).items():
            if key == "extra" or not isinstance(value, bool):
                continue
            yield key
        yield from self.extra

    def missing(self) -> Iterator[str]:
        for name in self.names():
            if not self.get(name):
                yield name
