"""Deterministic camera simulator.

A first-class feature, not a test fixture: it is intended to be good enough for
day-to-day development without hardware attached.
"""

from __future__ import annotations

from .backend import SimulatedBackend
from .profiles import (
    PROFILES,
    CameraProfile,
    ModeProfile,
    PreviewSpec,
    Timings,
    get_profile,
    profile_names,
)
from .scenarios import (
    SCENARIOS,
    AfOutcome,
    FocusChannel,
    Scenario,
    get_scenario,
    scenario_names,
)

__all__ = [
    "AfOutcome",
    "CameraProfile",
    "FocusChannel",
    "ModeProfile",
    "PROFILES",
    "PreviewSpec",
    "SCENARIOS",
    "Scenario",
    "SimulatedBackend",
    "Timings",
    "get_profile",
    "get_scenario",
    "profile_names",
    "scenario_names",
]
