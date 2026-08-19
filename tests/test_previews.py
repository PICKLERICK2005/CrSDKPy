"""Preview forms and their exactness guarantees."""

from __future__ import annotations

import pytest

import crsdkpy
from conftest import make_sdk
from crsdkpy._jpeg import jpeg_dimensions, looks_complete
from crsdkpy.simulator import Scenario


def test_live_view_frame_is_never_an_exact_still(session: crsdkpy.Session) -> None:
    frame = session.live_view.get_frame()
    assert isinstance(frame, crsdkpy.LiveViewFrame)
    assert not frame.is_exact_still
    assert frame.kind is crsdkpy.PreviewKind.LIVE_VIEW


def test_preview_kind_exactness() -> None:
    assert not crsdkpy.PreviewKind.LIVE_VIEW.is_exact_still
    assert crsdkpy.PreviewKind.POSTVIEW.is_exact_still
    assert crsdkpy.PreviewKind.THUMBNAIL.is_exact_still
    assert crsdkpy.PreviewKind.SCREENNAIL.is_exact_still


def test_screennail_is_exact(transfer_session: crsdkpy.Session) -> None:
    capture = transfer_session.autofocus_and_capture()
    content = capture.wait_for_content()
    preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
    assert preview.is_exact_still
    assert preview.content_id == content.content_id
    assert preview.byte_length > 0
    assert preview.data[:2] == b"\xff\xd8"


def test_thumbnail_is_exact(transfer_session: crsdkpy.Session) -> None:
    capture = transfer_session.autofocus_and_capture()
    capture.wait_for_content()
    preview = capture.preview(crsdkpy.PreviewKind.THUMBNAIL)
    assert preview.is_exact_still
    assert preview.width == 160


def test_live_view_is_rejected_as_a_capture_preview(
    transfer_session: crsdkpy.Session,
) -> None:
    capture = transfer_session.autofocus_and_capture()
    with pytest.raises(crsdkpy.UnsupportedOperationError):
        capture.preview(crsdkpy.PreviewKind.LIVE_VIEW)


def test_postview_unavailable_without_host_destination(
    transfer_session: crsdkpy.Session,
) -> None:
    capture = transfer_session.autofocus_and_capture()
    with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
        capture.preview(crsdkpy.PreviewKind.POSTVIEW)
    assert excinfo.value.capability == "postview_delivery"


def test_postview_delivered_with_host_destination(camera: crsdkpy.Camera) -> None:
    with camera.open("remote_transfer") as session:
        session.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
        capture = session.autofocus_and_capture()
        preview = capture.preview(crsdkpy.PreviewKind.POSTVIEW)
        assert preview.is_exact_still
        assert preview.byte_length > 0


def test_postview_works_where_configuration_is_rejected(
    camera: crsdkpy.Camera,
) -> None:
    """The headline capability finding, exercised end to end.

    In this mode the configuration call is refused, yet postview bytes are
    still delivered once the destination includes the host.
    """
    with camera.open("remote") as session:
        session.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
        caps = session.capabilities
        assert not caps.postview_configuration
        assert caps.postview_delivery

        with pytest.raises(crsdkpy.UnsupportedOperationError):
            session._backend.configure_postview(session._id, enabled=True)

        capture = session.autofocus_and_capture()
        preview = capture.preview(crsdkpy.PreviewKind.POSTVIEW)
        assert preview.is_exact_still
        assert preview.byte_length > 0


def test_preview_unavailable_in_mode_without_content(
    camera: crsdkpy.Camera,
) -> None:
    with camera.open("remote") as session:
        capture = session.autofocus_and_capture()
        with pytest.raises(crsdkpy.UnsupportedOperationError):
            capture.preview(crsdkpy.PreviewKind.SCREENNAIL)


def test_stale_preview_is_refused() -> None:
    """A camera returning the previous shot's bytes must not go unnoticed."""
    sdk = make_sdk(scenario=Scenario(stale_preview=True))
    camera = sdk.discover()[0]
    try:
        with camera.open("remote_transfer") as session:
            first = session.autofocus_and_capture()
            first.wait_for_content()
            first.preview(crsdkpy.PreviewKind.SCREENNAIL)

            second = session.autofocus_and_capture()
            second.wait_for_content()
            with pytest.raises(crsdkpy.CameraConnectionError, match="stale"):
                second.preview(crsdkpy.PreviewKind.SCREENNAIL)
    finally:
        sdk.close()


def test_preview_is_cached_per_capture(transfer_session: crsdkpy.Session) -> None:
    capture = transfer_session.autofocus_and_capture()
    capture.wait_for_content()
    first = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
    second = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
    assert first is second


@pytest.mark.parametrize(
    "kind", [crsdkpy.PreviewKind.THUMBNAIL, crsdkpy.PreviewKind.SCREENNAIL]
)
def test_simulated_previews_parse_like_real_ones(
    transfer_session: crsdkpy.Session, kind
) -> None:
    """Parity with the native path, which reads geometry from the bytes.

    A simulator that only claimed dimensions would let a parsing bug through.
    """
    capture = transfer_session.autofocus_and_capture()
    capture.wait_for_content()
    preview = capture.preview(kind)
    assert looks_complete(preview.data)
    assert jpeg_dimensions(preview.data) == (preview.width, preview.height)


def test_simulated_previews_carry_the_same_metadata_as_native(
    transfer_session: crsdkpy.Session,
) -> None:
    capture = transfer_session.autofocus_and_capture()
    content = capture.wait_for_content()
    preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
    assert preview.metadata["exact_still_association"] == "content_id"
    assert preview.metadata["filename"] == content.filename
    assert preview.metadata["file_id"] == content.file_id


def test_preview_sizes_are_profile_specific() -> None:
    """No universal postview size; it differs per camera."""
    with make_sdk(profile="inverted_modes") as sdk:
        camera = sdk.discover()[0]
        with camera.open("remote") as session:
            capture = session.autofocus_and_capture()
            capture.wait_for_content()
            preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
            assert (preview.width, preview.height) == (1920, 1080)
