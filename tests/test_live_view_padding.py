import struct

import pytest

import crsdkpy
from crsdkpy.backend import _cabi
from crsdkpy.backend.native import build_live_view_frame
from test_host_ipc import make_backend


def jpeg():
    # Synthetic SOF/SOS fixture, not a photograph or a pixel-decode fixture.
    return (b'\xff\xd8\xff\xc0' + struct.pack('>HBHHBBBB', 11, 8, 680, 1024,
                                                  1, 1, 0x11, 0)
            + b'\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00'
            + b'\x01\xff\x00\x02\xff\xd9')


def decode(data, length=None):
    info = _cabi.FrameInfoStruct(byte_length=len(data) if length is None else length,
                                frame_number=11, time_code=42, width=640, height=428)
    return build_live_view_frame(data, info, timestamp_ms=1234)


@pytest.mark.parametrize('padding', [0, 1, 45, 64])
def test_bounded_ff_padding_preserves_frame_contract(padding):
    original = jpeg()
    frame = decode(original + b'\xff' * padding)
    assert frame.data == original
    if padding == 0:
        assert frame.data is original
    assert frame.data.endswith(b'\xff\xd9')
    assert frame.byte_length == frame.metadata['byte_length'] == len(original)
    assert frame.metadata['raw_payload_length'] == len(original) + padding
    assert frame.metadata['stripped_padding_bytes'] == padding
    assert (frame.width, frame.height) == (1024, 680)
    assert frame.frame_number == 11 and frame.timestamp_ms == 1234
    assert frame.metadata['time_code'] == 42
    assert frame.kind is crsdkpy.PreviewKind.LIVE_VIEW and not frame.is_exact_still


@pytest.mark.parametrize('data', [
    jpeg() + b'\xff' * 65,
    jpeg() + b'\x00',
    jpeg() + b'garbage\xff',
    jpeg()[:-2] + b'\xff' * 45,
    jpeg()[:-1] + b'\xff' * 45,
    b'header' + jpeg() + b'\xff' * 45,
    b'\xff\xd8\xff\xd9' + b'\xff' * 45,
])
def test_padding_cannot_rescue_malformed_frames(data):
    with pytest.raises(crsdkpy.CameraConnectionError, match='live-view JPEG'):
        decode(data)


def test_raw_length_is_checked_before_normalization():
    raw = jpeg() + b'\xff' * 45
    with pytest.raises(crsdkpy.CameraConnectionError, match='raw payload length'):
        decode(raw, length=len(jpeg()))


def test_embedded_eoi_is_not_a_truncation_boundary():
    # EOI-looking bytes inside an APP segment must remain intact.
    original = jpeg()
    original = original[:2] + b'\xff\xe1\x00\x06x\xff\xd9y' + original[2:]
    assert decode(original + b'\xff' * 45).data == original


def test_hosted_padding_and_simulator_metadata_are_consistent():
    for backend in ('simulator', make_backend('live_view_ff_padding')):
        with crsdkpy.SDK(backend=backend) as sdk:
            with sdk.discover()[0].open('remote') as session:
                frame = session.live_view.get_frame(timeout_ms=100)
                assert (frame.byte_length == frame.metadata['byte_length']
                        == len(frame.data))
                padding = frame.metadata['stripped_padding_bytes']
                assert padding == (0 if backend == 'simulator' else 45)
                assert frame.metadata['raw_payload_length'] == len(frame.data) + padding
                assert frame.data.endswith(b'\xff\xd9')
