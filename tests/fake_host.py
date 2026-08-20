"""A pure-Python stand-in for ``crsdkpy_host``.

Speaks the same framed protocol over stdin/stdout, which is precisely why that
transport was chosen: every protocol, lifecycle and error test runs in CI with
no native build, no vendor SDK and no camera.

Run as ``python fake_host.py [behaviour]``. Behaviours:

    normal            one synthetic camera, sessions, properties, events
    af_no_lock        autofocus never reaches a focused state
    af_tracking       AF-C reports tracking before focusing
    no_camera         enumeration succeeds with zero cameras
    sdk_missing       init fails as if the vendor runtime were absent
    adapter_failure   init succeeds, enumeration fails with the adaptor code
    bad_protocol      acknowledges with an incompatible protocol major
    bad_abi           acknowledges with an incompatible C ABI major
    die_on_hello      exits before completing the handshake
    garbage           emits bytes that are not a valid frame
    stale_preview     answers a preview request for the previous still
    torn_preview      returns preview bytes that are not a whole JPEG
    no_postview       never announces a postview, whatever the destination
    empty_postview    announces a postview and then has nothing to deliver
    torn_postview     delivers postview bytes that are not a whole JPEG
    postview_config_refused
                      refuses postview configuration but still delivers
    stuck_live_view   keeps returning the same frame number
    connect_timeout_once
                      first session open reports the connection-callback
                      timeout, later ones succeed
    connect_timeout_always
                      every session open reports the connection-callback
                      timeout
"""

from __future__ import annotations

import os
import struct
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from crsdkpy.backend import _cabi, _ipc  # noqa: E402

BEHAVIOUR = sys.argv[1] if len(sys.argv) > 1 else "normal"

STATE = {
    "initialised": False,
    "cameras": [],
    "sessions": {},        # handle -> {"generation": int, "open": bool}
    "open_attempts": 0,
    "next_slot": 1,
    "generation": 0,
    "events": [],
    # IsoSensitivity, FocusMode, and a deliberately unnamed code.
    # IsoSensitivity, FocusMode(AF-S), S1, FocusIndication, unnamed code.
    "properties": {
        0x0104: 100,     # IsoSensitivity
        0x0109: 2,       # FocusMode, AF-S
        0x0001: 1,       # S1, unlocked
        0x0119: 2,       # StillImageStoreDestination, memory card
        0x0702: 87,      # BatteryRemain, percent
        0x0703: 4,       # BatteryLevel, 3/4
        0x0705: 0,       # RecordingState, not recording
        0x0707: 1,       # FocusIndication, unlocked
        0x0708: 0,       # MediaSLOT1_Status, ok
        0x0709: 1234,    # MediaSLOT1_RemainingNumber
        0x070A: 5678,    # MediaSLOT1_RemainingTime
        0x7FFE: 42,      # a deliberately unnamed code
    },
    "af_outcome": "focus",
    "mode": 0,             # control mode of the open session
    "contents": [],        # durable items, oldest first
    "next_content_id": 131_400,
    "last_preview_content": None,
    "postview_configured": False,
    "postview_pending": 0,      # announced byte length, 0 when nothing waiting
    "frame_number": 0,
    "live_view_blocked": 0,     # frames still to be swallowed after a capture
}

#: Vendor control mode that exposes the content index.
MODE_REMOTE_TRANSFER = 2

#: Property codes the fake host models, mirroring the characterized body.
CODE_S1 = 0x0001
CODE_DESTINATION = 0x0119
CODE_BATTERY_REMAIN = 0x0702
CODE_BATTERY_LEVEL = 0x0703
CODE_RECORDING_STATE = 0x0705
CODE_FOCUS_INDICATION = 0x0707
CODE_SLOT1_STATUS = 0x0708
CODE_SLOT1_SHOTS = 0x0709
CODE_SLOT1_SECONDS = 0x070A

#: Live view. Sizes vary frame to frame, as they do on real hardware.
LIVE_VIEW_WIDTH = 640
LIVE_VIEW_HEIGHT = 428

#: Postview is a full-resolution JPEG, so it is much larger than any preview.
POSTVIEW_WIDTH = 4240
POSTVIEW_HEIGHT = 2832
POSTVIEW_BYTES = 262_144

if BEHAVIOUR == "af_no_lock":
    STATE["af_outcome"] = "no_lock"
elif BEHAVIOUR == "af_tracking":
    STATE["af_outcome"] = "tracking"
    STATE["properties"][0x0109] = 3   # AF-C



def camera_info(index: int) -> _cabi.CameraInfoStruct:
    info = _cabi.CameraInfoStruct()
    info.device_key = b"FAKE-CAM:%04d" % index
    info.model = b"SIM-HostCamera"
    info.serial = b"FAKE%06d" % index
    info.firmware = b"1.00"
    info.transport = b"USB"
    info.adapter = b"Fake_PTP_USB"
    info.usb_pid = 0x0F52
    return info


def make_property(code: int, value: int) -> _cabi.PropertyStruct:
    prop = _cabi.PropertyStruct()
    prop.code = code
    prop.value = value
    prop.value_type = 1
    prop.access = 3
    prop.allowed_count = 0
    return prop


def make_event(kind: int, code: int, i0: int = 0, i1: int = 0) -> _cabi.EventStruct:
    event = _cabi.EventStruct()
    event.kind = kind
    event.code = code
    event.i0 = i0
    event.i1 = i1   # focus events use this to name the reporting channel
    event.timestamp_ms = 1234
    return event


def respond(out, request_id, *, status=0, category=0, message=b"",
            handle=0, i32=0, items=None, item_size=0, blob=None, count=None,
            tail=b""):
    if blob is None:
        blob = b"".join(_ipc.as_bytes(item) for item in (items or []))
    if count is None:
        count = len(items) if items else 0
    response = _ipc.ResponseStruct(
        status=status,
        category=category,
        count=count,
        item_size=item_size,
        handle=handle,
        i32_result=i32,
        message=message,
    )
    # A tail after the fixed struct is how operations return more than it
    # holds; see ipc_protocol.h.
    meta = _ipc.as_bytes(response) + tail
    out.write(_ipc.encode(_ipc.MSG_RESPONSE, request_id, meta, blob))
    out.flush()


def synth_jpeg(seed: int, length: int, width: int, height: int) -> bytes:
    """A real, parseable JPEG of the requested geometry."""
    header = b"\xff\xd8" + b"\xff\xc0" + struct.pack(
        ">HBHHBBBB", 11, 8, height, width, 1, 1, 0x11, 0
    )
    body_length = max(0, length - len(header) - 2)
    pattern = bytes(((seed * 7 + i) % 251) + 1 for i in range(min(64, body_length)))
    filler = bytes([(seed % 250) + 1]) * (body_length - len(pattern))
    return header + pattern + filler + b"\xff\xd9"


#: Geometry and rough size of each preview form, following the characterized
#: body: a screennail is a usable image, a thumbnail is not.
PREVIEW_SHAPE = {
    _cabi.PREVIEW_THUMBNAIL: (160, 120, 4_096),
    _cabi.PREVIEW_SCREENNAIL: (1616, 1080, 32_768),
}


def make_content(record: dict) -> _cabi.ContentStruct:
    item = _cabi.ContentStruct()
    item.content_id = record["content_id"]
    item.file_id = record["file_id"]
    item.file_number = record["file_number"]
    item.dir_number = 100
    item.content_type = 1
    item.file_format = 0xB101
    item.image_width = 4240
    item.image_height = 2832
    item.file_size = 24_000_000
    item.slot = 1
    item.file_count = 1
    item.created_year = 2026
    item.created_month = 8
    item.created_day = 18
    item.created_hour = 12
    item.created_minute = 0
    item.created_second = record["content_id"] % 60
    item.created_millisecond = 0
    item.path = record["path"].encode("utf-8")
    return item


def record_capture() -> None:
    """Add one durable item, as an exposure would.

    The identifier jumps by more than one on purpose: hardware was observed
    skipping values, so anything that assumes ``baseline + 1`` must fail here.
    """
    STATE["next_content_id"] += 3
    content_id = STATE["next_content_id"]
    number = 3400 + len(STATE["contents"])
    STATE["contents"].append({
        "content_id": content_id,
        "file_id": 1,
        "file_number": number,
        "path": f"A:/DCIM/100MSDCF/DSC{number:05d}.ARW",
    })
    # The camera announces that the list changed without saying what appeared.
    STATE["events"].append(make_event(4, 0, 1, 1))


def record_postview() -> None:
    """Announce a postview, as a capture with a host destination would.

    Delivery follows the still destination, not whether configuration was
    accepted: that pair was observed disagreeing on real hardware, so the two
    are modelled independently here.
    """
    if BEHAVIOUR == "no_postview":
        return
    destination = STATE["properties"].get(CODE_DESTINATION, 2)
    if destination not in (1, 3):      # host, or host and card
        return
    if BEHAVIOUR == "empty_postview":
        STATE["postview_pending"] = 1  # announced, but nothing real behind it
        return
    STATE["postview_pending"] = POSTVIEW_BYTES


def valid_session(handle: int) -> bool:
    record = STATE["sessions"].get(handle)
    return bool(record and record["open"])


def _drive_autofocus(*, engaged: bool) -> None:
    """Model the half-press starting or cancelling autofocus.

    Both focus channels report, using their own separate encodings, exactly as
    the characterized hardware does.
    """
    if not engaged:
        STATE["properties"][0x0707] = 1          # Unlocked
        return
    outcome = STATE["af_outcome"]
    if outcome == "no_lock":
        indication, af_status = 0x0202, 3        # NotFocused_AF_S
    elif outcome == "tracking":
        # Tracking arrives first and must never gate a release.
        STATE["properties"][0x0707] = 0x0303
        STATE["events"].append(make_event(2, 0x60001, 5, _cabi.FOCUS_SRC_WARNING))
        indication, af_status = 0x0103, 6        # then Focused_AF_C
    else:
        indication, af_status = 0x0102, 2        # Focused_AF_S
    STATE["properties"][0x0707] = indication
    # The warning channel uses its own enumeration.
    STATE["events"].append(make_event(2, 0x60001, af_status, _cabi.FOCUS_SRC_WARNING))
    STATE["events"].append(make_event(1, 0x0707))


def handle_request(out, request_id: int, meta: bytes) -> bool:
    request = _ipc.from_bytes(_ipc.RequestStruct, meta)
    op = request.op
    fixed = _cabi.ctypes.sizeof(_ipc.RequestStruct)
    content_args = (
        _ipc.from_bytes(_ipc.ContentArgsStruct, meta[fixed:])
        if len(meta) >= fixed + _cabi.ctypes.sizeof(_ipc.ContentArgsStruct)
        else _ipc.ContentArgsStruct()
    )

    if op == _ipc.OP_PING:
        respond(out, request_id)
        return True

    if op == _ipc.OP_INIT:
        if BEHAVIOUR == "sdk_missing":
            respond(out, request_id, status=-8, category=_ipc.CAT_SDK_MISSING,
                    message=b"the vendor runtime could not be loaded")
            return True
        STATE["initialised"] = True
        respond(out, request_id)
        return True

    if op == _ipc.OP_SHUTDOWN:
        STATE["initialised"] = False
        respond(out, request_id)
        return True

    if op == _ipc.OP_ENUMERATE:
        if BEHAVIOUR == "adapter_failure":
            respond(out, request_id, status=0x8703, category=_ipc.CAT_ADAPTER_PATH,
                    message=b"the vendor SDK could not create a transport adapter")
            return True
        count = 0 if BEHAVIOUR == "no_camera" else 1
        items = [camera_info(i) for i in range(count)]
        respond(out, request_id, items=items,
                item_size=_cabi.ctypes.sizeof(_cabi.CameraInfoStruct))
        return True

    if op == _ipc.OP_OPEN_SESSION:
        # The camera was still holding a previous transport session, so Connect
        # was accepted and the connection callback never came. A real host has
        # already disconnected and released the device by the time it answers
        # this, which is why one more attempt is worth making.
        if BEHAVIOUR == "connect_timeout_always" or (
            BEHAVIOUR == "connect_timeout_once" and not STATE["open_attempts"]
        ):
            STATE["open_attempts"] += 1
            respond(out, request_id, status=-9,
                    category=_ipc.CAT_CONNECT_TIMEOUT,
                    message=b"timed out waiting for the connection callback")
            return True
        STATE["open_attempts"] += 1
        STATE["generation"] += 1
        slot = STATE["next_slot"]
        STATE["next_slot"] += 1
        handle = (STATE["generation"] << 32) | slot
        STATE["sessions"][handle] = {"open": True}
        STATE["mode"] = request.i32_arg
        # Queue the connection events a real session would produce.
        STATE["events"] = [
            make_event(_ipc.CAT_NONE, 0, 0),
        ]
        STATE["events"] = [make_event(0, 0, 1)]  # CONNECTION -> connected
        respond(out, request_id, handle=handle)
        return True

    if op == _ipc.OP_CLOSE_SESSION:
        record = STATE["sessions"].get(request.handle)
        if record:
            record["open"] = False
        respond(out, request_id)  # idempotent, never an error
        return True

    if op == _ipc.OP_CONNECTION_STATE:
        state = 1 if valid_session(request.handle) else 4
        respond(out, request_id, i32=state)
        return True

    if not valid_session(request.handle) and op in (
        _ipc.OP_POLL_EVENTS,
        _ipc.OP_LIST_PROPERTIES,
        _ipc.OP_GET_PROPERTY,
        _ipc.OP_LIST_CONTENT,
        _ipc.OP_CONTENT_PREVIEW,
        _ipc.OP_CONFIGURE_POSTVIEW,
        _ipc.OP_PULL_POSTVIEW,
        _ipc.OP_LIVE_VIEW_INFO,
        _ipc.OP_LIVE_VIEW_FRAME,
    ):
        respond(out, request_id, status=-5, category=_ipc.CAT_STALE_HANDLE,
                message=b"stale or unknown session handle")
        return True

    if op == _ipc.OP_POLL_EVENTS:
        items = STATE["events"]
        STATE["events"] = []
        respond(out, request_id, items=items,
                item_size=_cabi.ctypes.sizeof(_cabi.EventStruct))
        return True

    if op == _ipc.OP_LIST_PROPERTIES:
        items = [
            make_property(code, value)
            for code, value in sorted(STATE["properties"].items())
        ]
        respond(out, request_id, items=items,
                item_size=_cabi.ctypes.sizeof(_cabi.PropertyStruct))
        return True

    if op == _ipc.OP_SEND_COMMAND:
        # A successful release clears the half-press stage, as characterized.
        if request.u32_arg in (0, 7) and request.i32_arg == 0:
            STATE["properties"][0x0001] = 1
            STATE["properties"][0x0707] = 1
        if request.i32_arg not in (0, 1):
            respond(out, request_id, status=-4, category=_ipc.CAT_INVALID_ARG,
                    message=b"command parameter must be up or down")
            return True
        # Release up completes an exposure, mirroring the characterized body.
        if request.u32_arg in (0, 7) and request.i32_arg == 0:
            STATE["events"].append(make_event(3, 0))   # CAPTURE
            record_capture()
            record_postview()
            # Live view pauses briefly around an exposure and resumes on its
            # own; nothing has to restart it.
            STATE["live_view_blocked"] = 2
        # The movie-record button is a toggle, so a press flips the state.
        if request.u32_arg == 1 and request.i32_arg == 0:
            current = STATE["properties"].get(CODE_RECORDING_STATE, 0)
            STATE["properties"][CODE_RECORDING_STATE] = 0 if current else 1
            STATE["events"].append(make_event(1, CODE_RECORDING_STATE))
        respond(out, request_id)
        return True

    if op == _ipc.OP_CONFIGURE_POSTVIEW:
        if BEHAVIOUR == "postview_config_refused":
            # Observed on hardware in one control mode. Delivery is unaffected.
            respond(out, request_id, status=-11, category=_ipc.CAT_UNSUPPORTED,
                    message=b"the camera refused to configure postview in this "
                            b"control mode")
            return True
        STATE["postview_configured"] = bool(request.i32_arg)
        respond(out, request_id)
        return True

    if op == _ipc.OP_PULL_POSTVIEW:
        size = STATE["postview_pending"]
        if not size:
            respond(out, request_id, count=0)   # nothing announced yet
            return True
        STATE["postview_pending"] = 0
        if BEHAVIOUR == "empty_postview":
            respond(out, request_id, status=-4, category=_ipc.CAT_INVALID_ARG,
                    message=b"the camera announced a postview of zero bytes")
            return True
        data = synth_jpeg(7, size, POSTVIEW_WIDTH, POSTVIEW_HEIGHT)
        if BEHAVIOUR == "torn_postview":
            data = b"\xff\xd8" + b"\x00" * 32
        info = _cabi.PostviewInfoStruct(
            byte_length=len(data),
            notified_ms=2000,
            pulled_ms=2002,
            filename=b"DSC03400.JPG",
        )
        respond(out, request_id, blob=data, count=len(data), item_size=1,
                tail=_ipc.as_bytes(info))
        return True

    if op == _ipc.OP_LIVE_VIEW_INFO:
        usable = STATE["mode"] != MODE_REMOTE_TRANSFER
        info = _cabi.LiveViewInfoStruct(
            # The measured contradiction: the query succeeds in both modes,
            # but one of them reports a buffer that cannot produce a frame.
            info_ok=1,
            vendor_error=0,
            width=LIVE_VIEW_WIDTH if usable else 0,
            height=LIVE_VIEW_HEIGHT if usable else 0,
            buffer_size=131_072 if usable else 0,
        )
        respond(out, request_id, tail=_ipc.as_bytes(info))
        return True

    if op == _ipc.OP_LIVE_VIEW_FRAME:
        if STATE["mode"] == MODE_REMOTE_TRANSFER:
            respond(out, request_id, status=-11, category=_ipc.CAT_UNSUPPORTED,
                    message=b"the camera reports a zero-byte live-view buffer")
            return True
        if STATE["live_view_blocked"] > 0:
            STATE["live_view_blocked"] -= 1
            respond(out, request_id, count=0)   # the gap around an exposure
            return True
        STATE["frame_number"] += 1
        number = STATE["frame_number"]
        if BEHAVIOUR == "stuck_live_view":
            number = 1          # the camera keeps handing back one frame
        # Sizes vary frame to frame, as they do on real hardware.
        size = 28_000 + (number * 997) % 59_000
        data = synth_jpeg(number, size, LIVE_VIEW_WIDTH, LIVE_VIEW_HEIGHT)
        info = _cabi.FrameInfoStruct(
            byte_length=len(data),
            frame_number=number,
            width=LIVE_VIEW_WIDTH,
            height=LIVE_VIEW_HEIGHT,
            time_code=number * 33,
            fetched_ms=number * 33,
        )
        respond(out, request_id, blob=data, count=len(data), item_size=1,
                tail=_ipc.as_bytes(info))
        return True

    if op == _ipc.OP_LIST_CONTENT:
        if STATE["mode"] != MODE_REMOTE_TRANSFER:
            respond(out, request_id, status=-11, category=_ipc.CAT_UNSUPPORTED,
                    message=b"the content index needs the RemoteTransfer mode")
            return True
        after = content_args.after_content_id
        items = [
            make_content(record)
            for record in STATE["contents"]
            if record["content_id"] > after
        ]
        respond(out, request_id, items=items,
                item_size=_cabi.ctypes.sizeof(_cabi.ContentStruct))
        return True

    if op == _ipc.OP_CONTENT_PREVIEW:
        if STATE["mode"] != MODE_REMOTE_TRANSFER:
            respond(out, request_id, status=-11, category=_ipc.CAT_UNSUPPORTED,
                    message=b"previews need the RemoteTransfer mode")
            return True
        wanted = content_args.content_id
        known = {record["content_id"] for record in STATE["contents"]}
        if wanted not in known:
            respond(out, request_id, status=-7, category=_ipc.CAT_NOT_FOUND,
                    message=b"no such content id on this camera")
            return True
        served = wanted
        if BEHAVIOUR == "stale_preview" and STATE["last_preview_content"] is not None:
            # Answer with the previous still. A client that trusts the bytes
            # without checking identity will show the wrong image.
            served = STATE["last_preview_content"]
        STATE["last_preview_content"] = wanted

        width, height, size = PREVIEW_SHAPE.get(
            content_args.kind, (640, 480, 8_192)
        )
        data = synth_jpeg(served * 31 + content_args.kind, size, width, height)
        if BEHAVIOUR == "torn_preview":
            data = b"\xff\xd8" + b"\x00" * 64   # signature only, no frame header

        info = _cabi.PreviewInfoStruct(
            content_id=served,
            file_id=content_args.file_id,
            kind=content_args.kind,
            vendor_notify=0,
            byte_length=len(data),
            slot=content_args.slot or 1,
            deliveries=1,
            last_percent=100,
            requested_ms=1000,
            completed_ms=1086,
        )
        respond(out, request_id, blob=data, count=len(data), item_size=1,
                tail=_ipc.as_bytes(info))
        return True

    if op == _ipc.OP_SET_PROPERTY:
        if request.u32_arg == 0x0707:   # FocusIndication is read-only
            respond(out, request_id, status=-11, category=_ipc.CAT_UNSUPPORTED,
                    message=b"the camera reports this property as not settable")
            return True
        if request.u32_arg not in STATE["properties"]:
            respond(out, request_id, status=-7, category=_ipc.CAT_NOT_FOUND,
                    message=b"camera does not expose that property")
            return True
        value = (request.i32_arg2 << 32) | (request.i32_arg & 0xFFFFFFFF)
        STATE["properties"][request.u32_arg] = value
        # A real camera reports the change back as a property event.
        STATE["events"].append(make_event(1, request.u32_arg))
        if request.u32_arg == 0x0001:
            _drive_autofocus(engaged=value == 2)
        respond(out, request_id)
        return True

    if op == _ipc.OP_GET_PROPERTY:
        known = STATE["properties"]
        if request.u32_arg not in known:
            respond(out, request_id, status=-7, category=_ipc.CAT_NOT_FOUND,
                    message=b"camera does not expose that property")
            return True
        item = make_property(request.u32_arg, known[request.u32_arg])
        respond(out, request_id, items=[item],
                item_size=_cabi.ctypes.sizeof(_cabi.PropertyStruct))
        return True

    if op == _ipc.OP_TEST_CRASH:
        out.flush()
        os._exit(97)

    respond(out, request_id, status=-11, category=_ipc.CAT_UNSUPPORTED,
            message=b"operation is not implemented by this host")
    return True


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    if BEHAVIOUR == "garbage":
        stdout.write(b"this is not a frame at all, not even close")
        stdout.flush()
        return 0

    while True:
        frame = _ipc.read_frame(stdin)
        if frame is None:
            return 0
        message_type, request_id, meta, _blob = frame

        if message_type == _ipc.MSG_HELLO:
            if BEHAVIOUR == "die_on_hello":
                return 91
            ack = _ipc.HelloAckStruct(
                protocol_major=(
                    99 if BEHAVIOUR == "bad_protocol" else _ipc.VERSION_MAJOR
                ),
                protocol_minor=_ipc.VERSION_MINOR,
                abi_major=99 if BEHAVIOUR == "bad_abi" else _cabi.ABI_VERSION_MAJOR,
                abi_minor=0,
                host_version=(1 << 16),
                sdk_available=0 if BEHAVIOUR == "sdk_missing" else 1,
                host_build=b"fake_host",
                sdk_note=b"pure-Python stand-in; no vendor SDK involved",
            )
            stdout.write(
                _ipc.encode(_ipc.MSG_HELLO_ACK, request_id, _ipc.as_bytes(ack))
            )
            stdout.flush()
            continue

        if message_type == _ipc.MSG_BYE:
            return 0

        if message_type != _ipc.MSG_REQUEST:
            respond(stdout, request_id, status=-4, category=_ipc.CAT_INVALID_ARG,
                    message=b"unknown message type")
            continue

        handle_request(stdout, request_id, meta)


if __name__ == "__main__":
    sys.exit(main())
