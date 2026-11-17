import pytest

import crsdkpy
from crsdkpy.backend import _cabi
from crsdkpy.backend.native import build_live_view_frame
from crsdkpy.enums import PreviewKind
from test_host_ipc import make_backend


@pytest.mark.parametrize('behaviour', [
    'live_view_offset_payload', 'live_view_truncated_payload',
    'live_view_bad_geometry', 'live_view_wrong_length',
])
def test_host_rejects_invalid_jpeg_payload(behaviour):
    with crsdkpy.SDK(backend=make_backend(behaviour)) as sdk:
        with sdk.discover()[0].open('remote') as session:
            with pytest.raises(crsdkpy.CameraConnectionError, match='live-view JPEG'):
                session.live_view.get_frame(timeout_ms=100)


def test_host_uses_jpeg_geometry_and_preserves_identity_metadata():
    with crsdkpy.SDK(backend=make_backend('live_view_actual_geometry')) as sdk:
        with sdk.discover()[0].open('remote') as session:
            frame = session.live_view.get_frame(timeout_ms=100)
            assert (frame.width, frame.height) == (1024, 680)
            assert frame.metadata['geometry_source'] == 'frame'
            assert frame.frame_number == 1 and frame.metadata['time_code'] == 33
            assert frame.timestamp_ms >= 0
            assert frame.kind is PreviewKind.LIVE_VIEW and not frame.is_exact_still


def test_zero_payload_is_not_a_valid_jpeg():
    with pytest.raises(crsdkpy.CameraConnectionError, match='live-view JPEG'):
        build_live_view_frame(b'', _cabi.FrameInfoStruct(), timestamp_ms=1)


def test_host_rejects_inconsistent_ipc_frame_count():
    with crsdkpy.SDK(backend=make_backend('live_view_wrong_ipc_count')) as sdk:
        with sdk.discover()[0].open('remote') as session:
            with pytest.raises(crsdkpy.BackendError, match='IPC payload length'):
                session.live_view.get_frame(timeout_ms=100)
