"""Content index, thumbnails and screennails.

Runs against the pure-Python fake host, so no native build, no vendor SDK and
no camera are needed. The behaviours modelled here are the ones hardware
actually exhibits: identifiers that skip values, a change notification that
names nothing, and previews whose only proof of identity is the content id
they were requested for.
"""

from __future__ import annotations

import os
import sys

import pytest

import crsdkpy
from crsdkpy._jpeg import (
    is_jpeg,
    jpeg_dimensions,
    jpeg_segments,
    looks_complete,
)
from crsdkpy.backend.host import HostBackend

FAKE_HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_host.py")

THUMBNAIL = crsdkpy.PreviewKind.THUMBNAIL
SCREENNAIL = crsdkpy.PreviewKind.SCREENNAIL


def make_backend(behaviour: str = "normal", **kwargs) -> HostBackend:
    return HostBackend(
        command=[sys.executable, FAKE_HOST, behaviour],
        adapter_dir=os.path.dirname(FAKE_HOST),
        **kwargs,
    )


def open_session(sdk, mode="remote_transfer"):
    camera = sdk.discover()[0]
    return camera.open(mode)


# -- JPEG inspection --------------------------------------------------------
def test_non_jpeg_bytes_are_rejected() -> None:
    assert not is_jpeg(b"GIF89a")
    assert jpeg_dimensions(b"GIF89a") is None


def test_dimensions_come_from_the_frame_header() -> None:
    from tests.fake_host import synth_jpeg

    data = synth_jpeg(1, 4096, 1616, 1080)
    assert is_jpeg(data)
    assert jpeg_dimensions(data) == (1616, 1080)
    assert looks_complete(data)


def test_a_signature_without_a_frame_header_is_not_readable() -> None:
    """A truncated transfer keeps a valid signature; that is the trap."""
    assert is_jpeg(b"\xff\xd8" + b"\x00" * 64)
    assert jpeg_dimensions(b"\xff\xd8" + b"\x00" * 64) is None


# -- structure walking ------------------------------------------------------
def _jpeg_with_metadata(payload: bytes, scan: bytes) -> bytes:
    """A JPEG carrying an APP1 segment and some entropy-coded data."""
    import struct as _struct

    sof = b"\xff\xc0" + _struct.pack(">HBHHBBBB", 11, 8, 8, 8, 1, 1, 0x11, 0)
    app1 = b"\xff\xe1" + _struct.pack(">H", len(payload) + 2) + payload
    sos = b"\xff\xda" + _struct.pack(">HBBBBBB", 8, 1, 1, 0, 0, 63, 0)
    return b"\xff\xd8" + app1 + sof + sos + scan + b"\xff\xd9"


def test_segments_separate_metadata_from_picture() -> None:
    data = _jpeg_with_metadata(b"Exif\x00\x00some-identifier", b"\x11\x22\x33\x44")
    found = {segment.name: segment for segment in jpeg_segments(data)}
    assert "FFE1" in found and found["FFE1"].is_metadata
    assert "FFC0" in found and not found["FFC0"].is_metadata
    assert "SCAN" in found and found["SCAN"].is_scan
    # The scan runs to the end of the file, trailing marker included.
    assert found["SCAN"].end == len(data)


def test_segments_do_not_scan_inside_compressed_data() -> None:
    """Picture bytes can contain anything, markers included."""
    data = _jpeg_with_metadata(b"Exif\x00\x00x", b"\xff\xc0\xff\xe1\xff\xda")
    scans = [s for s in jpeg_segments(data) if s.is_scan]
    assert len(scans) == 1
    # Nothing after the start of scan was mistaken for a segment.
    assert [s.name for s in jpeg_segments(data)][-1] == "SCAN"


def test_segments_of_a_non_jpeg_are_empty() -> None:
    assert list(jpeg_segments(b"not a jpeg")) == []


def test_metadata_and_picture_regions_are_disjoint_and_ordered() -> None:
    data = _jpeg_with_metadata(b"Exif\x00\x00identifier", b"\x01\x02\x03")
    previous_end = 2
    for segment in jpeg_segments(data):
        assert segment.start >= previous_end
        previous_end = segment.end


# -- capability gating ------------------------------------------------------
def test_content_index_is_absent_in_remote_mode() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, "remote") as session:
            caps = session.capabilities
            assert not caps.content_index
            assert not caps.thumbnail
            assert not caps.screennail


def test_content_index_is_present_in_remote_transfer_mode() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            caps = session.capabilities
            assert caps.content_index
            assert caps.thumbnail
            assert caps.screennail


def test_content_calls_in_the_wrong_mode_name_the_capability() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, "remote") as session:
            backend = sdk.backend
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                backend.latest_content(session._id)
            assert excinfo.value.capability == "content_index"
            # Refused for the reason that is actually true, not as "unbuilt".
            assert "remote_transfer" in str(excinfo.value)
            assert "not implemented yet" not in str(excinfo.value)


def test_preview_kinds_outside_the_index_are_refused_by_name() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                sdk.backend.get_preview(
                    session._id, 1, crsdkpy.PreviewKind.POSTVIEW
                )
            assert excinfo.value.capability == "postview_delivery"


# -- the index --------------------------------------------------------------
def test_index_is_empty_before_anything_is_shot() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert sdk.backend.latest_content(session._id) is None


def test_a_capture_produces_exactly_one_new_item() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            backend = sdk.backend
            baseline = backend.latest_content(session._id)
            assert baseline is None

            capture = session.capture()
            assert capture.exposed
            fresh = backend.list_content(session._id)
            assert len(fresh) == 1


def test_new_content_is_detected_against_a_baseline_not_by_increment() -> None:
    """Identifiers are monotonic but skip values, so ``+1`` is never valid."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            backend = sdk.backend
            session.capture()
            first = backend.latest_content(session._id)
            session.capture()
            second = backend.latest_content(session._id)

            assert second.content_id > first.content_id
            assert second.content_id != first.content_id + 1
            newer = backend.list_content(session._id, newer_than=first.content_id)
            assert [c.content_id for c in newer] == [second.content_id]


def test_content_carries_its_identity_and_filename() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            session.capture()
            item = sdk.backend.latest_content(session._id)
            assert item.file_id == 1
            assert item.filename == "DSC03400.ARW"
            assert item.path.endswith("/DSC03400.ARW")
            assert item.file_number == 3400
            assert item.captured_at.startswith("2026-08-18T")
            # Geometry of the original still, not of any preview of it.
            assert (item.width, item.height) == (4240, 2832)


def test_a_capture_resolves_its_own_content() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            content = capture.wait_for_content(timeout_ms=2_000)
            assert content.content_id == sdk.backend.latest_content(
                session._id
            ).content_id
            assert content.filename == "DSC03400.ARW"
            assert capture.state is crsdkpy.CaptureState.CONTENT_AVAILABLE


# -- previews ---------------------------------------------------------------
@pytest.mark.parametrize(
    "kind,expected",
    [(THUMBNAIL, (160, 120)), (SCREENNAIL, (1616, 1080))],
)
def test_previews_are_real_jpegs_of_their_own_geometry(kind, expected) -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            preview = capture.preview(kind, timeout_ms=2_000)
            assert is_jpeg(preview.data)
            assert looks_complete(preview.data)
            # Parsed from the bytes, not taken from the index: the index
            # reports the original still's size, which is not this.
            assert jpeg_dimensions(preview.data) == expected
            assert (preview.width, preview.height) == expected
            assert preview.mime == "image/jpeg"


@pytest.mark.parametrize("kind", [THUMBNAIL, SCREENNAIL])
def test_previews_are_marked_exact_still_associated(kind) -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            content = capture.wait_for_content(timeout_ms=2_000)
            preview = capture.preview(kind, timeout_ms=2_000)
            assert preview.is_exact_still
            assert preview.content_id == content.content_id
            assert preview.metadata["exact_still_association"] == "content_id"
            assert preview.metadata["file_id"] == content.file_id
            assert preview.metadata["filename"] == content.filename


def test_preview_bytes_cross_the_pipe_unchanged() -> None:
    """The blob carries the image as itself; nothing is encoded into text."""
    from tests.fake_host import PREVIEW_SHAPE, synth_jpeg
    from crsdkpy.backend import _cabi

    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            content = capture.wait_for_content(timeout_ms=2_000)
            preview = capture.preview(SCREENNAIL, timeout_ms=2_000)

    width, height, size = PREVIEW_SHAPE[_cabi.PREVIEW_SCREENNAIL]
    expected = synth_jpeg(
        content.content_id * 31 + _cabi.PREVIEW_SCREENNAIL, size, width, height
    )
    assert preview.data == expected
    assert preview.byte_length == len(expected)
    assert preview.metadata["byte_length"] == len(expected)


def test_the_two_forms_differ_for_the_same_still() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            thumbnail = capture.preview(THUMBNAIL, timeout_ms=2_000)
            screennail = capture.preview(SCREENNAIL, timeout_ms=2_000)
            assert thumbnail.data != screennail.data
            assert thumbnail.content_id == screennail.content_id
            assert thumbnail.byte_length < screennail.byte_length


def test_a_second_shot_cannot_be_served_the_first_shots_preview() -> None:
    """The stale-preview trap, from the client's side."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            first = session.capture()
            first_content = first.wait_for_content(timeout_ms=2_000)
            first_preview = first.preview(SCREENNAIL, timeout_ms=2_000)

            second = session.capture()
            second_content = second.wait_for_content(timeout_ms=2_000)
            second_preview = second.preview(SCREENNAIL, timeout_ms=2_000)

            assert second_content.content_id > first_content.content_id
            assert second_preview.content_id == second_content.content_id
            assert second_preview.data != first_preview.data


def test_a_preview_belonging_to_another_still_is_refused() -> None:
    """A host that answers for the wrong content must not go unnoticed."""
    with crsdkpy.SDK(backend=make_backend("stale_preview")) as sdk:
        with open_session(sdk) as session:
            first = session.capture()
            first.preview(SCREENNAIL, timeout_ms=2_000)

            second = session.capture()
            with pytest.raises(crsdkpy.CameraConnectionError) as excinfo:
                second.preview(SCREENNAIL, timeout_ms=2_000)
            assert "stale" in str(excinfo.value)


def test_unreadable_preview_bytes_are_refused() -> None:
    with crsdkpy.SDK(backend=make_backend("torn_preview")) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            with pytest.raises(crsdkpy.CameraConnectionError) as excinfo:
                capture.preview(SCREENNAIL, timeout_ms=2_000)
            assert "truncated" in str(excinfo.value)


def test_a_preview_is_cached_per_capture_and_kind() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            capture = session.capture()
            once = capture.preview(SCREENNAIL, timeout_ms=2_000)
            twice = capture.preview(SCREENNAIL, timeout_ms=2_000)
            assert once is twice
            assert capture.state is crsdkpy.CaptureState.PREVIEW_AVAILABLE


def test_a_negative_bound_does_not_wrap_to_the_largest_identifier() -> None:
    """The wire field is unsigned; a naive cast would match nothing."""
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            session.capture()

            def ids(**kwargs):
                return [c.content_id for c in sdk.backend.list_content(
                    session._id, **kwargs
                )]

            everything = ids()
            assert everything
            assert ids(newer_than=-1) == everything
            assert ids(newer_than=0) == everything


# -- the public content facade ----------------------------------------------
def test_content_is_reachable_without_touching_a_backend() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            assert session.content.available
            assert session.content.latest() is None

            session.capture()
            first = session.content.latest()
            assert first is not None
            assert first.filename == "DSC03400.ARW"

            session.capture()
            second = session.content.latest()
            assert second.content_id > first.content_id
            assert [c.content_id for c in session.content.since(first)] == [
                second.content_id
            ]
            assert len(list(session.content)) == 2


def test_content_facade_fetches_previews_by_identity() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk) as session:
            session.capture()
            item = session.content.latest()

            by_object = session.content.preview(item, SCREENNAIL)
            by_id = session.content.preview(item.content_id, SCREENNAIL)
            assert by_object.content_id == by_id.content_id == item.content_id
            assert by_object.is_exact_still

            thumbnail = session.content.preview(item, THUMBNAIL)
            assert thumbnail.width == 160


def test_content_facade_refuses_in_a_mode_without_an_index() -> None:
    with crsdkpy.SDK(backend=make_backend()) as sdk:
        with open_session(sdk, "remote") as session:
            assert not session.content.available
            assert "unavailable" in repr(session.content)
            for call in (
                session.content.latest,
                session.content.since,
                lambda: session.content.preview(1),
            ):
                with pytest.raises(crsdkpy.UnsupportedOperationError) as excinfo:
                    call()
                assert excinfo.value.capability == "content_index"
