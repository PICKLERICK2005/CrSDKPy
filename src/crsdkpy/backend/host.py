"""Out-of-process host backend.

Runs the vendor SDK inside a first-party ``crsdkpy_host`` executable and talks
to it over the child's stdin/stdout pipes.

Two reasons, one forced and one earned:

* The vendor SDK resolves its transport-adapter directory against the host
  executable's directory. Measured across six configurations: only placing an
  executable beside the adapters works. A library cannot move the interpreter,
  so it supplies its own executable instead.
* Vendor code then runs in a replaceable process. A native fault reports as a
  backend error rather than taking the interpreter with it.

This implements the same backend contract as the simulator, so the public API
is unchanged and the existing test suite applies unmodified.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Optional

from ..capabilities import SessionCapabilities
from ..clock import Clock, RealClock
from ..enums import (
    ConnectionState,
    PreviewKind,
    PropertyValueType,
    SessionMode,
    StillDestination,
)
from ..errors import (
    BackendError,
    CameraBusyError,
    CameraConnectionError,
    CrSDKPyError,
    NativeBackendError,
    OperationTimeoutError,
    PropertyNotSupportedError,
    SDKNotFoundError,
    SessionClosedError,
    UnsupportedOperationError,
)
from ..events import Event
from ..previews import LiveViewFrame, Preview
from ..properties import Property, PropertyCode
from . import _cabi, _ipc
from .contract import Backend, BackendCameraInfo, ContentRef, LiveViewInfo
from .native import (
    _ABI_TO_STATE,
    _PREVIEW_TIMEOUT_MS,
    CONTENT_MODES,
    PREVIEW_CAPABILITY,
    DeviceStatusMixin,
    FrameSequencer,
    MeasuredCapabilities,
    NativeCapabilityMixin,
    ShutterStageMixin,
    VideoStageMixin,
    build_content_preview,
    build_live_view_frame,
    build_postview,
    command_to_abi,
    decode_camera_info,
    decode_content,
    decode_event,
    decode_live_view_info,
    decode_property,
    parameter_to_abi,
    preview_kind_to_abi,
    unsupported_content_mode,
)

__all__ = ["HostBackend", "HostState", "find_host_executable"]


def _as_int32(value: int) -> int:
    """Reinterpret an unsigned 32-bit value as signed for the wire."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


class HostState:
    """Lifecycle of the helper process."""

    NOT_STARTED = "not_started"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"


#: Environment variable naming an explicit host executable.
HOST_ENV_VAR = "CRSDKPY_HOST"

#: Environment variable naming the directory for host-bound stills.
SAVE_DIR_ENV_VAR = "CRSDKPY_SAVE_DIR"

#: Retries of a connection-callback timeout. Exactly one, because the value of
#: a second attempt comes from the first one's disconnect having cleared the
#: camera's stale session, and a third has nothing new to clear.
_CONNECT_RETRIES = 1


def resolve_save_directory(explicit: Optional[str] = None) -> str:
    """Choose the directory the camera may write a host-bound still into.

    The vendor requires one to be configured before a capture whose destination
    includes the host: without it the camera announces no postview and leaves
    the destination property unsettable for the rest of the session. So this
    always returns a path rather than sometimes returning nothing.

    The default is deliberately **not** the directory the host runs in. That is
    the vendor runtime's own directory, which the library does not own and which
    need not be writable. A temporary directory is somewhere the library may
    always create, and it is honest about the current state of affairs: with the
    destinations CrSDKPy supports today the camera writes nothing there, and the
    path exists to satisfy the precondition. When original-file transfer lands
    and files really do arrive, the destination for them becomes a caller's
    decision and belongs in the public API, not in this default.
    """
    chosen = explicit or os.environ.get(SAVE_DIR_ENV_VAR) or os.path.join(
        tempfile.gettempdir(), "crsdkpy-host"
    )
    chosen = os.path.abspath(chosen)
    try:
        os.makedirs(chosen, exist_ok=True)
    except OSError:
        # Not fatal: a card-only session never needs it, and the host reports a
        # warning event if the camera refuses the path.
        pass
    return chosen


def _host_filename() -> str:
    return "crsdkpy_host.exe" if sys.platform == "win32" else "crsdkpy_host"


def find_host_executable(explicit: Optional[str] = None) -> Optional[str]:
    """Locate the built host, or return ``None``.

    An explicit path is taken literally: a wrong one is an error rather than a
    silent fallback to some other binary.
    """
    if explicit:
        return os.path.abspath(explicit) if os.path.isfile(explicit) else None
    from_env = os.environ.get(HOST_ENV_VAR)
    if from_env:
        return os.path.abspath(from_env) if os.path.isfile(from_env) else None

    name = _host_filename()
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    for relative in (
        os.path.join("native", "build", "Release", name),
        os.path.join("native", "build", "Debug", name),
        os.path.join("native", "build", name),
    ):
        candidate = os.path.join(repo_root, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


class HostBackend(
    ShutterStageMixin,
    DeviceStatusMixin,
    VideoStageMixin,
    NativeCapabilityMixin,
    Backend,
):
    """Backend that drives the vendor SDK through a helper process."""

    name = "host"

    def __init__(
        self,
        host_path: Optional[str] = None,
        *,
        clock: Optional[Clock] = None,
        enumerate_timeout_sec: int = 3,
        adapter_dir: Optional[str] = None,
        start_timeout_sec: float = 20.0,
        command: Optional[Sequence[str]] = None,
        save_directory: Optional[str] = None,
    ) -> None:
        # `command` launches an arbitrary argv instead of a located binary. It
        # exists so the pure-Python fake host can be driven through exactly the
        # same code path in tests.
        self._command = list(command) if command else None
        resolved = None if self._command else find_host_executable(host_path)
        if resolved is None and self._command is None:
            raise SDKNotFoundError(
                "The CrSDKPy host executable was not found. Build it with:\n"
                "  cmake -S native -B native/build "
                "-DCRSDK_ROOT=/path/to/CrSDK/RemoteCli\n"
                "  cmake --build native/build --config Release\n"
                f"or set {HOST_ENV_VAR} to the built {_host_filename()}. The Sony "
                "Camera Remote SDK is not distributed with CrSDKPy and must be "
                "obtained from Sony.",
                operation="host.locate",
            )
        self._host_path = resolved
        # The vendor runtime lives beside the host executable; that placement
        # is what satisfies the adapter-directory constraint.
        if adapter_dir:
            self._adapter_dir = adapter_dir
        elif resolved:
            self._adapter_dir = os.path.dirname(resolved)
        else:
            self._adapter_dir = os.getcwd()
        self._clock = clock or RealClock()
        self._enumerate_timeout = enumerate_timeout_sec
        self._start_timeout = start_timeout_sec
        self._save_directory = resolve_save_directory(save_directory)

        self._process: Optional[subprocess.Popen] = None
        self._state = HostState.NOT_STARTED
        self._lock = threading.RLock()
        self._request_id = 0
        self._handshake: Optional[_ipc.HelloAckStruct] = None

        self._handles: dict[str, int] = {}
        self._modes: dict[str, SessionMode] = {}
        self._session_counter = 0
        self._measurements = MeasuredCapabilities()
        self._frames = FrameSequencer()

    # -- lifecycle ---------------------------------------------------------
    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def state(self) -> str:
        self._refresh_state()
        return self._state

    @property
    def handshake(self) -> Optional[_ipc.HelloAckStruct]:
        return self._handshake

    def _refresh_state(self) -> None:
        if self._state in (HostState.READY, HostState.STARTING) and self._process:
            if self._process.poll() is not None:
                self._state = HostState.CRASHED

    def start(self) -> None:
        with self._lock:
            if self._state == HostState.READY:
                return
            self._state = HostState.STARTING
            try:
                # The save directory is passed explicitly rather than left to
                # inheritance, so the resolved policy is what the host sees
                # even when the parent's environment says something else.
                child_env = dict(os.environ)
                child_env[SAVE_DIR_ENV_VAR] = self._save_directory
                self._process = subprocess.Popen(
                    self._command or [self._host_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=self._adapter_dir,
                    close_fds=True,
                    env=child_env,
                )
            except OSError as exc:
                self._state = HostState.CRASHED
                raise NativeBackendError(
                    "could not start the host process at "
                    f"{self._command or self._host_path}: {exc}",
                    operation="host.start",
                ) from exc

            try:
                self._do_handshake()
                self._call(
                    _ipc.OP_INIT,
                    text=self._adapter_dir,
                    operation="init",
                )
            except Exception:
                self._terminate()
                raise
            self._state = HostState.READY

    def _do_handshake(self) -> None:
        hello = _ipc.HelloStruct(
            version_major=_ipc.VERSION_MAJOR, version_minor=_ipc.VERSION_MINOR
        )
        request_id = self._next_request_id()
        self._write(_ipc.encode(_ipc.MSG_HELLO, request_id, _ipc.as_bytes(hello)))
        message_type, _, meta, _ = self._read_frame_or_die("handshake")
        if message_type != _ipc.MSG_HELLO_ACK:
            raise NativeBackendError(
                f"host replied with message type {message_type} during the "
                "handshake instead of an acknowledgement",
                operation="host.handshake",
            )
        ack = _ipc.from_bytes(_ipc.HelloAckStruct, meta)
        if ack.protocol_major != _ipc.VERSION_MAJOR:
            raise NativeBackendError(
                f"host speaks IPC protocol {ack.protocol_major}.{ack.protocol_minor}, "
                f"this build expects major {_ipc.VERSION_MAJOR}. Rebuild the host "
                "from this checkout.",
                operation="host.handshake",
            )
        if ack.abi_major != _cabi.ABI_VERSION_MAJOR:
            raise NativeBackendError(
                f"host was built against C ABI major {ack.abi_major}, this build "
                f"expects {_cabi.ABI_VERSION_MAJOR}. Rebuild the host.",
                operation="host.handshake",
            )
        self._handshake = ack

    def shutdown(self) -> None:
        with self._lock:
            if self._process is None:
                self._state = HostState.STOPPED
                return
            self._state = HostState.STOPPING
            if self._process.poll() is None:
                try:
                    self._write(_ipc.encode(_ipc.MSG_BYE, self._next_request_id()))
                except Exception:
                    pass
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._terminate()
            self._cleanup_process()
            self._state = HostState.STOPPED

    def _terminate(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.kill()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill is final
                pass
        self._cleanup_process()

    def _cleanup_process(self) -> None:
        if self._process is None:
            return
        for stream in (self._process.stdin, self._process.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._process = None
        self._handles.clear()
        self._modes.clear()

    # -- transport ---------------------------------------------------------
    def _next_request_id(self) -> int:
        self._request_id = (self._request_id + 1) & 0xFFFFFFFF
        return self._request_id or 1

    def _require_process(self, operation: str) -> subprocess.Popen:
        process = self._process
        if process is None:
            raise NativeBackendError(
                "the host process is not running", operation=operation
            )
        code = process.poll()
        if code is not None:
            self._state = HostState.CRASHED
            self._cleanup_process()
            raise BackendError(
                f"the host process exited unexpectedly with code {code}; the "
                "vendor SDK ran in that process, so the interpreter is "
                "unaffected. Start a new SDK to recover.",
                operation=operation,
                backend_code=code,
            )
        return process

    def _write(self, payload: bytes) -> None:
        process = self._require_process("host.write")
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._state = HostState.CRASHED
            raise BackendError(
                "the host process closed its input pipe; it most likely died",
                operation="host.write",
            ) from exc

    def _read_frame_or_die(self, operation: str):
        process = self._require_process(operation)
        try:
            frame = _ipc.read_frame(process.stdout)
        except _ipc.ProtocolError as exc:
            self._state = HostState.CRASHED
            self._terminate()
            raise BackendError(
                f"the host sent a malformed frame during {operation}: {exc}",
                operation=operation,
            ) from exc
        if frame is None:
            code = process.poll()
            self._state = HostState.CRASHED
            self._cleanup_process()
            raise BackendError(
                f"the host process ended during {operation} (exit code {code}); "
                "the interpreter is unaffected",
                operation=operation,
                backend_code=code if code is not None else None,
            )
        return frame

    def _call(
        self,
        op: int,
        *,
        handle: int = 0,
        u32: int = 0,
        i32: int = 0,
        i32_2: int = 0,
        text: str = "",
        operation: str = "",
    ):
        """Send one request and wait for its matching response."""
        response, blob, _ = self._call_ex(
            op,
            handle=handle,
            u32=u32,
            i32=i32,
            i32_2=i32_2,
            text=text,
            operation=operation,
        )
        return response, blob

    def _call_ex(
        self,
        op: int,
        *,
        handle: int = 0,
        u32: int = 0,
        i32: int = 0,
        i32_2: int = 0,
        text: str = "",
        operation: str = "",
        extra: bytes = b"",
    ):
        """Send one request and wait for its matching response.

        Returns ``(response, blob, meta_tail)``. *extra* and the returned tail
        are the additional POD some operations append after the fixed struct;
        see ``ipc_protocol.h``.

        Unsolicited event frames arriving while a request is outstanding are
        buffered rather than mistaken for the response.
        """
        with self._lock:
            request = _ipc.RequestStruct(
                op=op,
                u32_arg=u32,
                i32_arg=_as_int32(i32),
                i32_arg2=_as_int32(i32_2),
                handle=handle,
                text=text.encode("utf-8")[:207],
            )
            request_id = self._next_request_id()
            self._write(
                _ipc.encode(
                    _ipc.MSG_REQUEST, request_id, _ipc.as_bytes(request) + extra
                )
            )

            while True:
                message_type, got_id, meta, blob = self._read_frame_or_die(
                    operation or f"op{op}"
                )
                if message_type == _ipc.MSG_EVENT:
                    self._buffer_event(meta, blob)
                    continue
                if message_type != _ipc.MSG_RESPONSE:
                    raise BackendError(
                        f"host sent message type {message_type} where a response "
                        "was expected",
                        operation=operation,
                    )
                if got_id != request_id:
                    raise BackendError(
                        f"response id {got_id} does not match request id "
                        f"{request_id}; the stream is out of step",
                        operation=operation,
                    )
                response = _ipc.from_bytes(_ipc.ResponseStruct, meta)
                if response.status != 0:
                    raise self._to_exception(response, operation)
                return response, blob, meta[ctypes.sizeof(_ipc.ResponseStruct) :]

    def _buffer_event(self, meta: bytes, blob: bytes) -> None:  # pragma: no cover
        # Reserved for a push-event channel. Events are polled today, so this
        # exists to keep an unsolicited frame from corrupting a response.
        pass

    @staticmethod
    def _to_exception(response, operation: str) -> CrSDKPyError:
        message = response.message.decode("utf-8", "replace") or (
            f"{operation} failed with status {response.status}"
        )
        code = response.status if response.status > 0 else None
        category = response.category
        if category == _ipc.CAT_STALE_HANDLE:
            return SessionClosedError(
                "the session handle is closed or stale", operation=operation
            )
        if category == _ipc.CAT_NOT_STARTED:
            return NativeBackendError(
                "the host has not initialised the vendor SDK", operation=operation
            )
        if category == _ipc.CAT_UNSUPPORTED:
            return UnsupportedOperationError(message, operation=operation)
        if category == _ipc.CAT_SDK_MISSING:
            return SDKNotFoundError(message, operation=operation)
        if category == _ipc.CAT_ADAPTER_PATH:
            # Keep the specific, already-understood diagnosis.
            return CameraConnectionError(
                message
                or "the vendor SDK could not create a transport adapter; its "
                "adapter directory is resolved against the host executable's "
                "directory",
                operation=operation,
                backend_code=code,
            )
        if category == _ipc.CAT_NOT_CONNECTED:
            return CameraConnectionError(
                "the session is not connected", operation=operation
            )
        if category == _ipc.CAT_CONNECT_TIMEOUT:
            # Still a connection error to the caller. The marker exists so the
            # one place that retries can recognise it without matching on the
            # message text.
            error = CameraConnectionError(
                message, operation=operation, backend_code=code
            )
            error._connect_callback_timeout = True
            return error
        if category == _ipc.CAT_TIMEOUT:
            return OperationTimeoutError(message, operation=operation)
        if category == _ipc.CAT_NOT_FOUND:
            return PropertyNotSupportedError(message)
        if category == _ipc.CAT_BUSY:
            # Transient. Reporting a connection error here would invite a
            # caller to tear down a session that is perfectly healthy.
            return CameraBusyError(
                message, operation=operation, backend_code=code
            )
        if category == _ipc.CAT_INVALID_ARG:
            return BackendError(message, operation=operation, backend_code=code)
        return CameraConnectionError(
            message, operation=operation, backend_code=code
        )

    # -- discovery ---------------------------------------------------------
    def enumerate_cameras(self) -> Sequence[BackendCameraInfo]:
        if self._state != HostState.READY:
            raise NativeBackendError(
                "the host backend has not been started", operation="enumerate_cameras"
            )
        response, blob = self._call(
            _ipc.OP_ENUMERATE,
            i32=self._enumerate_timeout,
            operation="enumerate_cameras",
        )
        infos = _ipc.unpack_array(_cabi.CameraInfoStruct, blob, response.count)
        return [decode_camera_info(info) for info in infos]

    # -- sessions ----------------------------------------------------------
    def _open_with_retry(self, abi_mode: int, device_key: str):
        """Open a session, retrying only a connection-callback timeout.

        Hardware behaviour this exists for: when a previous consumer went away
        without disconnecting, the camera still holds that transport session.
        The vendor accepts Connect and never delivers the connection callback,
        so the attempt spends its whole deadline waiting. What clears the stale
        session is that failed attempt's own disconnect, which makes a second
        attempt materially different from the first rather than a hopeful
        repeat: it runs against a camera the first attempt just cleaned up.
        Measured on an ILME-FX3A, the first attempt failed at 15.03 s and the
        next succeeded in 0.59 s.

        Deliberately narrow. Only this one category retries, and only once. A
        vendor rejection of Connect is reported as-is, because nothing was
        cleaned up and repeating it would just spend the deadline twice.
        """
        attempts = _CONNECT_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                response, _ = self._call(
                    _ipc.OP_OPEN_SESSION,
                    i32=abi_mode,
                    text=device_key,
                    operation="open_session",
                )
                return response
            except CameraConnectionError as exc:
                if attempt == attempts or not getattr(
                    exc, "_connect_callback_timeout", False
                ):
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def open_session(
        self,
        device_key: str,
        mode: SessionMode,
        destination: Optional[StillDestination] = None,
    ) -> str:
        abi_mode = {
            SessionMode.REMOTE: 0,
            SessionMode.CONTENTS_TRANSFER: 1,
            SessionMode.REMOTE_TRANSFER: 2,
        }[mode]
        response = self._open_with_retry(abi_mode, device_key)
        self._session_counter += 1
        session_id = f"host-session-{self._session_counter}"
        self._handles[session_id] = response.handle
        self._modes[session_id] = mode
        # Destination is a property, so it is applied after connecting rather
        # than being part of the connection itself.
        if destination is not None:
            self._apply_destination(session_id, destination)
        return session_id

    def _handle(self, session_id: str, operation: str) -> int:
        handle = self._handles.get(session_id)
        if handle is None:
            raise SessionClosedError(
                f"unknown session {session_id!r}", operation=operation
            )
        return handle

    def close_session(self, session_id: str) -> None:
        handle = self._handles.pop(session_id, None)
        self._modes.pop(session_id, None)
        self._measurements.forget(session_id)
        self._frames.forget(session_id)
        if handle is None:
            return  # idempotent
        self._call(_ipc.OP_CLOSE_SESSION, handle=handle, operation="close_session")

    def connection_state(self, session_id: str) -> ConnectionState:
        # A dead host is an error, not an orderly closed session. Reporting
        # CLOSED here would let a caller believe the session ended cleanly.
        if self.state == HostState.CRASHED:
            raise BackendError(
                "the host process is gone, so the state of this session is "
                "unknown; start a new SDK to recover",
                operation="connection_state",
            )
        handle = self._handles.get(session_id)
        if handle is None:
            return ConnectionState.CLOSED
        response, _ = self._call(
            _ipc.OP_CONNECTION_STATE, handle=handle, operation="connection_state"
        )
        return _ABI_TO_STATE.get(response.i32_result, ConnectionState.CLOSED)

    def _measured(self) -> MeasuredCapabilities:
        return self._measurements

    def session_capabilities(self, session_id: str) -> SessionCapabilities:
        mode = self._modes.get(session_id, SessionMode.REMOTE)
        return self._build_capabilities(
            session_id, mode, {"backend": True, "hosted": True}
        )

    # -- events ------------------------------------------------------------
    def poll_events(self, session_id: str, timeout_ms: int = 0) -> Sequence[Event]:
        handle = self._handle(session_id, "poll_events")
        response, blob = self._call(
            _ipc.OP_POLL_EVENTS,
            handle=handle,
            i32=int(timeout_ms),
            operation="poll_events",
        )
        raws = _ipc.unpack_array(_cabi.EventStruct, blob, response.count)
        observed = self._clock.now_ms()
        return [decode_event(raw, timestamp_ms=observed) for raw in raws]

    # -- properties --------------------------------------------------------
    def list_properties(self, session_id: str) -> Sequence[Property]:
        handle = self._handle(session_id, "list_properties")
        response, blob = self._call(
            _ipc.OP_LIST_PROPERTIES, handle=handle, operation="list_properties"
        )
        raws = _ipc.unpack_array(_cabi.PropertyStruct, blob, response.count)
        return [self._with_string(session_id, decode_property(raw)) for raw in raws]

    def _with_string(self, session_id: str, prop: Property) -> Property:
        """Fill in the value of a string-valued property.

        The vendor answers a property through one of two accessors and only one
        is meaningful for a given type: a string property reports zero through
        the numeric accessor, which is what the property array carries. So a
        string is fetched for exactly those properties and for nothing else --
        the numeric path is untouched, and on the reference body this is seven
        properties out of several hundred.
        """
        if prop.value_type is not PropertyValueType.STRING:
            return prop
        try:
            response, _ = self._call(
                _ipc.OP_PROPERTY_STRING,
                handle=self._handle(session_id, "property_string"),
                u32=int(prop.code),
                operation="property_string",
            )
        except CrSDKPyError:
            # A property that will not answer as a string is reported as it
            # arrived rather than failing the whole snapshot.
            return prop
        if response.count == 0:
            return prop
        text = response.message.decode("utf-8", "replace")
        return replace(prop, value=text)

    def get_property(self, session_id: str, code: PropertyCode) -> Property:
        handle = self._handle(session_id, "get_property")
        response, blob = self._call(
            _ipc.OP_GET_PROPERTY,
            handle=handle,
            u32=int(code),
            operation="get_property",
        )
        raws = _ipc.unpack_array(_cabi.PropertyStruct, blob, response.count)
        if not raws:
            raise PropertyNotSupportedError(
                f"camera does not expose property {code}", code=int(code)
            )
        return self._with_string(session_id, decode_property(raws[0]))

    # -- test hooks --------------------------------------------------------
    def _provoke_host_exit(self) -> None:
        """Make the host terminate abruptly, for isolation tests only.

        This is a clean deliberate exit inside the helper, not an induced
        native fault, so it exercises process-death handling without risking
        the camera or the vendor SDK.

        Raises :class:`~crsdkpy.errors.BackendError`: the host never answers,
        so the caller observes the death exactly as it would a real crash.
        """
        self._call(_ipc.OP_TEST_CRASH, operation="test_crash")
        raise AssertionError("the host was expected to terminate")

    def set_property(self, session_id: str, code: PropertyCode, value: Any) -> None:
        handle = self._handle(session_id, "set_property")
        raw = int(value)
        # Split across the two 32-bit slots so a full vendor value fits.
        self._call(
            _ipc.OP_SET_PROPERTY,
            handle=handle,
            u32=int(code),
            i32=raw & 0xFFFFFFFF,
            i32_2=(raw >> 32) & 0xFFFFFFFF,
            operation="set_property",
        )

    def send_command(self, session_id: str, command: Any, parameter: Any) -> None:
        handle = self._handle(session_id, "send_command")
        self._call(
            _ipc.OP_SEND_COMMAND,
            handle=handle,
            u32=command_to_abi(command),
            i32=parameter_to_abi(parameter),
            operation="send_command",
        )

    # -- live view ---------------------------------------------------------
    def live_view_info(self, session_id: str) -> LiveViewInfo:
        handle = self._handle(session_id, "live_view_info")
        _, _, tail = self._call_ex(
            _ipc.OP_LIVE_VIEW_INFO, handle=handle, operation="live_view_info"
        )
        return decode_live_view_info(
            _ipc.from_bytes(_cabi.LiveViewInfoStruct, tail)
        )

    def get_live_view_frame(self, session_id: str) -> Optional[LiveViewFrame]:
        handle = self._handle(session_id, "get_live_view_frame")
        response, blob, tail = self._call_ex(
            _ipc.OP_LIVE_VIEW_FRAME, handle=handle, operation="get_live_view_frame"
        )
        if response.count == 0 or not tail:
            return None  # nothing new; ordinary around an exposure
        info = _ipc.from_bytes(_cabi.FrameInfoStruct, tail)
        if not self._frames.accept(session_id, int(info.frame_number)):
            return None  # the caller already has this one
        return build_live_view_frame(
            blob, info, timestamp_ms=self._clock.now_ms()
        )

    # -- postview ----------------------------------------------------------
    def configure_postview(
        self, session_id: str, *, enabled: bool, transfer_to_ram: bool = True
    ) -> None:
        handle = self._handle(session_id, "configure_postview")
        try:
            self._call(
                _ipc.OP_CONFIGURE_POSTVIEW,
                handle=handle,
                i32=1 if enabled else 0,
                i32_2=1 if transfer_to_ram else 0,
                operation="configure_postview",
            )
        except UnsupportedOperationError:
            self._measurements.postview_configuration_refused.add(session_id)
            raise

    def pull_postview(self, session_id: str) -> Optional[Preview]:
        handle = self._handle(session_id, "pull_postview")
        response, blob, tail = self._call_ex(
            _ipc.OP_PULL_POSTVIEW, handle=handle, operation="pull_postview"
        )
        if response.count == 0 or not tail:
            return None  # the camera has not announced one yet
        info = _ipc.from_bytes(_cabi.PostviewInfoStruct, tail)
        self._measurements.postview_delivered.add(session_id)
        return build_postview(blob, info, timestamp_ms=self._clock.now_ms())

    def take_transfer_path(self, session_id: str) -> Optional[str]:
        handle = self._handle(session_id, "take_transfer_path")
        response, _ = self._call(
            _ipc.OP_TAKE_TRANSFER_PATH,
            handle=handle,
            operation="take_transfer_path",
        )
        if response.count == 0:
            return None  # nothing written yet
        return response.message.decode("utf-8", "replace") or None

    # -- content index -----------------------------------------------------
    def _require_content_mode(
        self, session_id: str, operation: str, capability: str
    ) -> None:
        mode = self._modes.get(session_id, SessionMode.REMOTE)
        if mode not in CONTENT_MODES:
            raise unsupported_content_mode(mode, operation, capability)

    def latest_content(self, session_id: str) -> Optional[ContentRef]:
        items = self.list_content(session_id)
        return items[-1] if items else None

    def list_content(
        self, session_id: str, *, newer_than: Optional[int] = None
    ) -> Sequence[ContentRef]:
        handle = self._handle(session_id, "list_content")
        self._require_content_mode(session_id, "list_content", "content_index")
        args = _ipc.ContentArgsStruct(
            slot=_cabi.SLOT_1,
            # Clamped: the wire field is unsigned, so a negative bound would
            # wrap to the largest identifier and quietly match nothing.
            after_content_id=max(0, int(newer_than)) if newer_than is not None else 0,
        )
        response, blob, _ = self._call_ex(
            _ipc.OP_LIST_CONTENT,
            handle=handle,
            operation="list_content",
            extra=_ipc.as_bytes(args),
        )
        raws = _ipc.unpack_array(_cabi.ContentStruct, blob, response.count)
        observed = self._clock.now_ms()
        return tuple(decode_content(raw, observed_ms=observed) for raw in raws)

    def get_preview(
        self, session_id: str, content_id: int, kind: PreviewKind
    ) -> Preview:
        handle = self._handle(session_id, "get_preview")
        capability = PREVIEW_CAPABILITY.get(kind, "content_index")
        self._require_content_mode(session_id, "get_preview", capability)
        abi_kind = preview_kind_to_abi(kind)

        # The file id is not derivable from the content id, so the index is
        # consulted rather than guessed.
        content = self._content_by_id(session_id, int(content_id))
        args = _ipc.ContentArgsStruct(
            slot=_cabi.SLOT_1,
            content_id=int(content_id),
            file_id=content.file_id if content else 0,
            kind=abi_kind,
            timeout_ms=_PREVIEW_TIMEOUT_MS,
        )
        _, blob, tail = self._call_ex(
            _ipc.OP_CONTENT_PREVIEW,
            handle=handle,
            operation="get_preview",
            extra=_ipc.as_bytes(args),
        )
        info = _ipc.from_bytes(_cabi.PreviewInfoStruct, tail)
        return build_content_preview(
            kind,
            blob,
            info,
            timestamp_ms=self._clock.now_ms(),
            content=content,
        )

    def _content_by_id(self, session_id: str, content_id: int) -> Optional[ContentRef]:
        for item in self.list_content(session_id, newer_than=content_id - 1):
            if item.content_id == content_id:
                return item
        return None
