"""Camera commands.

Known commands are named; unknown commands remain expressible as plain
integers so that vendor functionality newer than this release stays reachable
through the raw layer.
"""

from __future__ import annotations

from enum import Enum
from typing import Union

__all__ = ["Command", "CommandParameter", "CommandLike"]


class Command(Enum):
    """Commands CrSDKPy recognises by name."""

    #: Full shutter release (S2) only. Does **not** assert the half-press
    #: stage, so on its own it performs no autofocus.
    RELEASE = "release"
    #: Combined half-press and release. Ungated: the exposure is committed
    #: before focus can be inspected, so it is not used by the high-level
    #: autofocus capture path.
    S1_AND_RELEASE = "s1_and_release"
    MOVIE_RECORD = "movie_record"
    CANCEL_SHOOTING = "cancel_shooting"

    def __str__(self) -> str:
        return self.value


class CommandParameter(Enum):
    """Button transition for a command.

    The vendor exposes exactly two values; there is no half-press parameter.
    A ``DOWN`` must always be followed by an ``UP``.
    """

    UP = 0
    DOWN = 1

    def __str__(self) -> str:
        return self.name.lower()


#: A command may be named or a raw vendor integer.
CommandLike = Union[Command, int]
