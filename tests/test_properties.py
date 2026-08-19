"""Generic property model, including codes CrSDKPy does not name."""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy.simulator import profiles as P


def test_property_code_wraps_any_integer() -> None:
    code = crsdkpy.PropertyCode(0x0581)
    assert code.code == 0x0581
    assert code.name is None
    assert not code.known
    assert code == 0x0581
    assert int(code) == 0x0581
    assert "0x0581" in repr(code)


def test_property_code_names_known_values() -> None:
    code = crsdkpy.PropertyCode(P.CODE_FOCUS_INDICATION)
    assert code.known
    assert code.name == "FocusIndication"


def test_property_code_is_hashable_with_ints() -> None:
    mapping = {crsdkpy.PropertyCode(0x1234): "value"}
    assert mapping[0x1234] == "value"  # type: ignore[index]


def test_property_code_rejects_negative() -> None:
    with pytest.raises(ValueError):
        crsdkpy.PropertyCode(-1)


def test_register_property_name_is_advisory() -> None:
    crsdkpy.register_property_name(0x7F01, "SimulatedFutureProperty")
    assert crsdkpy.PropertyCode(0x7F01).name == "SimulatedFutureProperty"


def test_snapshot_is_a_mapping(session: crsdkpy.Session) -> None:
    snapshot = session.properties.snapshot()
    assert len(snapshot) > 0
    assert P.CODE_ISO in snapshot
    prop = snapshot[P.CODE_ISO]
    assert prop.value == 100
    assert prop.code == P.CODE_ISO
    assert list(snapshot.codes()) == sorted(snapshot.codes())


def test_unknown_codes_are_first_class(session: crsdkpy.Session) -> None:
    """Hardware reported codes absent from the vendor's own enumeration."""
    snapshot = session.properties.snapshot()
    unknown = snapshot.unknown_codes()
    assert crsdkpy.PropertyCode(0x0581) in unknown
    assert crsdkpy.PropertyCode(0x0582) in unknown
    # They are fully usable despite having no name.
    prop = session.properties.get(0x0581)
    assert prop.code == 0x0581
    assert prop.name is None


def test_property_count_differs_by_mode(camera: crsdkpy.Camera) -> None:
    """Property count is mode-dependent and must never be a health check."""
    with camera.open("remote") as remote:
        remote_codes = set(int(c) for c in remote.properties.snapshot().codes())
    with camera.open("remote_transfer") as transfer:
        transfer_codes = set(int(c) for c in transfer.properties.snapshot().codes())

    assert transfer_codes < remote_codes
    assert remote_codes - transfer_codes == {0x0581, 0x0582}


def test_read_only_property_rejects_write(session: crsdkpy.Session) -> None:
    prop = session.properties.get(P.CODE_FOCUS_INDICATION)
    assert not prop.writable
    with pytest.raises(crsdkpy.UnsupportedOperationError):
        session.properties.set(P.CODE_FOCUS_INDICATION, 0)


def test_writable_property_round_trips(session: crsdkpy.Session) -> None:
    session.properties.set(P.CODE_ISO, 640)
    assert session.properties.get(P.CODE_ISO).value == 640


def test_allowed_values_reported(session: crsdkpy.Session) -> None:
    prop = session.properties.get(P.CODE_S1)
    assert prop.allowed_values == (P.LOCK_UNLOCKED, P.LOCK_LOCKED)
    assert prop.accepts(P.LOCK_LOCKED)
    assert not prop.accepts(99)


def test_property_without_constraints_accepts_anything(
    session: crsdkpy.Session,
) -> None:
    prop = session.properties.get(P.CODE_ISO)
    assert prop.allowed_values == ()
    assert prop.accepts(12800)


def test_missing_property_raises(session: crsdkpy.Session) -> None:
    with pytest.raises(crsdkpy.PropertyNotSupportedError) as excinfo:
        session.properties.get(0xABCD)
    assert excinfo.value.code == 0xABCD


def test_property_range_contains() -> None:
    rng = crsdkpy.PropertyRange(minimum=0, maximum=10, step=2)
    assert rng.contains(4)
    assert not rng.contains(5)
    assert not rng.contains(12)


def test_physical_change_arrives_as_coalesced_batch(
    sdk: crsdkpy.SDK, session: crsdkpy.Session
) -> None:
    """A physical control produces one batch, plus a possible straggler."""
    codes = [P.CODE_FOCUS_MODE, P.CODE_ISO, P.CODE_DRIVE_MODE]
    sdk.backend.simulate_physical_property_change(
        session._id, codes, values={P.CODE_FOCUS_MODE: 0x0001}
    )
    batches = [
        e
        for e in session.drain_events()
        if isinstance(e, crsdkpy.PropertyChangedEvent)
    ]
    assert len(batches) == 1
    assert len(batches[0].codes) == 3
    assert P.CODE_FOCUS_MODE in batches[0]
    # The new value is readable immediately, no settle delay required.
    assert session.properties.get(P.CODE_FOCUS_MODE).value == 0x0001


def test_straggler_property_event_arrives_later(
    sdk: crsdkpy.SDK, session: crsdkpy.Session
) -> None:
    codes = [P.CODE_FOCUS_MODE, P.CODE_ISO]
    sdk.backend.simulate_physical_property_change(session._id, codes)
    first = session.drain_events()
    assert any(isinstance(e, crsdkpy.PropertyChangedEvent) for e in first)
    # The straggler is not in the first batch; it lands ~100 ms later.
    later = session.drain_events(timeout_ms=500)
    straggler = [e for e in later if isinstance(e, crsdkpy.PropertyChangedEvent)]
    assert straggler
    assert len(straggler[0].codes) == 1


def test_future_profile_exposes_unnamed_codes() -> None:
    with make_sdk(profile="future_unknown") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            snapshot = session.properties.snapshot()
            unknown = {int(c) for c in snapshot.unknown_codes()}
            assert 0x7FFE in unknown
            assert session.properties.get(0x7FFE).code == 0x7FFE
