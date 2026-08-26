"""Composable simulator scenarios.

A :class:`Scenario` is orthogonal to a profile: the profile says what the
camera *is*, the scenario says what *happens* during a run. Each knob below
corresponds to a behaviour actually observed on hardware, or to a failure a
robust client must survive.

Scenarios compose with :meth:`Scenario.replace`, so a test can start from a
named scenario and vary one axis.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

__all__ = ["AfOutcome", "FocusChannel", "Scenario", "SCENARIOS", "get_scenario",
           "scenario_names"]


class AfOutcome(Enum):
    """What autofocus does when the half-press stage is asserted."""

    FOCUS = "focus"
    #: AF runs and gives up. No focused state is ever reported.
    NO_LOCK = "no_lock"
    #: AF-C passes through a tracking state before focusing. Treating tracking
    #: as success would release early.
    TRACKING_THEN_FOCUS = "tracking_then_focus"
    #: AF never reports anything at all.
    SILENT = "silent"


class FocusChannel(Enum):
    """Which asynchronous focus channel reports first."""

    PROPERTY = "property"
    STATUS = "status"


@dataclass(frozen=True)
class Scenario:
    """Behavioural configuration for one simulated run."""

    name: str = "nominal"

    # -- autofocus ---------------------------------------------------------
    af_outcome: AfOutcome = AfOutcome.FOCUS
    #: Which channel leads. Hardware showed both orderings across runs.
    af_leading_channel: FocusChannel = FocusChannel.STATUS
    #: How long the trailing channel keeps reporting the older state. A large
    #: value reproduces the observed transient disagreement.
    af_channel_skew_ms: Optional[int] = None
    #: The focus indication is already focused and emits no change event, so
    #: only a direct read can discover it.
    af_sticky: bool = False
    #: Whether a successful release clears the half-press stage by itself.
    release_clears_s1: bool = True

    # -- capture -----------------------------------------------------------
    #: Commands are accepted but no exposure ever happens.
    capture_without_exposure: bool = False
    #: Extra delay before durable content appears.
    content_extra_delay_ms: int = 0
    #: Increment between content identifiers. Values above 1 reproduce the
    #: observed non-contiguous identifiers.
    content_id_step: int = 1
    #: Serve the *previous* capture's preview, so a client that does not check
    #: identity is caught out.
    stale_preview: bool = False

    # -- original transfer ------------------------------------------------
    transfer_outcome: str = "ok"
    transfer_delay_ms: int = 500

    # -- focus positioning ------------------------------------------------
    focus_move_delay_ms: int = 200
    focus_stuck_driving: bool = False
    focus_stuck_after_moves: Optional[int] = None
    focus_unknown_driving_status: Optional[int] = None
    focus_position_available: bool = True
    focus_range_available: bool = True
    focus_position_writable: bool = True
    focus_session_loss_ms: Optional[int] = None

    # -- live view ---------------------------------------------------------
    #: The info call succeeds while reporting a zero buffer.
    live_view_info_ok_but_empty: bool = False
    #: The frame fetch itself fails hard.
    live_view_fetch_fails: bool = False

    # -- transport ---------------------------------------------------------
    #: Begin a reconnect this many ms after the session opens.
    reconnect_after_ms: Optional[int] = None
    #: Recover without ever reporting a disconnect, as observed.
    reconnect_without_disconnect: bool = True
    #: Emit a second connected event on recovery.
    duplicate_connected_event: bool = True

    # -- misc --------------------------------------------------------------
    #: Reject commands with a busy error.
    busy: bool = False
    #: Emit a straggler property batch after a physical change.
    property_stragglers: bool = True
    unavailable_property_codes: tuple[int, ...] = ()
    read_only_property_codes: tuple[int, ...] = ()
    property_value_overrides: tuple[tuple[int, int], ...] = ()
    values_undecoded_property_codes: tuple[int, ...] = ()
    #: Emit an event code with no typed representation.
    emit_unknown_events: bool = False

    # -- lens --------------------------------------------------------------
    #: What the camera reports for LensInformationEnableStatus. The first
    #: characterized body reported Disable with a prime attached, so Disable
    #: is the default and Enable is the case that has never met hardware.
    lens_information_enabled: bool = False
    #: Vendor warning the request answers with. Defaults to success so the
    #: refusal paths have to be asked for explicitly.
    lens_request_outcome: int = 0x0002003B
    #: Rows the prepared table contains. Zero means the vendor's "no
    #: applicable information", which is what a prime lens produces.
    lens_distance_rows: int = 0
    #: Withhold the request outcome entirely, so the wait has to time out.
    lens_request_silent: bool = False

    def replace(self, **changes: object) -> Scenario:
        """Return a copy with *changes* applied."""
        return replace(self, **changes)  # type: ignore[arg-type]


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(name="nominal"),
        Scenario(name="af_property_leads", af_leading_channel=FocusChannel.PROPERTY),
        Scenario(name="af_status_leads", af_leading_channel=FocusChannel.STATUS),
        Scenario(
            name="af_channel_disagreement",
            af_leading_channel=FocusChannel.STATUS,
            af_channel_skew_ms=100,
        ),
        Scenario(name="af_tracking_first", af_outcome=AfOutcome.TRACKING_THEN_FOCUS),
        Scenario(name="af_sticky", af_sticky=True),
        Scenario(name="af_no_lock", af_outcome=AfOutcome.NO_LOCK),
        Scenario(name="af_silent", af_outcome=AfOutcome.SILENT),
        Scenario(
            name="s1_not_cleared_by_release",
            release_clears_s1=False,
        ),
        Scenario(name="capture_without_exposure", capture_without_exposure=True),
        Scenario(
            name="lens_with_distance_table",
            lens_information_enabled=True,
            lens_distance_rows=6,
        ),
        Scenario(
            name="lens_request_refused",
            lens_information_enabled=True,
            lens_request_outcome=0x0002003D,
        ),
        Scenario(name="delayed_content", content_extra_delay_ms=3_000),
        Scenario(name="non_contiguous_content", content_id_step=2),
        Scenario(name="stale_preview", stale_preview=True),
        Scenario(name="transfer_busy", transfer_outcome="busy"),
        Scenario(name="transfer_failed", transfer_outcome="failed"),
        Scenario(name="transfer_storage_full", transfer_outcome="storage_full"),
        Scenario(name="focus_move_stuck", focus_stuck_driving=True),
        Scenario(name="focus_unknown_status", focus_unknown_driving_status=99),
        Scenario(name="focus_unavailable", focus_position_available=False),
        Scenario(name="focus_read_only", focus_position_writable=False),
        Scenario(name="live_view_empty_info", live_view_info_ok_but_empty=True),
        Scenario(name="live_view_fetch_fails", live_view_fetch_fails=True),
        Scenario(name="reconnect", reconnect_after_ms=1_000),
        Scenario(name="busy", busy=True),
        Scenario(name="unknown_events", emit_unknown_events=True),
    )
}


def scenario_names() -> Sequence[str]:
    return tuple(sorted(SCENARIOS))


def get_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"unknown scenario {name!r}; available: {', '.join(scenario_names())}"
        ) from None
