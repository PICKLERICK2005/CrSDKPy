"""Shared fixtures.

Every test runs on the simulator's virtual clock, so long vendor latencies
cost no wall-clock time and nothing is timing-dependent.
"""

from __future__ import annotations

from typing import Any, Optional
from collections.abc import Iterator

import pytest

import crsdkpy
from crsdkpy.simulator import Scenario, get_scenario


def make_sdk(
    profile: Any = "fx3a",
    scenario: Optional[Any] = None,
    **kwargs: Any,
) -> crsdkpy.SDK:
    if isinstance(scenario, str):
        scenario = get_scenario(scenario)
    return crsdkpy.SDK(
        backend="simulator", profile=profile, scenario=scenario, **kwargs
    )


@pytest.fixture
def sdk() -> Iterator[crsdkpy.SDK]:
    with make_sdk() as instance:
        yield instance


@pytest.fixture
def camera(sdk: crsdkpy.SDK) -> crsdkpy.Camera:
    return sdk.discover()[0]


@pytest.fixture
def session(camera: crsdkpy.Camera) -> Iterator[crsdkpy.Session]:
    with camera.open(crsdkpy.SessionMode.REMOTE) as opened:
        yield opened


@pytest.fixture
def transfer_session(camera: crsdkpy.Camera) -> Iterator[crsdkpy.Session]:
    with camera.open(crsdkpy.SessionMode.REMOTE_TRANSFER) as opened:
        yield opened


@pytest.fixture
def nominal_scenario() -> Scenario:
    return Scenario()
