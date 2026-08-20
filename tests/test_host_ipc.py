"""Host process, IPC protocol and error transport.

Everything here runs against the pure-Python fake host, so no native build, no
vendor SDK and no camera are required. That is the main reason the transport is
the child's stdin/stdout pipes.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

import crsdkpy
from crsdkpy.backend import _cabi, _ipc
from crsdkpy.backend.host import HostBackend, HostState, find_host_executable

FAKE_HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_host.py")


def make_backend(behaviour: str = "normal", **kwargs) -> HostBackend:
    return HostBackend(
        command=[sys.executable, FAKE_HOST, behaviour],
        adapter_dir=os.path.dirname(FAKE_HOST),
        **kwargs,
    )


# -- framing ---------------------------------------------------------------
def test_frame_round_trip() -> None:
    payload = _ipc.encode(_ipc.MSG_REQUEST, 42, b"meta", b"blob")
    message_type, request_id, meta_len, blob_len = _ipc.decode(
        payload[: _ipc.HEADER_SIZE]
    )
    assert message_type == _ipc.MSG_REQUEST
    assert request_id == 42
    assert meta_len == 4
    assert blob_len == 4


def test_header_is_twenty_four_bytes() -> None:
    assert _ipc.HEADER_SIZE == 24


def test_bad_magic_is_rejected() -> None:
    bad = b"\x00" * _ipc.HEADER_SIZE
    with pytest.raises(_ipc.ProtocolError, match="magic"):
        _ipc.decode(bad)


def test_protocol_version_mismatch_is_rejected() -> None:
    import struct

    header = struct.pack("<IHHIIII", _ipc.MAGIC, 99, _ipc.MSG_REQUEST, 1, 0, 0, 0)
    with pytest.raises(_ipc.ProtocolError, match="protocol major"):
        _ipc.decode(header)


def test_implausible_length_is_rejected() -> None:
    import struct

    header = struct.pack(
        "<IHHIIII", _ipc.MAGIC, _ipc.VERSION_MAJOR, _ipc.MSG_REQUEST, 1, 0, 1 << 30, 0
    )
    with pytest.raises(_ipc.ProtocolError, match="implausible"):
        _ipc.decode(header)


def test_clean_eof_returns_none() -> None:
    assert _ipc.read_frame(io.BytesIO(b"")) is None


def test_truncated_frame_is_an_error_not_an_eof() -> None:
    """A half-written frame means the peer died; that is not a clean close."""
    whole = _ipc.encode(_ipc.MSG_REQUEST, 1, b"12345678")
    with pytest.raises(_ipc.ProtocolError, match="mid-frame"):
        _ipc.read_frame(io.BytesIO(whole[:-3]))


def test_large_binary_payload_frames_correctly() -> None:
    """Image buffers must travel as themselves, not re-encoded."""
    blob = os.urandom(3 * 1024 * 1024)
    payload = _ipc.encode(_ipc.MSG_RESPONSE, 7, b"m", blob)
    message_type, request_id, meta, body = _ipc.read_frame(io.BytesIO(payload))
    assert message_type == _ipc.MSG_RESPONSE
    assert request_id == 7
    assert meta == b"m"
    assert body == blob


def test_oversized_payload_is_refused() -> None:
    with pytest.raises(_ipc.ProtocolError, match="too large"):
        _ipc.encode(_ipc.MSG_RESPONSE, 1, b"x" * (_ipc.MAX_META + 1))


def test_struct_sizes_match_the_c_layout() -> None:
    import ctypes

    # Fixed layouts; a change here must bump the protocol major version.
    assert ctypes.sizeof(_ipc.HeaderStruct) == 24
    assert ctypes.sizeof(_ipc.RequestStruct) == 232
    assert ctypes.sizeof(_ipc.ResponseStruct) == 544


# -- handshake --------------------------------------------------------------
def test_handshake_reports_versions() -> None:
    backend = make_backend()
    backend.start()
    try:
        ack = backend.handshake
        assert ack is not None
        assert ack.protocol_major == _ipc.VERSION_MAJOR
        assert ack.abi_major == _cabi.ABI_VERSION_MAJOR
        assert b"fake_host" in ack.host_build
    finally:
        backend.shutdown()


def test_incompatible_protocol_major_is_refused() -> None:
    backend = make_backend("bad_protocol")
    with pytest.raises(crsdkpy.NativeBackendError, match="IPC protocol"):
        backend.start()
    assert backend.state in (HostState.CRASHED, HostState.STOPPED, HostState.STARTING)


def test_incompatible_abi_major_is_refused() -> None:
    backend = make_backend("bad_abi")
    with pytest.raises(crsdkpy.NativeBackendError, match="C ABI major"):
        backend.start()


def test_host_dying_during_handshake_is_reported() -> None:
    backend = make_backend("die_on_hello")
    with pytest.raises(crsdkpy.BackendError, match="ended during"):
        backend.start()


def test_garbage_stream_is_reported_as_malformed() -> None:
    backend = make_backend("garbage")
    with pytest.raises(crsdkpy.BackendError, match="malformed"):
        backend.start()


# -- lifecycle --------------------------------------------------------------
def test_lifecycle_states() -> None:
    backend = make_backend()
    assert backend.state == HostState.NOT_STARTED
    backend.start()
    assert backend.state == HostState.READY
    backend.shutdown()
    assert backend.state == HostState.STOPPED


def test_start_and_shutdown_are_idempotent() -> None:
    backend = make_backend()
    backend.start()
    backend.start()
    backend.shutdown()
    backend.shutdown()
    assert backend.state == HostState.STOPPED


def test_operations_before_start_are_refused() -> None:
    backend = make_backend()
    with pytest.raises(crsdkpy.NativeBackendError):
        backend.enumerate_cameras()


def test_sdk_missing_is_reported_on_start() -> None:
    backend = make_backend("sdk_missing")
    with pytest.raises(crsdkpy.SDKNotFoundError, match="vendor runtime"):
        backend.start()


def test_missing_host_executable_is_actionable() -> None:
    with pytest.raises(crsdkpy.SDKNotFoundError) as excinfo:
        HostBackend(host_path="/definitely/not/a/host.exe")
    message = str(excinfo.value)
    assert "cmake" in message.lower()
    assert "CRSDKPY_HOST" in message
    assert "not distributed" in message


def test_find_host_executable_does_not_fall_back_on_explicit_path() -> None:
    assert find_host_executable("/nope/not/here") is None


# -- crash isolation --------------------------------------------------------
def test_python_survives_host_death() -> None:
    """A deliberate host exit, not an induced native fault."""
    backend = make_backend()
    backend.start()
    with pytest.raises(crsdkpy.BackendError) as excinfo:
        backend._provoke_host_exit()
    assert "unexpected" in str(excinfo.value) or "ended" in str(excinfo.value)
    assert backend.state == HostState.CRASHED
    # The interpreter is fine and the backend does not pretend to be healthy.
    assert 2 + 2 == 4
    with pytest.raises(crsdkpy.CrSDKPyError):
        backend.enumerate_cameras()


def test_session_is_not_reported_healthy_after_host_death() -> None:
    backend = make_backend()
    backend.start()
    session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
    assert backend.connection_state(session) is crsdkpy.ConnectionState.CONNECTED
    with pytest.raises(crsdkpy.BackendError):
        backend._provoke_host_exit()
    with pytest.raises(crsdkpy.CrSDKPyError):
        backend.connection_state(session)


# -- discovery and sessions -------------------------------------------------
def test_enumerate_over_ipc() -> None:
    backend = make_backend()
    backend.start()
    try:
        cameras = backend.enumerate_cameras()
        assert len(cameras) == 1
        assert cameras[0].model == "SIM-HostCamera"
        assert cameras[0].usb_pid == 0x0F52
        assert cameras[0].device_key == "FAKE-CAM:0000"
    finally:
        backend.shutdown()


def test_no_camera_is_not_an_error() -> None:
    backend = make_backend("no_camera")
    backend.start()
    try:
        assert backend.enumerate_cameras() == []
    finally:
        backend.shutdown()


def test_adapter_failure_keeps_its_specific_diagnosis() -> None:
    """The 0x8703 case must not degrade into a generic vendor error."""
    backend = make_backend("adapter_failure")
    backend.start()
    try:
        with pytest.raises(crsdkpy.CameraConnectionError) as excinfo:
            backend.enumerate_cameras()
        assert excinfo.value.backend_code == 0x8703
        assert "transport adapter" in str(excinfo.value)
    finally:
        backend.shutdown()


def test_session_open_close_and_idempotence() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        assert backend.connection_state(session) is crsdkpy.ConnectionState.CONNECTED
        backend.close_session(session)
        backend.close_session(session)
        assert backend.connection_state(session) is crsdkpy.ConnectionState.CLOSED
    finally:
        backend.shutdown()


def test_stale_session_fails_cleanly_and_does_not_alias() -> None:
    backend = make_backend()
    backend.start()
    try:
        first = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        stale_handle = backend._handles[first]
        backend.close_session(first)
        second = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        # A new session must never reuse the retired handle value.
        assert backend._handles[second] != stale_handle
        with pytest.raises(crsdkpy.SessionClosedError):
            backend.poll_events(first)
    finally:
        backend.shutdown()


def test_unknown_session_id_is_rejected() -> None:
    backend = make_backend()
    backend.start()
    try:
        with pytest.raises(crsdkpy.SessionClosedError):
            backend.poll_events("host-session-999")
        backend.close_session("host-session-999")  # idempotent
    finally:
        backend.shutdown()


# -- properties -------------------------------------------------------------
def test_property_list_over_ipc() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        properties = backend.list_properties(session)
        codes = {int(p.code) for p in properties}
        assert {0x0104, 0x0109, 0x0001, 0x0707, 0x7FFE} <= codes
        by_code = {int(p.code): p for p in properties}
        assert by_code[0x0104].value == 100
        assert by_code[0x0104].code.name == "IsoSensitivity"
        # An unnamed code survives the round trip untouched.
        assert by_code[0x7FFE].code.name is None
        assert by_code[0x7FFE].value == 42
    finally:
        backend.shutdown()


def test_property_read_over_ipc() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        prop = backend.get_property(session, crsdkpy.PropertyCode(0x0109))
        assert prop.value == 2
        assert prop.access is crsdkpy.PropertyAccess.READ_WRITE
    finally:
        backend.shutdown()


def test_unknown_property_code_reports_not_supported() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        with pytest.raises(crsdkpy.PropertyNotSupportedError):
            backend.get_property(session, crsdkpy.PropertyCode(0xABCD))
    finally:
        backend.shutdown()


# -- events -----------------------------------------------------------------
def test_events_traverse_ipc() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        events = backend.poll_events(session)
        assert events
        assert isinstance(events[0], crsdkpy.ConnectionEvent)
        # Drained once, gone.
        assert backend.poll_events(session) == []
    finally:
        backend.shutdown()


def test_request_ids_match_across_many_calls() -> None:
    """Sequential requests must never cross responses."""
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        for _ in range(25):
            state = backend.connection_state(session)
            assert state is crsdkpy.ConnectionState.CONNECTED
            # Not an exact count: the number of properties is a live figure
            # and asserting it turns a normal change into a failure.
            assert len(backend.list_properties(session)) > 0
    finally:
        backend.shutdown()


# -- capability refusals ----------------------------------------------------
def test_live_view_is_refused_where_the_camera_cannot_stream() -> None:
    """RemoteTransfer answers the info call and still cannot deliver."""
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session(
            "FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE_TRANSFER
        )
        info = backend.live_view_info(session)
        assert info.info_ok          # the query itself succeeded
        assert not info.usable       # but no frame can come out of it
        assert not backend.session_capabilities(session).live_view
    finally:
        backend.shutdown()


def test_live_view_is_available_in_remote_mode() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        info = backend.live_view_info(session)
        assert info.usable
        assert backend.session_capabilities(session).live_view
    finally:
        backend.shutdown()


def test_operations_on_an_unknown_session_are_refused() -> None:
    backend = make_backend()
    for call in (
        lambda b: b.get_live_view_frame("nope"),
        lambda b: b.pull_postview("nope"),
        lambda b: b.list_content("nope"),
    ):
        with pytest.raises(crsdkpy.SessionClosedError):
            call(backend)


def test_unknown_operation_is_refused_by_the_host() -> None:
    backend = make_backend()
    backend.start()
    try:
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            backend._call(4242, operation="bogus")
    finally:
        backend.shutdown()


# -- property writes --------------------------------------------------------
def test_property_write_round_trips_over_ipc() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        backend.set_property(session, crsdkpy.PropertyCode(0x0104), 125)
        assert backend.get_property(session, crsdkpy.PropertyCode(0x0104)).value == 125
        backend.set_property(session, crsdkpy.PropertyCode(0x0104), 100)
        assert backend.get_property(session, crsdkpy.PropertyCode(0x0104)).value == 100
    finally:
        backend.shutdown()


def test_property_write_emits_a_change_event() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        backend.poll_events(session)  # discard the connection burst
        backend.set_property(session, crsdkpy.PropertyCode(0x0104), 125)
        events = backend.poll_events(session)
        changed = [e for e in events if isinstance(e, crsdkpy.PropertyChangedEvent)]
        assert changed and 0x0104 in changed[0]
    finally:
        backend.shutdown()


def test_read_only_property_write_is_refused() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="not settable"):
            backend.set_property(session, crsdkpy.PropertyCode(0x0707), 1)
    finally:
        backend.shutdown()


def test_write_to_unknown_code_is_refused() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        with pytest.raises(crsdkpy.PropertyNotSupportedError):
            backend.set_property(session, crsdkpy.PropertyCode(0xABCD), 1)
    finally:
        backend.shutdown()


def test_large_property_value_survives_the_two_slot_split() -> None:
    """Values wider than 32 bits must not be truncated on the wire."""
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        wide = 0x1234_5678_9ABC
        backend.set_property(session, crsdkpy.PropertyCode(0x7FFE), wide)
        assert backend.get_property(session, crsdkpy.PropertyCode(0x7FFE)).value == wide
    finally:
        backend.shutdown()


def test_property_write_through_the_public_api() -> None:
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            session.properties.set(0x0104, 125)
            assert session.properties.get(0x0104).value == 125
            session.raw.set_property(0x0104, 100)
            assert session.properties.get(0x0104).value == 100


# -- commands ---------------------------------------------------------------
def test_release_sequence_produces_a_capture_event() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        backend.poll_events(session)  # discard the connection burst
        backend.send_command(
            session, crsdkpy.Command.RELEASE, crsdkpy.CommandParameter.DOWN
        )
        backend.send_command(
            session, crsdkpy.Command.RELEASE, crsdkpy.CommandParameter.UP
        )
        events = backend.poll_events(session)
        assert any(isinstance(e, crsdkpy.CaptureEvent) for e in events)
    finally:
        backend.shutdown()


def test_raw_numeric_command_passes_through() -> None:
    """A command CrSDKPy has never heard of must still be sendable."""
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        backend.send_command(session, 0xD2FF, crsdkpy.CommandParameter.DOWN)
        backend.send_command(session, 0xD2FF, crsdkpy.CommandParameter.UP)
    finally:
        backend.shutdown()


def test_bad_command_parameter_is_refused() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        with pytest.raises(crsdkpy.BackendError, match="up or down"):
            backend.send_command(session, crsdkpy.Command.RELEASE, 7)
    finally:
        backend.shutdown()


def test_capture_through_the_public_api_waits_for_the_event() -> None:
    """Command acceptance is not the success signal; the event is."""
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            capture = session.capture()
            assert capture.exposed
            assert capture.state is crsdkpy.CaptureState.EXPOSED


# -- gated autofocus --------------------------------------------------------
def test_half_press_round_trips() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        assert backend.get_half_press(session) is False
        backend.set_half_press(session, True)
        assert backend.get_half_press(session) is True
        backend.set_half_press(session, False)
        assert backend.get_half_press(session) is False
    finally:
        backend.shutdown()


def test_focus_state_decodes_the_property_channel() -> None:
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        assert backend.focus_state(session) is crsdkpy.FocusState.UNLOCKED
        backend.set_half_press(session, True)
        assert backend.focus_state(session) is crsdkpy.FocusState.FOCUSED_AF_S
    finally:
        backend.shutdown()


def test_af_status_channel_uses_its_own_encoding() -> None:
    """The two channels share states but not numbering."""
    backend = make_backend()
    backend.start()
    try:
        session = backend.open_session("FAKE-CAM:0000", crsdkpy.SessionMode.REMOTE)
        backend.poll_events(session)
        backend.set_half_press(session, True)
        focus = [
            e for e in backend.poll_events(session)
            if isinstance(e, crsdkpy.FocusEvent)
        ]
        assert focus
        warned = [e for e in focus if e.source == crsdkpy.FocusSource.STATUS_WARNING]
        assert warned
        # Raw value 2 on the warning channel, not the property channel's 0x0102.
        assert warned[0].raw_value == 2
        assert warned[0].state is crsdkpy.FocusState.FOCUSED_AF_S
    finally:
        backend.shutdown()


def test_gated_capture_succeeds_when_focus_confirms() -> None:
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            assert session.capabilities.autofocus_s1
            capture = session.autofocus_and_capture()
            assert capture.focus is not None and capture.focus.confirmed
            assert capture.exposed
            # A successful release clears the half-press stage by itself.
            assert session.raw.half_press is False


def test_gated_capture_refuses_when_focus_fails() -> None:
    """No exposure may be requested when autofocus does not confirm."""
    backend = make_backend("af_no_lock")
    with crsdkpy.SDK(backend=backend) as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            with pytest.raises(crsdkpy.AutofocusFailedError) as excinfo:
                session.autofocus_and_capture(focus_timeout_ms=800)
            assert excinfo.value.focus_state is crsdkpy.FocusState.NOT_FOCUSED_AF_S
            # The failed attempt must not leave the stage engaged.
            assert session.raw.half_press is False


def test_tracking_never_counts_as_focus() -> None:
    """AF-C passes through tracking; releasing on it would fire early."""
    backend = make_backend("af_tracking")
    with crsdkpy.SDK(backend=backend) as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            result = session.autofocus()
            assert result.confirmed
            assert result.state is crsdkpy.FocusState.FOCUSED_AF_C
            assert result.state is not crsdkpy.FocusState.TRACKING_AF_C
            session.raw.set_half_press(False)


# -- public API integration -------------------------------------------------
def test_public_api_drives_the_host_backend() -> None:
    """The host must satisfy the same contract as every other backend."""
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        assert sdk.backend_name == "host"
        cameras = sdk.discover()
        assert len(cameras) == 1
        camera = cameras[0]
        assert camera.model == "SIM-HostCamera"
        with camera.open("remote") as session:
            assert session.state is crsdkpy.ConnectionState.CONNECTED
            snapshot = session.properties.snapshot()
            assert len(snapshot) > 0
            assert snapshot.unknown_codes()
            assert session.raw.get_property(0x0104).value == 100
        assert session.closed
        # Camera identity outlives the session, as everywhere else.
        assert camera.device_key == "FAKE-CAM:0000"


def test_fake_host_process_actually_exits() -> None:
    """No orphaned helper processes after a normal shutdown."""
    backend = make_backend()
    backend.start()
    process = backend._process
    assert process is not None
    backend.shutdown()
    assert process.poll() is not None


def test_host_stderr_is_not_the_protocol_stream() -> None:
    """Sanity: the fake host writes frames only to stdout."""
    completed = subprocess.run(
        [sys.executable, FAKE_HOST, "garbage"],
        input=b"",
        capture_output=True,
        timeout=30,
    )
    assert completed.stdout.startswith(b"this is not a frame")
    assert completed.stderr == b""


def test_busy_is_not_reported_as_a_broken_connection() -> None:
    """A busy camera must stay distinguishable from a dead one.

    Hardware reaches this: the first content listing after opening a
    RemoteTransfer session can arrive while the camera is still building its
    index, and fails in about a millisecond with a vendor code the next call
    does not produce. Reporting that as a connection error invites a caller to
    tear down a session that is perfectly healthy, so the category has to
    survive the trip across the wire as its own thing.
    """
    response = _ipc.ResponseStruct()
    response.status = 0x8D05  # CrError_RemoteTransfer_GetContentsInfoListProcessing
    response.category = _ipc.CAT_BUSY
    response.message = b"the camera is still building its content index"

    error = HostBackend._to_exception(response, "list_content")

    assert isinstance(error, crsdkpy.CameraBusyError)
    assert not isinstance(error, crsdkpy.CameraConnectionError)
    # The vendor code is preserved, because deciding whether to retry is the
    # caller's business and it may want to know exactly what happened.
    assert error.backend_code == 0x8D05
    assert error.operation == "list_content"


def test_a_connection_callback_timeout_is_retried_once() -> None:
    """The one retry that hardware justifies, exercised without a camera.

    When a previous consumer went away without disconnecting, the camera still
    holds that transport session: the vendor accepts Connect and never delivers
    the connection callback. The failed attempt's own disconnect is what clears
    it, so the next attempt runs against a camera the first one just cleaned up
    and succeeds. Measured on hardware as 15.03 s then 0.59 s.
    """
    backend = make_backend("connect_timeout_once")
    try:
        backend.start()
        session = backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        assert session  # the retry got there
    finally:
        backend.shutdown()


def test_a_persistent_connect_failure_is_not_retried_forever() -> None:
    """One retry, not a loop. A camera that never connects has to say so."""
    backend = make_backend("connect_timeout_always")
    try:
        backend.start()
        with pytest.raises(crsdkpy.CameraConnectionError) as excinfo:
            backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        assert "connection callback" in str(excinfo.value)
    finally:
        backend.shutdown()


def test_the_save_directory_is_always_resolved_to_something() -> None:
    """A host-bound still needs a configured path, so there is no "unset".

    Without one the camera announces no postview at all and leaves the still
    destination unsettable for the rest of the session, so this returning
    nothing would be a silent loss of a whole feature.
    """
    from crsdkpy.backend.host import resolve_save_directory

    resolved = resolve_save_directory()
    assert os.path.isabs(resolved)
    assert os.path.isdir(resolved)


def test_an_explicit_save_directory_wins(tmp_path) -> None:
    from crsdkpy.backend.host import resolve_save_directory

    wanted = tmp_path / "somewhere-else"
    assert resolve_save_directory(str(wanted)) == str(wanted)
    assert wanted.is_dir()  # created rather than merely reported


def test_the_save_directory_is_not_the_vendor_runtime_directory() -> None:
    """The default must not be the directory the host runs in.

    That is the vendor runtime's own directory. The library does not own it and
    it need not be writable, so it is the wrong place to let a camera write.
    """
    from crsdkpy.backend.host import resolve_save_directory

    backend = make_backend()
    assert resolve_save_directory() != backend._adapter_dir


def _drain_transfer_events(backend, session, attempts: int = 6) -> list:
    seen = []
    for _ in range(attempts):
        seen.extend(
            e for e in backend.poll_events(session, timeout_ms=0)
            if isinstance(e, crsdkpy.TransferEvent)
        )
    return seen


def test_a_file_writing_transfer_reports_progress_then_success() -> None:
    """The result overload the vendor uses for file-writing transfers.

    Those requests report through a different callback overload than the one
    carrying bytes. With only the byte-carrying overload implemented, a transfer
    wrote its file correctly and reported nothing at all, so a caller had no way
    to know it had finished except by watching the filesystem.
    """
    backend = make_backend("transfer_file")
    try:
        backend.start()
        session = backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        events = _drain_transfer_events(backend, session)

        outcomes = [e.outcome for e in events]
        assert crsdkpy.TransferOutcome.IN_PROGRESS in outcomes
        assert crsdkpy.TransferOutcome.OK in outcomes
        # Progress must not read as an ending, or a caller stops waiting early.
        progress = next(
            e for e in events
            if e.outcome is crsdkpy.TransferOutcome.IN_PROGRESS
        )
        assert not progress.outcome.finished
        assert progress.percent == 40
        assert not bool(progress)

        done = next(
            e for e in events if e.outcome is crsdkpy.TransferOutcome.OK
        )
        assert done.outcome.finished
        assert bool(done)
        assert done.percent == 100
        assert done.has_path
        # The vendor code survives even though the outcome was recognised.
        assert done.notify_code == 0x20100

        # The path is collected separately and consumed once.
        assert backend.take_transfer_path(session) == "C:/saved/DSC09999.ARW"
        assert backend.take_transfer_path(session) is None
    finally:
        backend.shutdown()


def test_a_failed_transfer_is_finished_but_not_successful() -> None:
    """A caller waiting for the end must not wait forever on a failure."""
    backend = make_backend("transfer_file_ng")
    try:
        backend.start()
        session = backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        events = _drain_transfer_events(backend, session)

        failure = next(
            e for e in events
            if e.outcome is crsdkpy.TransferOutcome.FAILED
        )
        assert failure.outcome.finished  # the wait ends
        assert not bool(failure)         # but nothing was produced
        assert not failure.has_path
        assert backend.take_transfer_path(session) is None
    finally:
        backend.shutdown()


def test_an_unrecognised_transfer_code_keeps_the_vendor_value() -> None:
    """A newer camera reporting something new is not an error."""
    from crsdkpy.backend import _cabi
    from crsdkpy.backend.native import decode_event

    raw = _cabi.EventStruct()
    raw.kind = _cabi.EVENT_TRANSFER
    raw.code = 0x2FFFF          # not in this version's mapping
    raw.i0 = 77
    raw.i1 = _cabi.TRANSFER_UNKNOWN
    event = decode_event(raw, timestamp_ms=5)

    assert event.outcome is crsdkpy.TransferOutcome.UNKNOWN
    assert event.notify_code == 0x2FFFF
    assert event.percent == 77


def test_string_properties_report_their_string_not_zero() -> None:
    """String-valued properties answer through a different vendor accessor.

    Reading only the numeric accessor returns zero for every one of them, which
    is how model name, body serial, firmware version and lens identity all came
    back as 0 while the camera was reporting them perfectly well.
    """
    backend = make_backend()
    try:
        backend.start()
        session = backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)

        model = backend.get_property(session, crsdkpy.PropertyCode(0x07B2))
        assert model.value_type is crsdkpy.PropertyValueType.STRING
        assert model.value == "SIM-CAM-1"

        lens = backend.get_property(session, crsdkpy.PropertyCode(0x0765))
        assert lens.value == "SIM 100mm F2.0"
        firmware = backend.get_property(session, crsdkpy.PropertyCode(0x0751))
        assert firmware.value == "9.99"
    finally:
        backend.shutdown()


def test_numeric_properties_are_untouched_by_the_string_path() -> None:
    """The numeric path must not change, and must not pay for the string one."""
    backend = make_backend()
    try:
        backend.start()
        session = backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        iso = backend.get_property(session, crsdkpy.PropertyCode(0x0104))
        assert iso.value_type is crsdkpy.PropertyValueType.INT
        assert iso.value == 100
        assert isinstance(iso.value, int)
    finally:
        backend.shutdown()


def test_a_snapshot_carries_strings_and_numbers_together() -> None:
    backend = make_backend()
    try:
        backend.start()
        session = backend.open_session("cam-0", crsdkpy.SessionMode.REMOTE)
        by_code = {int(p.code): p for p in backend.list_properties(session)}
        assert by_code[0x07B2].value == "SIM-CAM-1"
        assert by_code[0x0104].value == 100
    finally:
        backend.shutdown()
